"""用假的 bleak 把真实 BLE 开门逻辑跑通（核心路径不能只靠"没装 bleak"短路）。

覆盖：
  1. 有 mac → 直连不扫描；报文逐字段符合协议；订阅 0003 通知；电量按原 App 口径解析
  2. mac 失效 → 回落扫描，并把学到的新地址写回配置文件
  3. 锁休眠不广播 → 抛出可行动的错误信息
  4. 非长连接模式下连接必定关闭（不泄漏连接）；PIN 每次随机
  5. 后台监听真跑起来：开门时主动交还射频、开完自动恢复
     （combo 蓝牙+WiFi 网卡"边扫边连"经常直接失败，这条是秒开的前提）
"""
import asyncio
import json
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG_PATH = HERE / "_ble_test_config.json"

SECRET = "TESTSECRET"
SERVICE = "00006e40-1000-1000-8000-00805f9b34fb"
WCHAR = "00006e40-0002-1000-8000-00805f9b34fb"
NCHAR = "00006e40-0003-1000-8000-00805f9b34fb"
OLD_MAC = "AA:BB:CC:00:00:01"
NEW_MAC = "AA:BB:CC:00:00:02"
BAD_MAC = "11:22:33:44:55:66"

CFG_PATH.write_text(json.dumps({
    "bind": "127.0.0.1:8799",
    "tokens": ["ble-test-token"],
    "allow_auto": False,
    "bg_scan": False,
    "learn_mac": True,
    "cooldown_sec": 0,
    "scan_timeout_sec": 0.3,
    "direct_timeout_sec": 1,
    "cache_fresh_sec": 30,
    "default_lock": "楼-305",
    "locks": [{"label": "楼-305", "lock_no": "N/A", "mac": OLD_MAC, "secret": SECRET,
               "service": SERVICE, "characteristic": WCHAR, "notify_characteristic": NCHAR}],
}, ensure_ascii=False), encoding="utf-8")

os.environ["YUNMEI_CONFIG"] = str(CFG_PATH)
sys.path.insert(0, str(HERE))

import yunmei                      # noqa: E402
import yunmei_server as ys         # noqa: E402

# ------------------------------------------------------------------ 假 bleak

events: list[str] = []


class FakeAdv:
    def __init__(self, service_uuids, local_name="YM-lock"):
        self.service_uuids = service_uuids
        self.local_name = local_name


class FakeDev:
    def __init__(self, address):
        self.address = address
        self.name = "YM-lock"


class FakeClient:
    """只有 OLD_MAC / NEW_MAC 允许连上，用来模拟锁轮换随机地址。"""
    live: set[str] = set()

    def __init__(self, address, timeout=None):
        self.address = address
        self.is_connected = False

    async def connect(self):
        ok = self.address in (OLD_MAC, NEW_MAC)
        # 记录发起连接瞬间后台扫描是否仍占着射频（combo 网卡上会导致连接失败）
        events.append(f"scan_running_at_connect:{ys.SCAN_STATE['running']}")
        events.append(f"connect:{self.address}:{ok}")
        if not ok:
            raise OSError("cannot connect")
        self.is_connected = True
        FakeClient.live.add(self.address)

    async def disconnect(self):
        self.is_connected = False
        FakeClient.live.discard(self.address)
        events.append(f"disconnect:{self.address}")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *a):
        await self.disconnect()
        return False

    async def start_notify(self, uuid, cb):
        events.append(f"notify:{uuid}")
        self._cb = cb

    async def write_gatt_char(self, uuid, data, response=None):
        events.append(f"write:{uuid}:{bytes(data).hex()}")
        payload = bytes([0x00, 0xAB, 0x62, 0x00])   # AB 后跟 0x62 → "62" → ≈92%
        asyncio.get_running_loop().call_soon(self._cb, None, bytearray(payload))


class FakeScanner:
    """扫描时把"锁"报成 NEW_MAC；report_new=False 模拟锁休眠不广播。"""
    report_new = True
    enters = 0

    def __init__(self, detection_callback=None, **kw):
        self.cb = detection_callback

    async def __aenter__(self):
        FakeScanner.enters += 1
        if FakeScanner.report_new:
            self.cb(FakeDev(NEW_MAC), FakeAdv([SERVICE]))
        return self

    async def __aexit__(self, *a):
        return False


fake = types.ModuleType("bleak")
fake.BleakClient = FakeClient
fake.BleakScanner = FakeScanner
sys.modules["bleak"] = fake

ok = True


def check(name, cond, detail=""):
    global ok
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    ok = ok and bool(cond)


def reset():
    events.clear()
    ys.SCAN_STATE["running"] = False
    ys._seen.clear()
    FakeScanner.report_new = True
    FakeScanner.enters = 0


def pin_of(event: str) -> bytes:
    n = len(SECRET)
    return bytes.fromhex(event.split(":")[2])[3 + n:9 + n]


# ------------------------------------------------------- 1~4 协议与回退逻辑

