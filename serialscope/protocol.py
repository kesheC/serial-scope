"""帧协议：AA 55 | ver | type | seq | len | payload | crc16(MODBUS).

设计要点
--------
1. 固定帧头 ``0xAA 0x55``，后面跟版本号，方便以后扩展字段而不破坏旧解析器。
2. ``seq`` 单调自增（uint16，回绕），上位机据此计算"丢帧数"——比时间戳可靠，
   因为时间戳依赖两端时钟，而序列号只依赖发送端计数。
3. 长度字段存在，解析器才能跳过不认识的新 payload，做前向兼容。
4. CRC16/MODBUS 覆盖 ver..payload，帧头不参与，避免帧头噪声污染校验。
5. 解析器是**流式**的：允许任意切分输入（串口一次 read 可能只到半帧），
   遇到噪声/半帧/CRC 错都能重新同步到下一个帧头。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterable

MAGIC = b"\xAA\x55"
VERSION = 0x01

# 帧类型
TYPE_SAMPLE = 0x01  # payload: float32 LE，单通道采样值
TYPE_STATUS = 0x02  # payload: utf-8 文本，设备状态

HEADER_LEN = 2 + 1 + 1 + 2 + 2  # magic + ver + type + seq + len
CRC_LEN = 2


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS：poly=0xA001, init=0xFFFF，查表法以外的标准位运算实现."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


@dataclass(frozen=True)
class Frame:
    type: int
    seq: int
    payload: bytes
    raw: bytes = field(repr=False, default=b"")

    @property
    def sample(self) -> float:
        if self.type != TYPE_SAMPLE or len(self.payload) != 4:
            raise ValueError("frame is not a sample frame")
        return struct.unpack("<f", self.payload)[0]

    @property
    def text(self) -> str:
        if self.type != TYPE_STATUS:
            raise ValueError("frame is not a status frame")
        return self.payload.decode("utf-8", "replace")


def encode_frame(ftype: int, seq: int, payload: bytes) -> bytes:
    if not 0 <= seq <= 0xFFFF:
        raise ValueError("seq out of uint16 range")
    body = struct.pack(">BBHH", VERSION, ftype, seq, len(payload)) + payload
    return MAGIC + body + struct.pack(">H", crc16_modbus(body))


def encode_sample(seq: int, value: float) -> bytes:
    return encode_frame(TYPE_SAMPLE, seq, struct.pack("<f", value))


@dataclass
class DecodeStats:
    """解析过程中的可观测指标，UI 直接展示，测试也断言这些值."""

    frames: int = 0
    crc_errors: int = 0
    resyncs: int = 0  # 因帧头错位而丢弃的字节段数量
    bad_lengths: int = 0
    bytes_in: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "frames": self.frames,
            "crc_errors": self.crc_errors,
            "resyncs": self.resyncs,
            "bad_lengths": self.bad_lengths,
            "bytes_in": self.bytes_in,
        }


class FrameDecoder:
    """流式解码器：feed() 任意长度字节，返回本次能解出的完整帧."""

    MAX_PAYLOAD = 4096  # 防御性上限：异常长度字段不足以让我们无限等下去

    def __init__(self) -> None:
        self._buf = bytearray()
        self.stats = DecodeStats()

    def feed(self, chunk: bytes) -> list[Frame]:
        self.stats.bytes_in += len(chunk)
        self._buf.extend(chunk)
        out: list[Frame] = []

        while True:
            result = self._try_pop()
            if result is None:
                break  # 需要更多数据
            if result is False:
                continue  # 丢弃了坏字节，已有进展，继续重试
            out.append(result)
        return out

    # ---- 内部实现 ----

    def _try_pop(self) -> Frame | None | bool:
        """返回 Frame=解出一帧；None=数据不够等下一块；False=丢弃了坏字节需重试."""
        buf = self._buf
        idx = buf.find(MAGIC)
        if idx < 0:
            # 没有帧头：保留最后 1 字节（可能是半个 magic），其余丢弃
            if len(buf) > 1:
                self.stats.resyncs += 1
                del buf[: len(buf) - 1]
            return None
        if idx > 0:
            self.stats.resyncs += 1
            del buf[:idx]
        if len(buf) < HEADER_LEN:
            return None

        _ver, ftype, seq, length = struct.unpack(">BBHH", bytes(buf[2:HEADER_LEN]))
        if length > self.MAX_PAYLOAD:
            self.stats.bad_lengths += 1
            del buf[:2]  # 丢掉这个假帧头，从下一字节继续找
            return False

        total = HEADER_LEN + length + CRC_LEN
        if len(buf) < total:
            return None  # 半帧，等更多数据

        crc_body = bytes(buf[2 : HEADER_LEN + length])
        payload = bytes(buf[HEADER_LEN : HEADER_LEN + length])
        (crc_recv,) = struct.unpack(">H", bytes(buf[HEADER_LEN + length : total]))
        raw = bytes(buf[:total])
        if crc16_modbus(crc_body) != crc_recv:
            self.stats.crc_errors += 1
            del buf[:2]  # 校验失败，跳过帧头重新同步
            return False

        del buf[:total]
        self.stats.frames += 1
        return Frame(type=ftype, seq=seq, payload=payload, raw=raw)


@dataclass
class LossStats:
    """丢帧统计：只在 seq 不连续时累加，支持 uint16 回绕."""

    expected: int | None = None
    received: int = 0
    lost: int = 0
    last_seq: int | None = None

    def observe(self, seq: int) -> None:
        if self.expected is not None:
            gap = (seq - self.expected) & 0xFFFF
            if gap:
                self.lost += gap
        self.expected = (seq + 1) & 0xFFFF
        self.received += 1
        self.last_seq = seq

    @property
    def loss_rate(self) -> float:
        total = self.received + self.lost
        return self.lost / total if total else 0.0


def iter_frames(decoder: FrameDecoder, chunks: Iterable[bytes]) -> Iterable[Frame]:
    for chunk in chunks:
        yield from decoder.feed(chunk)
