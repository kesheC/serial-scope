# serial-scope · 串口数据采集上位机

[![CI](https://github.com/kesheC/serial-scope/actions/workflows/ci.yml/badge.svg)](https://github.com/kesheC/serial-scope/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-23%20passed-brightgreen)](https://github.com/kesheC/serial-scope/tree/main/tests)
[![python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://github.com/kesheC/serial-scope)

CI：`.github/workflows/ci.yml`（Python 3.10–3.12 × Ubuntu/Windows，跑单测 + 无界面冒烟）。

一个**带协议层**的串口数据采集与实时显示上位机：下位机按自定义帧格式吐采样，上位机负责流式解码、CRC 校验、丢帧统计、实时曲线与 CSV 录制。纯 Python 标准库实现（串口可选 `pyserial`），没有显示器或没有硬件时用内置模拟器跑通全链路。

![运行截图](docs/preview.png)

*上图是回放 `samples/demo.bin` 的实际界面：右下角统计行显示 586 帧、2 次重同步、12 帧丢失（2.0%），曲线是解码后的采样值。*


```
                    ┌──────────────── serial-scope ─────────────────┐
 下位机 / 模拟器       │                                              │
 ├ AA 55 01 01 ... ──▶│ Source.read() ─▶ FrameDecoder.feed()         │
 │  (帧流, 可任意切分) │                    │ 半帧等待 / 噪声重同步      │
 └───────────────────┘                    │ CRC16 校验 / 异常长度拒绝   │
                                          ▼                          │
                                   LossStats(seq)  ──▶ 丢帧率          │
                                          │                          │
                                   SampleWindow(环形缓冲) ──▶ 曲线      │
                                          │                          │
                                   queue ─▶ Tk 渲染线程               │
                                          └──▶ Recorder → CSV         │
                    └──────────────────────────────────────────────┘
```

## 帧协议

| 字段 | 长度 | 说明 |
| --- | --- | --- |
| `magic` | 2 B | 固定 `0xAA 0x55`，用于在噪声流中定位帧起点 |
| `ver` | 1 B | 协议版本，当前 `0x01`，为后续加字段留余地 |
| `type` | 1 B | `0x01` 采样帧（payload = float32 LE）、`0x02` 状态帧（utf-8 文本） |
| `seq` | 2 B | 单调自增（uint16，回绕），**丢帧统计的依据** |
| `len` | 2 B | payload 长度（上限 4096，防异常长度把解析器卡死） |
| `payload` | len B | 载荷 |
| `crc16` | 2 B | CRC-16/MODBUS，覆盖 `ver..payload`（帧头不参与） |

## 快速开始

```bash
# 1. 没有硬件：用模拟器（自带噪声字节与 5% 丢帧，用来验证统计链路）
python -m serialscope --demo

# 2. 无界面快速验证（CI / 无显示器 / 排查链路）
python -m serialscope --demo --headless --seconds 3
# simulator 3s → 帧=147 CRC错=0 重同步=0 丢帧=7 (4.5%) 最新=13.60 均值=1.91

# 3. 接真实串口（需要 pip install pyserial）
python -m serialscope --port COM3 --baud 115200

# 4. 回放抓包文件
python -m serialscope --file dump.bin

# 5. 测试
python -m unittest discover -s tests -t .
```

界面：左侧实时曲线（环形缓冲最近 600 点），底部一行统计 `帧 / CRC错 / 重同步 / 丢帧(率) / 当前值 / 均值`，右侧「开始/停止」与「录制 CSV」。记录的 CSV 是 `timestamp_ms,seq,value`，可以直接丢进 pandas 或 Excel 二次分析。

## 几个设计取舍

**1. 为什么用 seq 而不是时间戳算丢帧？**
两端时钟不同源，串口又没有全局时间。`seq` 只依赖发送端计数，差一个就是丢一帧；差值用 `(seq - expected) & 0xFFFF` 计算，天然支持 uint16 回绕。

**2. 为什么解析器要做成流式的？**
一次 `read()` 可能只读到半帧，也可能一次读到十帧。解码器内部维护 `bytearray` 缓冲：不够一帧就等下一块；帧头错位、CRC 失败、长度字段异常时**丢 2 字节重找帧头**而不是清空缓冲——清空会把紧随其后的正常帧一起丢掉。`feed()` 用 `Frame / None / False` 三态区分「解出帧 / 等数据 / 丢坏字节需重试」，否则一次输入里的后续好帧会被静默吞掉（这个 bug 就是被 `test_crc_error_detected_and_recovers` 抓出来的）。

**3. 为什么 CRC 不覆盖帧头？**
帧头只用来定位起点，一旦错位解析器会自己重同步，没必要也不应该让一个噪声字节污染整帧校验值；覆盖 `ver..payload` 足以发现载荷与长度字段的错误。

**4. 为什么读线程只做解析、UI 只做渲染？**
串口读取是阻塞 I/O，放进主线程会让界面卡死；放进 UI 线程用 `after()` 轮询又会被长帧阻塞。这里读线程把解出的帧塞进 `queue`，UI 每 40 ms `drain()` 一次，**队列满时丢的是显示帧，统计数据仍然完整**——显示可以掉帧，统计不能。

**5. 为什么长度字段要有上限？**
噪声里可能撞出 `AA 55` 假帧头，后面跟一个巨大的长度值，解析器会一直等 65535 字节，表现为「卡死但没报错」。`MAX_PAYLOAD=4096` 直接拒绝并把该情况计入 `bad_lengths`，让故障可见。

**6. 为什么用 Tk 而不是 Qt / Web？**
目标是「拷走就能跑」的调试工具：Tk 随 Python 自带，零依赖、零安装，工控机上放个 exe 甚至源码就能用。牺牲的是观感，换来的是部署成本。

## 测试

23 个用例，覆盖三层：

- **协议层**：CRC 标准向量（`"123456789" → 0x4B37`）、编解码往返、一字节一字节喂入、半帧等待、噪声/垃圾字节重同步、CRC 错后恢复、异常长度拒绝、截断输入不抛异常、丢帧统计与回绕。
- **链路层**：脚本化数据源驱动 `Capture`，验证帧计数、环形窗口容量、状态帧不污染采样窗口。
- **IO 层**：`SampleWindow` 容量与统计、`Recorder` 的 CSV 表头与行格式、`FileSource` 回放完整性。

```bash
python -m unittest discover -s tests -t .   # Ran 23 tests ... OK
```

CI（GitHub Actions）在 Python 3.10/3.11/3.12 上跑同样的命令，并额外跑一次 `--demo --headless` 冒烟。

## 已知边界

- 单通道：`TYPE_SAMPLE` 只有一个 float32。多通道需要改 payload 布局并同步改 `Frame.sample`。
- 不做波特率自适应：速率不匹配表现为持续 `resyncs` 上涨，需要人工确认。
- 界面用 Tk 绘制，点数超过约 5000 时重绘会有可感知延迟；当前环形缓冲限制在 600 点。
- CSV 录制不做断点续写与文件轮转，长时间采集需自行控制。

