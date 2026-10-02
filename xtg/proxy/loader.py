"""Loading proxy lists from disk.

Canonical format (``xtg/proxy/list_proxy.json``)::

    {"proxys": {"P1": "socks5://host:port", "P2": "socks5://user:pass@host:port"}}

For convenience the loader also accepts ``"proxies"`` (correct plural), a bare
list of URLs and a list of objects with ``host``/``port``/``username`` keys.
Invalid entries never abort the load: they are reported separately so that
``xtg proxy list`` can show what is wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xtg.core.errors import ProxyError
from xtg.proxy.parser import ProxyEntry, parse_proxy_url

LIST_KEYS: tuple[str, ...] = ("proxys", "proxies")


@dataclass(slots=True)
class InvalidProxy:
    """An entry that could not be parsed, kept for diagnostics."""

    name: str
    raw: str
    reason: str


@dataclass(slots=True)
class ProxyLoadResult:
    """Outcome of reading a proxy list file."""

    path: Path
    entries: list[ProxyEntry] = field(default_factory=list)
    invalid: list[InvalidProxy] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.entries) + len(self.invalid)

    def __bool__(self) -> bool:
        return bool(self.entries)


def load_proxy_list(path: Path) -> ProxyLoadResult:
    """Read and parse a proxy list file."""
    if not path.exists():
        raise ProxyError(
            f"Proxy list not found: {path}",
            hint="Create xtg/proxy/list_proxy.json or set XTG_PROXY_FILE.",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProxyError(f"Proxy list {path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ProxyError(f"Cannot read proxy list {path}: {exc}") from exc

    result = ProxyLoadResult(path=path)
    for name, raw, error in _iter_raw_entries(payload):
        if error is not None:
            result.invalid.append(InvalidProxy(name=name, raw=raw, reason=error))
            continue
        try:
            result.entries.append(parse_proxy_url(name, raw))
        except ProxyError as exc:
            result.invalid.append(InvalidProxy(name=name, raw=raw, reason=str(exc)))
    return result


def _iter_raw_entries(payload: Any) -> list[tuple[str, str, str | None]]:
    """Normalise the supported JSON shapes into ``(name, raw, error)`` triples."""
    if isinstance(payload, str):
        return [("P1", payload, None)]

    if isinstance(payload, list):
        return [
            _item_to_raw(item, index)
            for index, item in enumerate(payload)
            if isinstance(item, (str, dict))
        ]

    if isinstance(payload, dict):
        for key in LIST_KEYS:
            section = payload.get(key)
            if isinstance(section, dict):
                return [(str(name), str(raw), None) for name, raw in section.items()]
            if isinstance(section, list):
                return [
                    _item_to_raw(item, index)
                    for index, item in enumerate(section)
                    if isinstance(item, (str, dict))
                ]
        # A flat mapping of name -> url is accepted as well.
        if payload and all(isinstance(value, str) for value in payload.values()):
            return [(str(name), str(raw), None) for name, raw in payload.items()]

    raise ProxyError(
        "Unsupported proxy list format",
        hint='Expected {"proxys": {"P1": "socks5://host:port"}}, a list or a flat mapping.',
    )


def _item_to_raw(item: str | dict[str, Any], index: int) -> tuple[str, str, str | None]:
    if isinstance(item, str):
        return (f"P{index + 1}", item, None)
    try:
        return (_name_from_object(item, index), _raw_from_any(item), None)
    except ProxyError as exc:
        return (_name_from_object(item, index), "", str(exc))


def _name_from_object(item: Any, fallback_index: int = 0) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or item.get("id") or f"P{fallback_index + 1}")
    return f"P{fallback_index + 1}"


def _raw_from_any(item: str | dict[str, Any]) -> str:
    if isinstance(item, str):
        return item
    host = item.get("host")
    port = item.get("port")
    if not host or not port:
        raise ProxyError(f"Proxy entry {item!r} needs both 'host' and 'port'")
    scheme = item.get("scheme") or item.get("type") or "socks5"
    username = item.get("username")
    password = item.get("password")
    auth = f"{username}:{password}@" if username and password else ""
    return f"{scheme}://{auth}{host}:{port}"


def write_proxy_list(path: Path, entries: list[ProxyEntry]) -> Path:
    """Persist ``entries`` back to the canonical JSON format."""
    payload = {LIST_KEYS[0]: {entry.name: entry.url for entry in entries}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
