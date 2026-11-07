from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import yaml

from .store import ProxyEntry



SUPPORTED_PROXY_TYPES = {"http", "https", "socks5", "socks5h", "socks4"}

_BARE_PROXY_RE = re.compile(
    r"^(?:(?P<user>[^@\s/]+)@)?(?P<host>\[[0-9a-fA-F:]+\]|[0-9a-zA-Z.-]+):(?P<port>\d{2,5})$"
)


def is_proxy_url(text: str) -> bool:
    """True when the line is an HTTP/SOCKS proxy endpoint, not a subscription URL."""
    t = (text or "").strip()
    if not t:
        return False
    lower = t.lower()
    if lower.startswith(("socks5://", "socks5h://", "socks4://", "socks://")):
        return True
    if lower.startswith(("http://", "https://")):
        parsed = urlparse(t)
        if not parsed.hostname:
            return False
        path = (parsed.path or "").rstrip("/")
        if parsed.port and path in ("", "/"):
            return True
        if parsed.username is not None and parsed.port:
            return True
        return False
    return bool(_BARE_PROXY_RE.match(t))


def is_subscription_fetch_url(text: str) -> bool:
    """Single-line http(s) URL that should be fetched as Clash/base64 subscription."""
    t = (text or "").strip()
    if any(ch in (text or "") for ch in ("\n", "\r")):
        return False
    lower = t.lower()
    if not lower.startswith(("http://", "https://")):
        return False
    return not is_proxy_url(t)


def normalize_proxy_url(text: str) -> tuple[str, str]:
    """Return (proxy_url, protocol) for http/socks5. Raises ValueError if invalid."""
    t = (text or "").strip()
    if not t:
        raise ValueError("proxy_url required")
    lower = t.lower()
    if lower.startswith(("socks5://", "socks5h://", "socks4://", "socks://")):
        # Force parseable scheme for urlparse
        to_parse = t
        if lower.startswith("socks://"):
            to_parse = "socks5://" + t.split("://", 1)[1]
        parsed = urlparse(to_parse)
        if not parsed.hostname:
            raise ValueError("socks 代理缺少主机")
        port = parsed.port or 1080
        auth = ""
        if parsed.username is not None:
            auth = f"{parsed.username}:{parsed.password or ''}@"
        url = f"socks5://{auth}{parsed.hostname}:{port}"
        return url, "socks5"
    if lower.startswith(("http://", "https://")):
        parsed = urlparse(t)
        if not parsed.hostname or not parsed.port:
            raise ValueError("http 代理需要 host:port")
        return t, "http"
    m = _BARE_PROXY_RE.match(t)
    if m:
        user = m.group("user")
        host = m.group("host")
        port = m.group("port")
        auth = f"{user}@" if user else ""
        url = f"http://{auth}{host}:{port}"
        return url, "http"
    raise ValueError("仅支持 http(s)://、socks5:// 或 host:port")



@dataclass
class ParsedNode:
    name: str
    proxy_url: str
    protocol: str  # http | socks5 | unsupported
    meta: dict[str, Any]


def _b64_decode(data: str) -> bytes:
    data = data.strip().replace("\n", "").replace("\r", "").replace(" ", "")
    pad = (-len(data)) % 4
    if pad:
        data += "=" * pad
    return base64.urlsafe_b64decode(data)


def _try_b64_text(data: str) -> str | None:
    try:
        raw = _b64_decode(data)
        text = raw.decode("utf-8", errors="replace")
        # Heuristic: looks like nodes or yaml/json
        if any(
            x in text
            for x in (
                "vmess://",
                "vless://",
                "trojan://",
                "ss://",
                "hysteria",
                "proxies:",
                "socks5://",
                "http://",
                "https://",
            )
        ) or "\n" in text:
            return text
        # many base64 subs are one URI per line
        if re.search(r"^[a-z0-9+\-]+:/", text, re.I | re.M):
            return text
    except (binascii.Error, ValueError, UnicodeError):
        return None
    return None


def _build_proxy_url(
    scheme: str, host: str, port: int, username: str | None = None, password: str | None = None
) -> str:
    auth = ""
    if username is not None:
        user = username
        pwd = password or ""
        auth = f"{user}:{pwd}@"
    return f"{scheme}://{auth}{host}:{port}"


def parse_clash_proxy(item: dict[str, Any]) -> ParsedNode | None:
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or item.get("server") or "proxy")
    ptype = str(item.get("type") or "").lower().strip()
    server = item.get("server") or item.get("servername")
    port = item.get("port")
    if not server or not port:
        # maybe already a URL-like
        return None

    username = item.get("username") or item.get("user")
    password = item.get("password")

    if ptype in ("http", "https"):
        # Clash http proxy
        scheme = "http"
        if item.get("tls"):
            scheme = "https"
        url = _build_proxy_url(scheme, str(server), int(port), username, password)
        return ParsedNode(name=name, proxy_url=url, protocol="http", meta=dict(item))

    if ptype in ("socks5", "socks", "socks5h"):
        url = _build_proxy_url("socks5", str(server), int(port), username, password)
        return ParsedNode(name=name, proxy_url=url, protocol="socks5", meta=dict(item))

    # Unsupported for direct httpx (vmess/vless/trojan/ss/hysteria...)
    return ParsedNode(
        name=name,
        proxy_url="",
        protocol="unsupported",
        meta={**dict(item), "clash_type": ptype, "server": server, "port": port},
    )


