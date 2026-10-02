"""Manual entry point: ``python -m xtg.proxy [uri]``.

Probes a real MTProto proxy with the obfuscated2 handshake so a real endpoint can
be checked without hardcoding credentials anywhere:

.. code-block:: bash

    uv run python -m xtg.proxy "tg://proxy?server=host&port=443&secret=..."
    XTG_MTPROXY_URI="tg://proxy?..." uv run python -m xtg.proxy

Exits 0 when the proxy accepted the handshake, 1 otherwise. The secret is never
printed: the masked URL is used for output.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from xtg.core.errors import ProxyError
from xtg.proxy.mtproto import DEFAULT_DC_ID, probe_uri
from xtg.proxy.parser import parse_proxy_url


def main(argv: list[str] | None = None) -> int:
    """Probe one MTProto proxy and report the protocol level outcome."""
    parser = argparse.ArgumentParser(
        prog="python -m xtg.proxy",
        description="Health-check a real MTProto proxy using the obfuscated2 handshake.",
    )
    parser.add_argument(
        "uri",
        nargs="?",
        default=os.environ.get("XTG_MTPROXY_URI", ""),
        help="tg://proxy?server=...&port=...&secret=... (or set XTG_MTPROXY_URI)",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="connect timeout in seconds")
    parser.add_argument(
        "--settle",
        type=float,
        default=2.0,
        help="seconds to watch for a hang-up or a reply after the handshake",
    )
    parser.add_argument("--dc-id", type=int, default=DEFAULT_DC_ID, help="destination DC id")
    args = parser.parse_args(argv)

    if not args.uri:
        parser.error("no URI given (pass it as an argument or set XTG_MTPROXY_URI)")

    try:
        entry = parse_proxy_url("probe", args.uri)
        result = asyncio.run(
            probe_uri(
                args.uri,
                timeout=args.timeout,
                settle=args.settle,
                dc_id=args.dc_id,
            )
        )
    except ProxyError as exc:
        print(f"unusable proxy: {exc}", file=sys.stderr)
        return 1

    print(f"{entry.masked_url} -> {result.status}: {result.detail}")
    return 0 if result.status == "alive" else 1


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
