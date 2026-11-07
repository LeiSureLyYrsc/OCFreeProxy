from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator


@dataclass
class ProxyEntry:
    id: str
    name: str
    proxy_url: str  # http:// / https:// / socks5:// or empty for direct
    protocol: str  # http | socks5 | direct | unsupported
    enabled: bool = True
    source: str = "manual"  # manual | clash | subscription | direct
    subscription_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    cooldown_until: float = 0.0
    cooldown_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class Subscription:
    id: str
    name: str
    kind: str  # clash_url | paste
    content: str  # URL or raw pasted content
    enabled: bool = True
    last_refresh_at: float = 0.0
    last_error: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Store:
    """SQLite-backed persistence for proxies, subscriptions, and settings."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_refresh_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proxies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    proxy_url TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    source TEXT NOT NULL DEFAULT 'manual',
                    subscription_id TEXT,
                    meta TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    last_used_at REAL NOT NULL DEFAULT 0,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    cooldown_reason TEXT NOT NULL DEFAULT ''
                );
                """
            )

    # ----- settings -----
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get_setting(key)
        if v is None:
            return default
        return v.lower() in ("1", "true", "yes", "on")

    def set_bool(self, key: str, value: bool) -> None:
        self.set_setting(key, "true" if value else "false")

    # ----- subscriptions -----
    def list_subscriptions(self) -> list[Subscription]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions ORDER BY created_at DESC"
            ).fetchall()
            return [self._row_to_sub(r) for r in rows]

    def get_subscription(self, sub_id: str) -> Subscription | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?", (sub_id,)
            ).fetchone()
            return self._row_to_sub(row) if row else None

    def add_subscription(
        self, name: str, kind: str, content: str, enabled: bool = True
    ) -> Subscription:
        sub = Subscription(
            id=str(uuid.uuid4()),
            name=name or f"{kind}-{int(time.time())}",
            kind=kind,
            content=content.strip(),
            enabled=enabled,
        )
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO subscriptions"
                "(id,name,kind,content,enabled,last_refresh_at,last_error,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    sub.id,
                    sub.name,
                    sub.kind,
                    sub.content,
                    1 if sub.enabled else 0,
                    sub.last_refresh_at,
                    sub.last_error,
                    sub.created_at,
                ),
            )
        return sub

    def update_subscription(self, sub: Subscription) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE subscriptions SET name=?, kind=?, content=?, enabled=?, "
                "last_refresh_at=?, last_error=? WHERE id=?",
                (
                    sub.name,
                    sub.kind,
                    sub.content,
                    1 if sub.enabled else 0,
                    sub.last_refresh_at,
                    sub.last_error,
                    sub.id,
                ),
            )

    def delete_subscription(self, sub_id: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM proxies WHERE subscription_id = ?", (sub_id,))
            conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))

    def _row_to_sub(self, row: sqlite3.Row) -> Subscription:
        return Subscription(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            content=row["content"],
            enabled=bool(row["enabled"]),
            last_refresh_at=row["last_refresh_at"],
            last_error=row["last_error"] or "",
            created_at=row["created_at"],
        )

    # ----- proxies -----
    def list_proxies(self, include_direct: bool = True) -> list[ProxyEntry]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM proxies ORDER BY created_at ASC"
            ).fetchall()
            items = [self._row_to_proxy(r) for r in rows]
        if not include_direct:
            items = [p for p in items if p.protocol != "direct"]
        return items

    def get_proxy(self, proxy_id: str) -> ProxyEntry | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM proxies WHERE id = ?", (proxy_id,)
            ).fetchone()
            return self._row_to_proxy(row) if row else None

    def upsert_proxy(self, proxy: ProxyEntry) -> ProxyEntry:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO proxies"
                "(id,name,proxy_url,protocol,enabled,source,subscription_id,meta,"
                "created_at,last_used_at,cooldown_until,cooldown_reason)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " name=excluded.name, proxy_url=excluded.proxy_url,"
                " protocol=excluded.protocol, enabled=excluded.enabled,"
                " source=excluded.source, subscription_id=excluded.subscription_id,"
                " meta=excluded.meta, last_used_at=excluded.last_used_at,"
                " cooldown_until=excluded.cooldown_until,"
                " cooldown_reason=excluded.cooldown_reason",
                (
                    proxy.id,
                    proxy.name,
                    proxy.proxy_url,
                    proxy.protocol,
                    1 if proxy.enabled else 0,
                    proxy.source,
                    proxy.subscription_id,
                    json.dumps(proxy.meta, ensure_ascii=False),
                    proxy.created_at,
                    proxy.last_used_at,
                    proxy.cooldown_until,
                    proxy.cooldown_reason,
                ),
            )
        return proxy

    def add_proxy(
        self,
        name: str,
        proxy_url: str,
        protocol: str,
        *,
        source: str = "manual",
        subscription_id: str | None = None,
        meta: dict[str, Any] | None = None,
        enabled: bool = True,
        proxy_id: str | None = None,
    ) -> ProxyEntry:
        entry = ProxyEntry(
            id=proxy_id or str(uuid.uuid4()),
            name=name,
            proxy_url=proxy_url,
            protocol=protocol,
            enabled=enabled,
            source=source,
            subscription_id=subscription_id,
            meta=meta or {},
        )
        return self.upsert_proxy(entry)

    def set_proxy_enabled(self, proxy_id: str, enabled: bool) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE proxies SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, proxy_id),
            )

    def touch_proxy(self, proxy_id: str, when: float | None = None) -> None:
        ts = when if when is not None else time.time()
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE proxies SET last_used_at = ? WHERE id = ?",
                (ts, proxy_id),
            )

    def set_cooldown(
        self, proxy_id: str, until: float, reason: str = ""
    ) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE proxies SET cooldown_until = ?, cooldown_reason = ? WHERE id = ?",
                (until, reason, proxy_id),
            )

    def clear_cooldown(self, proxy_id: str) -> None:
        self.set_cooldown(proxy_id, 0.0, "")

    def delete_proxy(self, proxy_id: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "DELETE FROM proxies WHERE id = ? AND protocol != 'direct'",
                (proxy_id,),
            )

    def replace_subscription_proxies(
        self, subscription_id: str, proxies: list[ProxyEntry]
    ) -> None:
        with self._lock, self._conn() as conn:
            # Preserve cooldown/last_used for matching proxy_url
            old = {
                r["proxy_url"]: r
                for r in conn.execute(
                    "SELECT * FROM proxies WHERE subscription_id = ?",
                    (subscription_id,),
                ).fetchall()
            }
            conn.execute(
                "DELETE FROM proxies WHERE subscription_id = ?", (subscription_id,)
            )
            for p in proxies:
                prev = old.get(p.proxy_url)
                if prev:
                    p.last_used_at = prev["last_used_at"] or 0.0
                    p.cooldown_until = prev["cooldown_until"] or 0.0
                    p.cooldown_reason = prev["cooldown_reason"] or ""
                    if "enabled" in prev.keys():
                        # keep previous enable flag if same URL
                        p.enabled = bool(prev["enabled"])
                conn.execute(
                    "INSERT INTO proxies"
                    "(id,name,proxy_url,protocol,enabled,source,subscription_id,meta,"
                    "created_at,last_used_at,cooldown_until,cooldown_reason)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        p.id,
                        p.name,
                        p.proxy_url,
                        p.protocol,
                        1 if p.enabled else 0,
                        p.source,
                        p.subscription_id,
                        json.dumps(p.meta, ensure_ascii=False),
                        p.created_at,
                        p.last_used_at,
                        p.cooldown_until,
                        p.cooldown_reason,
                    ),
                )

    def ensure_direct_proxy(self) -> ProxyEntry:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM proxies WHERE id = 'direct'"
            ).fetchone()
            if row:
                return self._row_to_proxy(row)
        return self.add_proxy(
            name="本地连接 (Direct)",
            proxy_url="",
            protocol="direct",
            source="direct",
            proxy_id="direct",
            enabled=True,
        )

    def _row_to_proxy(self, row: sqlite3.Row) -> ProxyEntry:
        meta_raw = row["meta"] or "{}"
        try:
            meta = json.loads(meta_raw)
        except json.JSONDecodeError:
            meta = {}
        return ProxyEntry(
            id=row["id"],
            name=row["name"],
            proxy_url=row["proxy_url"],
            protocol=row["protocol"],
            enabled=bool(row["enabled"]),
            source=row["source"],
            subscription_id=row["subscription_id"],
            meta=meta,
            created_at=row["created_at"],
            last_used_at=row["last_used_at"] or 0.0,
            cooldown_until=row["cooldown_until"] or 0.0,
            cooldown_reason=row["cooldown_reason"] or "",
        )
