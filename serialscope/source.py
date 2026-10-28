"""数据源：真实串口 / 模拟器 / 回放文件，对上暴露同一接口.

为什么抽这一层：UI 只依赖 ``Source.read()``，所以没有硬件时也能演示、
在 CI 里也能跑完整链路。
"""

from __future__ import annotations

import math
import random
import time
from typing import Protocol

from .protocol import encode_sample, encode_frame, TYPE_STATUS


class Source(Protocol):
    name: str

    def read(self, timeout: float = 0.1) -> bytes: ...
    def close(self) -> None: ...


class SimulatedSource:
    """正弦 + 噪声 + 可控丢帧/噪声字节，用来复现真实链路的异常."""

    name = "simulator"

    def __init__(
        self,
        hz: float = 50.0,
        noise_byte_rate: float = 0.02,
        drop_rate: float = 0.03,
        seed: int | None = None,
    ) -> None:
        self.period = 1.0 / hz
        self.noise_byte_rate = noise_byte_rate
        self.drop_rate = drop_rate
        self._rng = random.Random(seed)
        self._t0 = time.monotonic()
        self._seq = 0
        self._next_emit = self._t0

    def read(self, timeout: float = 0.1) -> bytes:
        out = bytearray()
        now = time.monotonic()
        # 到点就产帧，最多补生成极少量，避免 UI 卡顿时堆积
        budget = 20
        while now >= self._next_emit and budget > 0:
            budget -= 1
            self._next_emit += self.period
            payload = self._make_payload()
            if payload is not None:
                out += payload
            if self._rng.random() < self.noise_byte_rate:
                out += bytes(self._rng.randrange(256) for _ in range(self._rng.randint(1, 4)))
        sleep_for = max(0.0, min(timeout, self._next_emit - time.monotonic()))
        if sleep_for:
            time.sleep(sleep_for)
        return bytes(out)

    def _make_payload(self) -> bytes | None:
        t = time.monotonic() - self._t0
        if self._rng.random() < self.drop_rate:
            self._seq = (self._seq + 1) & 0xFFFF  # 故意跳过一帧：丢包可视化
            if self._rng.random() < 0.5:
                return None
            return None
        self._seq = (self._seq + 1) & 0xFFFF
        value = math.sin(2 * math.pi * 0.5 * t) * 100 + self._rng.gauss(0, 2)
        return encode_sample(self._seq, value)

    def close(self) -> None:  # pragma: no cover - 无资源可释放
        return None


class SerialSource:
    """真实串口；pyserial 是可选依赖，没有它也能跑模拟器."""

    def __init__(self, port: str, baud: int = 115200) -> None:
        try:
            import serial  # type: ignore
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise RuntimeError("需要 pyserial：pip install pyserial") from exc
        self._ser = serial.Serial(port=port, baudrate=baud, timeout=0.1)
        self.name = f"{port}@{baud}"

    def read(self, timeout: float = 0.1) -> bytes:
        self._ser.timeout = timeout
        return self._ser.read(256) or b""

    def close(self) -> None:
        self._ser.close()


class FileSource:
    """回放：把一个抓包文件按块喂给解码器，用于离线复现问题."""

    def __init__(self, path: str, chunk: int = 64) -> None:
        self._fh = open(path, "rb")
        self._chunk = chunk
        self.name = f"file:{path}"

    def read(self, timeout: float = 0.1) -> bytes:
        data = self._fh.read(self._chunk)
        if not data:
            time.sleep(0.01)
        return data

    def close(self) -> None:
        self._fh.close()


def status_frame(text: str) -> bytes:
    return encode_frame(TYPE_STATUS, 0, text.encode("utf-8"))
