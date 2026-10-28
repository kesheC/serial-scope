"""环形缓冲 + 滚动统计，UI 与无界面模式共用."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class SampleWindow:
    """保存最近 N 个采样点；固定容量，长期运行不会吃内存."""

    capacity: int = 600
    values: deque[float] = field(default_factory=lambda: deque(maxlen=600))

    def __post_init__(self) -> None:
        self.values = deque(maxlen=self.capacity)

    def push(self, value: float) -> None:
        self.values.append(value)

    @property
    def latest(self) -> float | None:
        return self.values[-1] if self.values else None

    def min_max(self) -> tuple[float, float]:
        if not self.values:
            return (0.0, 0.0)
        return (min(self.values), max(self.values))

    def mean(self) -> float:
        return sum(self.values) / len(self.values) if self.values else 0.0


@dataclass
class Recorder:
    """CSV 录制：写一行就 flush，断电最多丢一行."""

    path: str
    _fh: object | None = None

    def open(self) -> None:
        self._fh = open(self.path, "w", encoding="utf-8", newline="")
        self._fh.write("timestamp_ms,seq,value\n")  # type: ignore[union-attr]

    def write(self, ts_ms: int, seq: int, value: float) -> None:
        if self._fh is None:
            return
        self._fh.write(f"{ts_ms},{seq},{value:.6f}\n")  # type: ignore[union-attr]
        self._fh.flush()  # type: ignore[union-attr]

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()  # type: ignore[union-attr]
            self._fh = None
