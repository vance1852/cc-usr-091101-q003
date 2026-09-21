"""收件箱工人命令行入口（不被包 __init__ 预导入，可安全用 -m 执行）。

用法：
    python3 -m asset_hub.cli --root <账本根> --recover --once
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .hub import AssetHub
from .inbox import Inbox, Worker, recover_in_flight
from .store import Store


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="分镜资产中枢收件箱工人")
    parser.add_argument("--root", required=True, help="Store 与 inbox 的根目录")
    parser.add_argument("--once", action="store_true", help="处理一轮后退出")
    parser.add_argument("--recover", action="store_true", help="启动时先恢复 in_flight 派发")
    args = parser.parse_args(argv)

    root = Path(args.root)
    store = Store(root)
    hub = AssetHub(store)
    inbox = Inbox(root / "inbox")
    if args.recover:
        for r in recover_in_flight(hub):
            print(json.dumps(r, ensure_ascii=False))
    worker = Worker(hub, inbox)
    for r in worker.run_once():
        print(json.dumps(r, ensure_ascii=False))
    if not args.once:  # pragma: no cover
        worker.run_forever()
    store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
