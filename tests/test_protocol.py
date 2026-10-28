"""协议层单元测试：编码/解码、半帧、噪声、CRC 错、丢帧统计."""

from __future__ import annotations

import struct
import unittest

from serialscope.protocol import (
    FrameDecoder,
    LossStats,
    TYPE_SAMPLE,
    TYPE_STATUS,
    crc16_modbus,
    encode_frame,
    encode_sample,
)


class TestCrc(unittest.TestCase):
    def test_known_value(self) -> None:
        # CRC-16/MODBUS 标准测试向量："123456789" → 0x4B37
        self.assertEqual(crc16_modbus(b"123456789"), 0x4B37)

    def test_empty(self) -> None:
        self.assertEqual(crc16_modbus(b""), 0xFFFF)


class TestRoundTrip(unittest.TestCase):
    def test_sample_round_trip(self) -> None:
        dec = FrameDecoder()
        frames = dec.feed(encode_sample(42, 3.5))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 42)
        self.assertEqual(frames[0].type, TYPE_SAMPLE)
        self.assertAlmostEqual(frames[0].sample, 3.5, places=6)

    def test_text_round_trip(self) -> None:
        dec = FrameDecoder()
        frames = dec.feed(encode_frame(TYPE_STATUS, 0, "设备就绪".encode()))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].text, "设备就绪")

    def test_many_in_one_chunk(self) -> None:
        dec = FrameDecoder()
        blob = b"".join(encode_sample(i, float(i)) for i in range(10))
        self.assertEqual(len(dec.feed(blob)), 10)
        self.assertEqual(dec.stats.frames, 10)


class TestStreaming(unittest.TestCase):
    def test_byte_by_byte(self) -> None:
        """串口可能一字节一字节地吐，解码器必须能拼起来."""
        dec = FrameDecoder()
        raw = encode_sample(1, 1.25)
        got = []
        for b in raw:
            got.extend(dec.feed(bytes([b])))
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0].sample, 1.25, places=6)

    def test_half_frame_waits(self) -> None:
        dec = FrameDecoder()
        raw = encode_sample(2, 2.0)
        self.assertEqual(dec.feed(raw[:5]), [])
        self.assertEqual(len(dec.feed(raw[5:])), 1)

    def test_noise_before_magic(self) -> None:
        dec = FrameDecoder()
        frames = dec.feed(b"\x00\x11\x22" + encode_sample(3, 3.0))
        self.assertEqual(len(frames), 1)
        self.assertGreaterEqual(dec.stats.resyncs, 1)

    def test_garbage_between_frames(self) -> None:
        dec = FrameDecoder()
        blob = encode_sample(1, 1.0) + b"\xff\x00\xaa" + encode_sample(2, 2.0)
        self.assertEqual(len(dec.feed(blob)), 2)


class TestErrors(unittest.TestCase):
    def test_crc_error_detected_and_recovers(self) -> None:
        dec = FrameDecoder()
        bad = bytearray(encode_sample(1, 1.0))
        bad[-1] ^= 0xFF  # 破坏 CRC
        frames = dec.feed(bytes(bad) + encode_sample(2, 2.0))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 2)
        self.assertGreaterEqual(dec.stats.crc_errors, 1)

    def test_absurd_length_rejected(self) -> None:
        dec = FrameDecoder()
        evil = b"\xAA\x55\x01\x01\x00\x01\xFF\xFF" + b"\x00" * 8
        dec.feed(evil + encode_sample(5, 5.0))
        self.assertGreaterEqual(dec.stats.bad_lengths, 1)
        self.assertEqual(dec.stats.frames, 1)

    def test_truncated_stream_does_not_raise(self) -> None:
        dec = FrameDecoder()
        raw = encode_sample(9, 9.0)
        dec.feed(raw[:-1])  # 少最后一字节
        dec.feed(b"")  # 空块
        self.assertEqual(dec.stats.frames, 0)


class TestLossStats(unittest.TestCase):
    def test_contiguous(self) -> None:
        s = LossStats()
        for seq in (1, 2, 3):
            s.observe(seq)
        self.assertEqual(s.lost, 0)
        self.assertEqual(s.received, 3)
        self.assertAlmostEqual(s.loss_rate, 0.0)

    def test_gap_counted(self) -> None:
        s = LossStats()
        s.observe(1)
        s.observe(5)  # 2,3,4 丢了
        self.assertEqual(s.lost, 3)
        self.assertAlmostEqual(s.loss_rate, 3 / 5)

    def test_wraparound(self) -> None:
        s = LossStats()
        s.observe(0xFFFE)
        s.observe(0xFFFF)
        s.observe(0x0000)
        self.assertEqual(s.lost, 0)

    def test_frame_header_size(self) -> None:
        """帧头长度写死会影响对端解析，这里钉住协议常量."""
        raw = encode_frame(TYPE_SAMPLE, 0, struct.pack("<f", 0.0))
        # magic(2) + ver(1) + type(1) + seq(2) + len(2) + payload(4) + crc(2)
        self.assertEqual(len(raw), 8 + 4 + 2)


if __name__ == "__main__":
    unittest.main()