async def run_protocol():
    lock = ys.LOCKS["楼-305"]
    n = len(SECRET)

    # 1) 有 mac：直连、不扫描
    reset()
    msg = await ys.ble_open(lock)
    writes = [e for e in events if e.startswith("write:")]

    check("有 mac 时走直连", f"connect:{OLD_MAC}:True" in events, events[:2])
    check("直连命中时不做整轮扫描（秒开关键）",
          FakeScanner.enters == 0, f"scan_enters={FakeScanner.enters}")
    check("发起连接时后台扫描未占射频", "scan_running_at_connect:False" in events)
    check("订阅了 0003 通知特征", f"notify:{NCHAR}" in events)

    pkt = bytes.fromhex(writes[0].split(":")[2]) if writes else b""
    check("写到 0002 写特征", bool(writes) and writes[0].startswith(f"write:{WCHAR}:"),
          writes[:1])
    check("报文逐字段符合协议 d0|len|secret|a5|6位PIN|ID01|a7",
          len(pkt) == n + 14              # len 字段值等于总长（都不含首两字节）
          and pkt[0] == 0xD0
          and pkt[1] == n + 14
          and pkt[2:2 + n] == SECRET.encode()
          and pkt[2 + n] == 0xA5
          and all(b <= 9 for b in pkt[3 + n:9 + n])
          and pkt[9 + n:13 + n] == b"ID01"
          and pkt[13 + n] == 0xA7,
          pkt.hex())
    check("报文含明文 lockSecret（静态密钥，务必保密）", SECRET.encode() in pkt)
    check("结果解析出电量且换算符合原 App", "电量≈92%" in msg, msg)
    check("成功后关闭连接", f"disconnect:{OLD_MAC}" in events)
    check("无残留连接", not FakeClient.live, str(FakeClient.live))

    # 2) mac 失效：回落扫描 + 学习新地址
    reset()
    lock["mac"] = BAD_MAC
    msg2 = await ys.ble_open(lock)
    check("坏 mac 回落扫描后仍成功", NEW_MAC in msg2, msg2)
    saved = json.loads(CFG_PATH.read_text(encoding="utf-8"))["locks"][0]["mac"]
    check("学到的新地址已写回配置", saved == NEW_MAC, saved)
    check("失败的连接也被清理", not FakeClient.live)

    # 3) 锁休眠不广播：清晰报错而不是崩
    reset()
    lock["mac"] = ""
    FakeScanner.report_new = False          # 必须在 reset 之后，reset 会把它复原
    try:
        await ys.ble_open(lock)
        check("扫不到时抛异常", False, "竟然没抛")
    except RuntimeError as e:
        check("扫不到时给出可行动的错误信息",
              "没扫到锁" in str(e) and "适配器" in str(e), str(e)[:90])
    FakeScanner.report_new = True

    # 4) PIN 每次随机（锁不校验它，但别让它变成固定值）
    pins = set()
    for _ in range(4):
        reset()
        lock["mac"] = OLD_MAC
        await ys.ble_open(lock)
        pins.add(pin_of([e for e in events if e.startswith("write:")][0]))
    check("PIN 字段每次随机", len(pins) >= 2, f"4 次里 {len(pins)} 种")

    # 5) 无 mac 但广播缓存新鲜 → 仍走直连
    reset()
    lock["mac"] = ""
    ys._seen["楼-305"] = (OLD_MAC, __import__("time").monotonic())
    await ys.ble_open(lock)
    check("mac 缺失但广播缓存新鲜时用缓存直连",
          f"connect:{OLD_MAC}:True" in events and FakeScanner.enters == 0,
          f"scan_enters={FakeScanner.enters}")


# ---------------------------------------------- 5) 后台监听 ↔ 开门 的射频让渡

async def run_monitor_handoff():
    reset()
    ys._busy = None
    lock = ys.LOCKS["楼-305"]
    lock["mac"] = OLD_MAC

    task = asyncio.create_task(ys._monitor())
    for _ in range(100):
        if ys.SCAN_STATE["running"]:
            break
        await asyncio.sleep(0.02)
    check("后台监听能起来", ys.SCAN_STATE["running"] is True)
    check("后台监听学到的地址进了缓存", "楼-305" in ys._seen, str(ys._seen))

    await asyncio.wait_for(ys.ble_open(lock), timeout=10)

    check("开门前监听从射频上退场",
          "scan_running_at_connect:False" in events,
          next((e for e in events if e.startswith("scan_running")), "无记录"))
    check("开门期间射频未被抢回", ys.SCAN_STATE["running"] is False)

    for _ in range(100):
        if ys.SCAN_STATE["running"]:
            break
        await asyncio.sleep(0.02)
    check("开门结束后自动恢复监听", ys.SCAN_STATE["running"] is True)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def main():
    await run_protocol()
    await run_monitor_handoff()


try:
    asyncio.run(main())
finally:
    CFG_PATH.unlink(missing_ok=True)
    print("\n" + ("BLE 核心逻辑全部通过" if ok else "存在失败项"))

sys.exit(0 if ok else 1)
