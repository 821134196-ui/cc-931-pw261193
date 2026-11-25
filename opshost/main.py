"""命令行入口：python -m opshost.main --mode both --port 8000"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from .backends.live import become_subreaper
from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="opshost",
        description="本地运维作业托管工具：记录启动链与每代实例身份",
    )
    parser.add_argument("--mode", choices=["live", "sim", "both"], default="both",
                        help="live=仅真实作业，sim=仅模拟演练，both=两者都开（默认）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=".ops-data/state.json",
                        help="状态持久化文件（原子 JSON）")
    args = parser.parse_args(argv)

    # 真实后端需要 subreaper 身份来收养「父先退出」的 worker
    if args.mode in ("live", "both"):
        if not become_subreaper():
            print("警告: PR_SET_CHILD_SUBREAPER 失败，孤儿进程将无法被收养", file=sys.stderr)

    gw = serve(args.data, args.mode, args.host, args.port)
    print(f"opshost 已启动: http://{args.host}:{gw.port}/  (mode={args.mode}, data={args.data})")

    stop = {"flag": False}

    def _handle(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    try:
        while not stop["flag"]:
            time.sleep(0.2)
    finally:
        gw.shutdown()
        print("opshost 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
