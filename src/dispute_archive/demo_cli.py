"""命令行入口。

用法：
    python3 -m dispute_archive.demo_cli fixtures/dispute_scenario.json \
        [--package out/package.json] [--log out/journal.jsonl]

读取虚构争议事件流，归档分流并打印端到端走查摘要，可选导出争议包与哈希链日志。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .demo import main as demo_main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="跨境数字订单争议归档与分流演示")
    parser.add_argument("fixture", type=Path, help="争议事件流 JSON（见 fixtures/dispute_scenario.json）")
    parser.add_argument("--package", type=Path, default=None, help="导出可复核争议包 JSON 的路径")
    parser.add_argument("--log", type=Path, default=None, help="导出哈希链日志 JSONL 的路径")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    if not args.fixture.exists():
        raise SystemExit(f"找不到夹具文件：{args.fixture}")
    demo_main(args.fixture, out_package=args.package, out_log=args.log)
