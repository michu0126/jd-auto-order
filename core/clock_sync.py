"""SNTP 校准应用时钟，不修改 Windows 系统时间。"""
import asyncio
import logging
import socket
import struct
import time
from dataclasses import dataclass

LOG = logging.getLogger(__name__)
NTP_EPOCH = 2208988800


@dataclass
class Sample:
    server: str
    offset_ns: int
    delay_ms: float


def timestamp(value):
    seconds, fraction = struct.unpack("!II", value)
    return seconds - NTP_EPOCH + fraction / 2**32


def query(server, timeout=2):
    packet = bytearray(48)
    packet[0] = 0x23
    t1 = time.time()
    seconds = t1 + NTP_EPOCH
    stamp = struct.pack("!II", int(seconds), int((seconds % 1) * 2**32))
    packet[40:48] = stamp
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((server, 123))
        sock.send(packet)
        response = sock.recv(512)
        t4 = time.time()
    if len(response) < 48 or response[24:32] != stamp:
        raise ValueError("NTP 回包长度或请求时间戳不符")
    if response[0] >> 6 == 3 or response[0] & 7 != 4 or not 1 <= response[1] <= 15:
        raise ValueError("NTP 服务器状态不可用")
    t2, t3 = timestamp(response[32:40]), timestamp(response[40:48])
    offset = ((t2 - t1) + (t3 - t4)) / 2
    delay = (t4 - t1) - (t3 - t2)
    if not 0 <= delay < 2 or abs(offset) > 86400 or t3 < t2:
        raise ValueError("NTP 偏差或延迟异常")
    return Sample(server, round(offset * 1e9), delay * 1000)


async def calibrate(servers):
    results = await asyncio.gather(*(asyncio.to_thread(query, host) for host in servers), return_exceptions=True)
    good = [r for r in results if isinstance(r, Sample)]
    if not good:
        LOG.warning("时间源均不可达，使用电脑时间；未声称完成网络校准")
        return 0, "local-unverified"
    best = min(good, key=lambda sample: sample.delay_ms)
    LOG.info("时间校准：%s，偏差 %+.3f ms，往返延迟 %.2f ms", best.server, best.offset_ns / 1e6, best.delay_ms)
    return best.offset_ns, best.server
