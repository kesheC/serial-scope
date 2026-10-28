"""界面与主循环：读线程只做解析，UI 只做渲染，两者用 queue 解耦."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

from .protocol import FrameDecoder, LossStats, Frame, TYPE_SAMPLE
from .source import Source, SimulatedSource
from .window import Recorder, SampleWindow


@dataclass
class Snapshot:
    """一次 UI 刷新所需的全部状态，避免 UI 直接读线程内存."""

    values: list[float]
    frames: int
    crc_errors: int
    resyncs: int
    lost: int
    loss_rate: float
    latest: float | None
    mean: float
    lo: float
    hi: float


class Capture:
    """把「数据源 → 解码 → 统计 → 窗口」串起来，UI 和 CLI 共用."""

    def __init__(self, source: Source, window_size: int = 600) -> None:
        self.source = source
        self.decoder = FrameDecoder()
        self.loss = LossStats()
        self.window = SampleWindow(capacity=window_size)
        self._q: queue.Queue[Frame] = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.recorder: Recorder | None = None
        self._t0 = time.monotonic()
        self.frames = 0

    # ---- 生命周期 ----

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, name="reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def close(self) -> None:
        self.stop()
        self.source.close()
        if self.recorder is not None:
            self.recorder.close()

    # ---- 读线程 ----

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self.source.read(timeout=0.05)
            except Exception:  # 串口拔掉等异常：保留统计后退出，不静默
                break
            if not chunk:
                continue
            for frame in self.decoder.feed(chunk):
                self.frames += 1
                if frame.type == TYPE_SAMPLE:
                    value = frame.sample
                    self.loss.observe(frame.seq)
                    self.window.push(value)
                    if self.recorder is not None:
                        ts = int((time.monotonic() - self._t0) * 1000)
                        self.recorder.write(ts, frame.seq, value)
                try:
                    self._q.put_nowait(frame)
                except queue.Full:
                    pass  # UI 跟不上时丢帧，但统计仍然完整

    # ---- UI 侧 ----

    def drain(self) -> int:
        """消费队列里还没处理的帧数（UI 用来判断是否需要重绘）."""
        n = 0
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return n
            n += 1

    def snapshot(self) -> Snapshot:
        lo, hi = self.window.min_max()
        return Snapshot(
            values=list(self.window.values),
            frames=self.decoder.stats.frames,
            crc_errors=self.decoder.stats.crc_errors,
            resyncs=self.decoder.stats.resyncs,
            lost=self.loss.lost,
            loss_rate=self.loss.loss_rate,
            latest=self.window.latest,
            mean=self.window.mean(),
            lo=lo,
            hi=hi,
        )


class App:
    """Tk 界面：一块画布 + 一行统计 + 三个按钮。"""

    def __init__(self, capture: Capture, title: str = "serial-scope") -> None:
        import tkinter as tk

        self.tk = tk
        self.capture = capture
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("900x460")
        self.root.configure(bg="#0f1720")

        self.canvas = tk.Canvas(self.root, bg="#0f1720", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=12, pady=(12, 6))

        bar = tk.Frame(self.root, bg="#0f1720")
        bar.pack(fill="x", padx=12, pady=(0, 10))
        self.stats_var = tk.StringVar(value="等待数据…")
        tk.Label(bar, textvariable=self.stats_var, fg="#8fd3c1", bg="#0f1720",
                 font=("Consolas", 10)).pack(side="left")
        self.btn = tk.Button(bar, text="停止", command=self.toggle,
                             bg="#1f3b4d", fg="#d7e6ee", relief="flat", padx=14)
        self.btn.pack(side="right")
        self.rec_btn = tk.Button(bar, text="录制 CSV", command=self.toggle_record,
                                 bg="#1f3b4d", fg="#d7e6ee", relief="flat", padx=14)
        self.rec_btn.pack(side="right", padx=(0, 8))

    # ---- 交互 ----

    def toggle(self) -> None:
        if self.btn["text"] == "停止":
            self.capture.stop()
            self.btn.config(text="开始")
        else:
            self.capture.start()
            self.btn.config(text="停止")

    def toggle_record(self) -> None:
        if self.capture.recorder is None:
            rec = Recorder(f"capture-{int(time.time())}.csv")
            rec.open()
            self.capture.recorder = rec
            self.rec_btn.config(text="停止录制")
        else:
            self.capture.recorder.close()
            self.capture.recorder = None
            self.rec_btn.config(text="录制 CSV")

    # ---- 渲染 ----

    def draw(self, snap: Snapshot) -> None:
        c = self.canvas
        c.delete("all")
        w = max(c.winfo_width(), 200)
        h = max(c.winfo_height(), 120)
        pad = 46
        lo, hi = snap.lo, snap.hi
        if hi - lo < 1e-6:
            lo, hi = lo - 1, hi + 1
        span = hi - lo

        # 网格 + 刻度
        for i in range(5):
            y = pad + (h - 2 * pad) * i / 4
            c.create_line(pad, y, w - pad, y, fill="#1c2a36")
            c.create_text(4, y, anchor="w", fill="#5b7285", font=("Consolas", 8),
                          text=f"{hi - span * i / 4:7.1f}")

        n = len(snap.values)
        if n >= 2:
            step = (w - 2 * pad) / max(1, n - 1)
            pts: list[float] = []
            for i, v in enumerate(snap.values):
                x = pad + i * step
                y = pad + (h - 2 * pad) * (1 - (v - lo) / span)
                pts.extend((x, y))
            c.create_line(*pts, fill="#4fd1b3", width=2, smooth=True)
            lx, ly = pts[-2], pts[-1]
            c.create_oval(lx - 3, ly - 3, lx + 3, ly + 3, fill="#ffd479", outline="")

        self.stats_var.set(
            f"帧 {snap.frames:6d} | CRC错 {snap.crc_errors:4d} | 重同步 {snap.resyncs:4d} | "
            f"丢帧 {snap.lost:4d} ({snap.loss_rate * 100:4.1f}%) | "
            f"当前 {snap.latest if snap.latest is not None else 0:7.2f} | 均值 {snap.mean:7.2f}"
        )

    def tick(self) -> None:
        if self.capture.drain():
            self.draw(self.capture.snapshot())
        self.root.after(40, self.tick)

    def run(self) -> None:
        self.capture.start()
        self.root.after(60, self.tick)
        try:
            self.root.mainloop()
        finally:
            self.capture.close()


def run_headless(source: Source, seconds: float = 5.0, window_size: int = 600) -> Snapshot:
    """无界面模式：CI / 没有显示器 / 想快速验证链路时用."""
    cap = Capture(source, window_size=window_size)
    cap.start()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)
    cap.stop()
    snap = cap.snapshot()
    cap.close()
    return snap


def demo_main(seconds: float = 5.0) -> int:
    snap = run_headless(SimulatedSource(seed=7), seconds=seconds)
    print(f"simulator {seconds:.0f}s → 帧={snap.frames} CRC错={snap.crc_errors} "
          f"重同步={snap.resyncs} 丢帧={snap.lost} ({snap.loss_rate * 100:.1f}%) "
          f"最新={snap.latest:.2f} 均值={snap.mean:.2f}")
    return 0 if snap.frames > 0 else 1
