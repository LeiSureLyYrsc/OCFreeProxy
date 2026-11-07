from __future__ import annotations

import time
from dataclasses import dataclass

from .store import ProxyEntry, Store


@dataclass
class SelectedProxy:
    entry: ProxyEntry
    httpx_proxy: str | None  # None = direct


class ProxyPool:
    """
    Select egress from enabled, non-cooling proxies.
    When use_direct is True, the synthetic `direct` member is included.
    Selection: least-recently-used among available (ties broken by created_at).
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self.store.ensure_direct_proxy()

    @property
    def use_direct(self) -> bool:
        return self.store.get_bool("use_direct", True)

    def set_use_direct(self, enabled: bool) -> None:
        self.store.set_bool("use_direct", enabled)

    def available(self, now: float | None = None) -> list[ProxyEntry]:
        now = now if now is not None else time.time()
        use_direct = self.use_direct
        result: list[ProxyEntry] = []
        for p in self.store.list_proxies():
            if not p.enabled:
                continue
            if p.protocol == "direct":
                if not use_direct:
                    continue
            elif p.protocol not in ("http", "socks5"):
                continue  # unsupported cannot be used for httpx egress
            elif not p.proxy_url:
                continue
            if p.cooldown_until and p.cooldown_until > now:
                continue
            result.append(p)
        # LRU: smallest last_used_at first
        result.sort(key=lambda x: (x.last_used_at, x.created_at))
        return result

    def select(self) -> SelectedProxy | None:
        avail = self.available()
        if not avail:
            return None
        chosen = avail[0]
        self.store.touch_proxy(chosen.id)
        proxy_url = chosen.proxy_url if chosen.protocol != "direct" else None
        # Re-read after touch for accurate last_used
        fresh = self.store.get_proxy(chosen.id) or chosen
        return SelectedProxy(entry=fresh, httpx_proxy=proxy_url)

    def mark_cooldown(self, proxy_id: str, seconds: int, reason: str = "") -> None:
        until = time.time() + max(1, int(seconds))
        self.store.set_cooldown(proxy_id, until, reason or f"cooldown {seconds}s")

    def clear_cooldown(self, proxy_id: str) -> None:
        self.store.clear_cooldown(proxy_id)

    def status(self) -> dict:
        now = time.time()
        use_direct = self.use_direct
        proxies = self.store.list_proxies()
        items = []
        for p in proxies:
            if p.protocol == "direct" and not use_direct:
                state = "excluded"
            elif not p.enabled:
                state = "disabled"
            elif p.protocol not in ("http", "socks5", "direct"):
                state = "unsupported"
            elif p.cooldown_until and p.cooldown_until > now:
                state = "cooling"
            else:
                state = "active"
            remaining = max(0, int(p.cooldown_until - now)) if state == "cooling" else 0
            items.append(
                {
                    **p.to_dict(),
                    "state": state,
                    "cooldown_remaining": remaining,
                    "in_pool": state == "active",
                }
            )
        active = sum(1 for i in items if i["state"] == "active")
        cooling = sum(1 for i in items if i["state"] == "cooling")
        return {
            "use_direct": use_direct,
            "active_count": active,
            "cooling_count": cooling,
            "proxies": items,
        }
