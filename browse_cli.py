"""Interactive browser CLI for testing Tor/direct routing.

Usage:
    uv run browse_cli.py

At the prompt, enter a URL and choose a routing mode:
  a = auto   (.onion → Tor; clearnet → Direct, fallback to Tor on error)
  t = tor    (always route through Tor)
  d = direct (always direct; .onion rejected)
"""

import asyncio
import sys
from pathlib import Path

# Make tor_mcp importable from the OSINT directory
sys.path.insert(0, str(Path(__file__).parent))

import anyio
from tor_mcp.browser import goto_smart, shutdown_session_async
from tor_mcp.config import TorConfig

MODE_MAP = {"a": "auto", "t": "tor", "d": "direct"}


async def _run() -> None:
    config = TorConfig.from_env()
    print("=== browse_cli  (Ctrl-C or empty URL to quit) ===")
    print("Routing modes: a=auto  t=tor  d=direct\n")

    while True:
        try:
            url = input("URL (Enter to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not url:
            break

        raw = input("Mode [a/t/d] (a): ").strip().lower() or "a"
        mode = MODE_MAP.get(raw)
        if mode is None:
            print(f"  Unknown mode {raw!r}. Use a, t, or d.\n")
            continue

        print(f"  Opening via mode={mode!r} ...")
        try:
            result = await goto_smart(url, config=config, mode=mode)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {exc}\n")
            continue

        via    = result.get("via", "?")
        status = result.get("status", "?")
        title  = result.get("title", "")
        text   = result.get("text", "")

        print(f"  → via={via}  status={status}  title={title!r}")
        if text:
            preview = text[:500].replace("\n", " ")
            print(f"  ---\n  {preview}")
        print()

    await shutdown_session_async()
    print("Bye.")


if __name__ == "__main__":
    anyio.run(_run)