def parse_uri_line(line: str) -> ParsedNode | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Plain proxy URLs (http/socks5) or bare host:port
    lower = line.lower()
    if lower.startswith(("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks://")) or _BARE_PROXY_RE.match(line):
        try:
            url, protocol = normalize_proxy_url(line)
        except ValueError:
            return None
        parsed = urlparse(url)
        name = parsed.hostname or line
        return ParsedNode(name=name, proxy_url=url, protocol=protocol, meta={"raw": line})

    # ss://method:pass@host:port#name  or ss://base64
    if lower.startswith("ss://"):
        return ParsedNode(
            name=_uri_fragment_name(line, "ss"),
            proxy_url="",
            protocol="unsupported",
            meta={"raw": line, "scheme": "ss"},
        )

    if lower.startswith(("vmess://", "vless://", "trojan://", "hysteria2://", "hy2://", "hysteria://")):
        scheme = line.split("://", 1)[0].lower()
        name = _uri_fragment_name(line, scheme)
        meta: dict[str, Any] = {"raw": line, "scheme": scheme}
        if lower.startswith("vmess://"):
            try:
                payload = json.loads(_b64_decode(line[8:]).decode("utf-8", errors="replace"))
                meta["vmess"] = payload
                name = str(payload.get("ps") or name)
            except Exception:
                pass
        return ParsedNode(name=name, proxy_url="", protocol="unsupported", meta=meta)

    return None


def _uri_fragment_name(uri: str, fallback: str) -> str:
    if "#" in uri:
        return unquote(uri.split("#", 1)[1]) or fallback
    return fallback


def parse_clash_yaml(text: str) -> list[ParsedNode]:
    data = yaml.safe_load(text)
    if data is None:
        return []
    if isinstance(data, list):
        proxies = data
    elif isinstance(data, dict):
        proxies = data.get("proxies") or data.get("Proxy") or []
    else:
        return []
    nodes: list[ParsedNode] = []
    for item in proxies:
        node = parse_clash_proxy(item) if isinstance(item, dict) else None
        if node:
            nodes.append(node)
    return nodes


def parse_subscription_content(raw: str) -> list[ParsedNode]:
    """Parse Clash YAML/JSON, base64 node list, or plain URI/URL lines."""
    text = (raw or "").strip()
    if not text:
        return []

    # If looks like pure base64 blob, decode first
    if "://" not in text and "proxies:" not in text and not text.lstrip().startswith(("{", "[")):
        decoded = _try_b64_text(text)
        if decoded:
            text = decoded.strip()

    # Clash / YAML
    if "proxies:" in text or text.lstrip().startswith(("proxies:", "mixed-port:", "port:")):
        try:
            return parse_clash_yaml(text)
        except Exception:
            pass

    # JSON clash-like
    if text.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "proxies" in data:
                return parse_clash_yaml(yaml.safe_dump(data))
            if isinstance(data, list) and data and isinstance(data[0], dict) and "type" in data[0]:
                return [n for n in (parse_clash_proxy(i) for i in data) if n]
        except Exception:
            pass

    # Line-based (possibly still base64 per-line after decode)
    nodes: list[ParsedNode] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # nested base64 line?
        if "://" not in line and len(line) > 20:
            inner = _try_b64_text(line)
            if inner:
                for sub in inner.splitlines():
                    n = parse_uri_line(sub.strip())
                    if n:
                        nodes.append(n)
                continue
        n = parse_uri_line(line)
        if n:
            nodes.append(n)
    return nodes


async def fetch_subscription_url(url: str, timeout: float = 30.0) -> str:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "OCFreeProxy/0.1"})
        resp.raise_for_status()
        # Some providers return base64 as text/plain
        content_type = resp.headers.get("content-type", "")
        text = resp.text
        if "yaml" in content_type or "proxies:" in text:
            return text
        # try decode base64 whole body
        decoded = _try_b64_text(text)
        return decoded if decoded else text


def nodes_to_proxy_entries(
    nodes: list[ParsedNode],
    *,
    source: str,
    subscription_id: str | None,
    include_unsupported: bool = True,
) -> list[ProxyEntry]:
    entries: list[ProxyEntry] = []
    for node in nodes:
        if node.protocol == "unsupported" and not include_unsupported:
            continue
        entries.append(
            ProxyEntry(
                id=str(uuid.uuid4()),
                name=node.name,
                proxy_url=node.proxy_url,
                protocol=node.protocol,
                enabled=node.protocol != "unsupported",
                source=source,
                subscription_id=subscription_id,
                meta=node.meta,
            )
        )
    return entries


def summarize_nodes(nodes: list[ParsedNode]) -> dict[str, int]:
    usable = sum(1 for n in nodes if n.protocol in ("http", "socks5"))
    unsupported = sum(1 for n in nodes if n.protocol == "unsupported")
    return {"total": len(nodes), "usable": usable, "unsupported": unsupported}
