"""链路层测试：模拟源 → 解码 → 窗口/统计；以及 CSV 录制."""

from __future__ import annotations

import os
import tempfile
import unittest

from serialscope.app import Capture, run_headless
from serialscope.protocol import encode_sample
from serialscope.source import SimulatedSource, FileSource, status_frame
from serialscope.window import Recorder, SampleWindow


class ScriptedSource:
    """按脚本吐数据，测试里不依赖时间."""

    name = "scripted"

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, timeout: float = 0.1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        return None


class TestCapture(unittest.TestCase):
    def test_capture_counts_and_window(self) -> None:
        src = ScriptedSource([encode_sample(i, float(i)) for i in range(5)])
        cap = Capture(src, window_size=3)
        cap.start()
        cap.stop()
        snap = cap.snapshot()
        self.assertEqual(snap.frames, 5)
        self.assertEqual(len(snap.values), 3)  # 环形缓冲只留最近 3 个
        self.assertAlmostEqual(snap.latest or 0, 4.0, places=6)
        cap.close()

    def test_status_frames_not_counted_as_samples(self) -> None:
        src = ScriptedSource([status_frame("ok") + encode_sample(1, 1.0)])
        cap = Capture(src)
        cap.start()
        cap.stop()
        snap = cap.snapshot()
        self.assertEqual(snap.frames, 2)  # 帧数包含状态帧
        self.assertEqual(len(snap.values), 1)  # 采样窗口只收采样帧
        cap.close()


class TestSimulator(unittest.TestCase):
    def test_simulator_produces_frames_and_losses(self) -> None:
        snap = run_headless(SimulatedSource(hz=200, drop_rate=0.2, seed=1), seconds=0.6)
        self.assertGreater(snap.frames, 10)
        # 丢帧率是"设计进去"的，这里验证统计链路真的在算
        self.assertGreater(snap.lost, 0)

    def test_simulator_clean_has_no_loss(self) -> None:
        snap = run_headless(SimulatedSource(hz=200, drop_rate=0.0, noise_byte_rate=0.0, seed=2),
                            seconds=0.5)
        self.assertEqual(snap.lost, 0)
        self.assertEqual(snap.crc_errors, 0)


class TestWindowAndRecorder(unittest.TestCase):
    def test_window_capacity(self) -> None:
        w = SampleWindow(capacity=4)
        for v in range(10):
            w.push(float(v))
        self.assertEqual(list(w.values), [6.0, 7.0, 8.0, 9.0])
        self.assertEqual(w.min_max(), (6.0, 9.0))
        self.assertAlmostEqual(w.mean(), 7.5)

    def test_recorder_writes_rows(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cap.csv")
            rec = Recorder(path)
            rec.open()
            rec.write(0, 1, 1.5)
            rec.write(20, 2, 2.5)
            rec.close()
            with open(path, encoding="utf-8") as fh:
                rows = fh.read().strip().splitlines()
            self.assertEqual(rows[0], "timestamp_ms,seq,value")
            self.assertEqual(rows[1], "0,1,1.500000")
            self.assertEqual(len(rows), 3)

    def test_file_source_replays(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "dump.bin")
            with open(path, "wb") as fh:
                fh.write(b"".join(encode_sample(i, float(i)) for i in range(20)))
            snap = run_headless(FileSource(path, chunk=7), seconds=0.4)
            self.assertEqual(snap.frames, 20)


if __name__ == "__main__":
    unittest.main()
