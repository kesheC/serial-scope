"""命令行入口：``python -m serialscope --demo`` 或 ``python -m serialscope --port COM3``."""

from __future__ import annotations

import argparse
import sys

from .app import App, Capture, demo_main
from .source import SimulatedSource, SerialSource, FileSource


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="serialscope", description="串口数据采集上位机")
    p.add_argument("--port", help="串口设备，例如 COM3 或 /dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=115200, help="波特率，默认 115200")
    p.add_argument("--file", help="回放抓包文件（与 --port 二选一）")
    p.add_argument("--demo", action="store_true", help="使用内置模拟器（无需硬件）")
    p.add_argument("--headless", action="store_true", help="不开窗口，只跑统计并打印")
    p.add_argument("--seconds", type=float, default=5.0, help="headless 模式运行时长")
    return p


def _safe_stdio() -> None:
    """把标准输出切到 UTF-8。

    Windows 控制台默认是 cp936/cp1252，直接 print 中文或箭头字符会抛
    ``UnicodeEncodeError``（CI 的 windows-latest 就是这样失败的）。
    ``errors="replace"`` 是兜底：即使终端真的不支持，也只是显示成问号，
    不会把一次正常采集判成失败。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):  # 重定向到文件/被替换的流
            pass


def main(argv: list[str] | None = None) -> int:
    _safe_stdio()
    args = build_parser().parse_args(argv)

    if args.demo and args.headless:
        return demo_main(seconds=args.seconds)

    if args.demo or (not args.port and not args.file):
        source = SimulatedSource()
    elif args.file:
        source = FileSource(args.file)
    else:
        source = SerialSource(args.port, args.baud)  # type: ignore[arg-type]

    if args.headless:
        from .app import run_headless

        snap = run_headless(source, seconds=args.seconds)
        print(f"{source.name} → 帧={snap.frames} CRC错={snap.crc_errors} 重同步={snap.resyncs} "
              f"丢帧={snap.lost} ({snap.loss_rate * 100:.1f}%) 均值={snap.mean:.2f}")
        return 0 if snap.frames else 1

    App(Capture(source), title=f"serial-scope · {source.name}").run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
