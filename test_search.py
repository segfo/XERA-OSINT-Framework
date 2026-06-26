"""Google 検索遷移テスト"""
import asyncio
import anyio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from tor_mcp.browser import goto_smart, get_session, shutdown_session_async
from tor_mcp.config import TorConfig


async def main() -> None:
    cfg = TorConfig.from_env()
    cfg.headless = False

    print("1. Google を開く...")
    result = await goto_smart("https://www.google.com", cfg)
    print(f"   via={result['via']} status={result['status']} title={result['title']!r}")

    session = get_session(cfg)

    print("2. 検索ボックスに入力...")
    await session.fill("textarea[name='q']", "AIエージェントとは")

    print("3. Enter で検索実行...")
    result = await session.press("textarea[name='q']", "Enter")
    print(f"   url={result['url']!r}")
    print(f"   title={result['title']!r}")

    print("4. 検索結果が読み込まれるまで待機...")
    await session.wait_for("load", timeout_ms=10000)
    snap = await session.state()
    print(f"   title={snap['title']!r}")
    links = snap.get("links", [])
    print(f"   links ({len(links)} 件):")
    for link in links[:8]:
        print(f"   - {link}")

    await anyio.to_thread.run_sync(lambda: input("\nPress Enter to close browser..."))
    await shutdown_session_async()


anyio.run(main)
