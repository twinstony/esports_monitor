"""esports_monitor 入口。

启动命令：
    cd d:\\workspace\\python\\esports_monitor
    python main.py
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    # 确保 UTF-8 控制台（Windows 兼容）
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    # 将脚本目录加入 sys.path，确保 esports_monitor 包可被导入
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    os.chdir(script_dir)

    parser = argparse.ArgumentParser(description="Esports Market Monitor")
    parser.add_argument(
        "--mode", choices=["console"], default="console",
        help="运行模式（默认 console）",
    )
    parser.add_argument(
        "--base-dir", default=script_dir,
        help="项目根目录（默认为脚本所在目录）",
    )
    args = parser.parse_args()

    if args.mode == "console":
        run_console(args.base_dir)


def run_console(base_dir: str) -> None:
    from esports_monitor.config.manager import load_config, setup_logging
    from esports_monitor.core.monitor import Monitor

    config = load_config(base_dir)
    setup_logging(config)
    monitor = Monitor(config=config, base_dir=base_dir)
    try:
        monitor.run_continuous()
    except KeyboardInterrupt:
        print("\n收到中断信号，正在停止...")
        monitor.stop()


if __name__ == "__main__":
    main()
