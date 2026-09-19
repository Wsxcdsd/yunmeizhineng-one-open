#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云莓智能宿舍锁 · 局域网一键开门服务（部署在有蓝牙+WiFi 网卡的 Ubuntu 小主机上）

手机只发一个局域网 HTTP 请求，蓝牙由小主机执行。因此：
  · 手机不需要蓝牙、不需要装 App、不需要 Tailscale/内网穿透/公网域名
  · 手机连上寝室 Wi-Fi（或路由器局域网）即可，离线公网也能开

组成：
  GET  /               只有一个大按钮的页面（浏览器菜单"添加到主屏幕"= 桌面开门图标）
  GET  /manifest.webmanifest  PWA 清单（把 ?t= 固化进安装后的启动地址）
  GET  /icon.png       桌面图标（纯标准库内存生成，无需外部素材）
  POST /api/open       触发开门
  GET  /api/status     健康检查 / 最近一次结果

为什么能"秒开"：
  后台常驻 BLE 广播监听 + 记住门锁地址（首次成功后写回配置），
  把"每次重新扫描 2~10 秒"压成"直接连接 1~2 秒"。

部署（只做新增文件，不动 AstrBot/NapCat 的任何配置）：
  mkdir -p ~/yunmei && 把 yunmei.py / yunmei_server.py 放进去
  cd ~/yunmei && python3 -m venv venv && ./venv/bin/pip install bleak
  ./venv/bin/python yunmei.py 学号 密码 --dump-config yunmei_config.json
  ./venv/bin/python yunmei_server.py --print-urls     # 前台跑一次，打印手机该打开哪个地址
  # 手机浏览器打开 http://<小主机局域网IP>:8791/?t=<token> → 菜单 → 添加到主屏幕
  # 满意后：sudo cp yunmei.service /etc/systemd/system/ && sudo systemctl enable --now yunmei
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yunmei  # noqa: E402

CONFIG_PATH = os.environ.get("YUNMEI_CONFIG", str(Path(__file__).with_name("yunmei_config.json")))
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
# token 要嵌进 <script> 的字符串字面量，所以只允许无需转义的字符（见 _safe_token）
TOKEN_SAFE = re.compile(r"^[A-Za-z0-9_-]{6,80}$")

# ---------------------------------------------------------------- 配置


def load_config(strict: bool = True) -> dict:
    """运行时只需要 locks + tokens：BLE 开门完全离线，不需要云账号。

    strict=True（服务模式）时缺文件/缺字段直接快速失败；
    strict=False（--scan 诊断模式）时允许没有配置，因为扫蓝牙不需要锁参数。
    """
    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not strict:
            return {}
        raise SystemExit(
            f"找不到配置文件 {CONFIG_PATH}\n"
            f"先生成它：python yunmei.py <学号> <密码> --dump-config {CONFIG_PATH}\n"
            f"（字段说明见 CONFIG.example.json）"
        )
    except json.JSONDecodeError as e:
        raise SystemExit(f"配置文件 {CONFIG_PATH} 不是合法 JSON：{e}")
    return cfg


def validate_for_serve() -> None:
    """服务启动前的配置校验：问题要在这里炸掉，而不是等手机点按钮时才炸。"""
    if not CONFIG_PRESENT:
        raise SystemExit(
            f"找不到配置文件 {CONFIG_PATH}\n"
            f"先生成它：python yunmei.py <学号> <密码> --dump-config {CONFIG_PATH}\n"
            f"（字段说明见 CONFIG.example.json；"
            f"想先确认蓝牙能不能喊到锁：python yunmei_server.py --scan）"
        )
    if not CFG.get("tokens"):
        raise SystemExit(f"配置缺少 tokens（{CONFIG_PATH}）")
    bad = [t for t in CFG["tokens"] if not TOKEN_SAFE.match(str(t))]
    if bad:
        raise SystemExit(
            "tokens 只能用字母/数字/下划线/短横线，长度 6-80（要嵌进网页脚本里用）。\n"
            f"不合规的有：{bad}\n"
            "图省事就直接重跑 --dump-config，它生成的 token 一定合规。")
    if not CFG.get("locks"):
        raise SystemExit(f"配置缺少 locks（{CONFIG_PATH}）；"
                         f"先跑 python yunmei.py <账号> <密码> --dump-config {CONFIG_PATH}")
    for l in CFG["locks"]:
        missing = [k for k in ("label", "secret", "service", "characteristic") if not l.get(k)]
        if missing:
            raise SystemExit(f"锁 {l.get('label', '?')} 缺少字段 {missing}，"
                             f"请重跑 --dump-config 刷新")


CFG = load_config(strict=False)
CONFIG_PRESENT = Path(CONFIG_PATH).exists()
LOCKS: dict[str, dict] = {l["label"]: l for l in CFG.get("locks", [])}
DEFAULT_LOCK = CFG.get("default_lock") or next(iter(LOCKS), None)
COOLDOWN = float(CFG.get("cooldown_sec", 5))
SCAN_TIMEOUT = float(CFG.get("scan_timeout_sec", 10))
DIRECT_TIMEOUT = float(CFG.get("direct_timeout_sec", 6))
CACHE_FRESH = float(CFG.get("cache_fresh_sec", 30))
BG_SCAN = bool(CFG.get("bg_scan", True))
KEEP_CONNECTED = bool(CFG.get("keep_connected", False))
LEARN_MAC = bool(CFG.get("learn_mac", True))
ALLOW_AUTO = bool(CFG.get("allow_auto", True))
BIND_ADDR = CFG.get("bind", "0.0.0.0:8791")

_state_lock = threading.Lock()
_last_open = 0.0
_last_result = "尚未执行"
loop: asyncio.AbstractEventLoop | None = None

_seen: dict[str, tuple[str, float]] = {}     # label -> (BLE 地址, 最近看到的时刻)
_conns: dict[str, object] = {}               # label -> BleakClient（keep_connected 时）
_busy: asyncio.Event | None = None           # 置位=正在开门，后台扫描要让出射频
SCAN_STATE = {"running": False}              # 仅供 /api/status 观察后台监听是否在跑

# ---------------------------------------------------------------- BLE


def _save_learned(label: str, addr: str) -> None:
    """把成功连上的地址写回配置文件（对应原名 App 的"快速连接"）。"""
    if not LEARN_MAC:
        return
    lock = LOCKS[label]
    if lock.get("mac") == addr:
        return
    lock["mac"] = addr
    try:
        tmp = Path(CONFIG_PATH).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(CFG, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        os.chmod(CONFIG_PATH, 0o600)
        print(f"[info] 记住 {label} 的地址 {addr}，下次直连", flush=True)
    except OSError as e:
        print(f"[warn] 无法写回配置：{e}", flush=True)


def _candidates(lock: dict) -> list[str]:
    """给出按可信度排序的候选 BLE 地址：配置的 mac → 学到的地址 → 近期广播里看到的。"""
    out: list[str] = []
    for a in (lock.get("mac"), lock.get("learned_mac"),
              (lock.get("lock_no") if MAC_RE.match(lock.get("lock_no") or "") else None)):
        if a and a not in out:
            out.append(a)
    label = lock["label"]
    if label in _seen and time.monotonic() - _seen[label][1] < CACHE_FRESH:
        a = _seen[label][0]
        if a not in out:
            out.append(a)
    return out


def _match(lock: dict):
    """广播过滤器：按服务 UUID（可选再按蓝牙名）判断是不是这把锁。"""
    service = lock["service"].lower()
    names = [s.lower() for s in (CFG.get("only_if_name_contains") or [])]

    def m(dev, adv) -> bool:
        if service not in [u.lower() for u in (adv.service_uuids or [])]:
            return False
        if names:
            blob = ((dev.name or "") + " " + (adv.local_name or "")).lower()
            if not any(s in blob for s in names):
                return False
        return True

    return m


def _get_busy() -> asyncio.Event:
    """_busy 必须在事件循环所在线程创建，所以惰性初始化。"""
    global _busy
    if _busy is None:
        _busy = asyncio.Event()
    return _busy


async def _monitor() -> None:
    """后台常驻扫描：只为刷新 _seen（锁在哪、地址是什么），不建连接、不发包。

    重要：开门期间必须**完全释放**射频。WiFi+蓝牙 combo 网卡（就是这种
    "蓝牙wifi网卡"）在"正在扫描"时发起连接经常失败，所以这里一看到 _busy
    就退出 `async with BleakScanner`，把适配器交还给开门流程。
    """
    from bleak import BleakScanner

    matchers = {lb: _match(lk) for lb, lk in LOCKS.items()}

    def adv_cb(dev, adv):
        now = time.monotonic()
        for label, m in matchers.items():
            if m(dev, adv):
                _seen[label] = (dev.address, now)

    while True:
        if _busy is not None and _busy.is_set():
            await asyncio.sleep(0.1)
            continue
        try:
            async with BleakScanner(detection_callback=adv_cb):
                SCAN_STATE["running"] = True
                while _busy is None or not _busy.is_set():
                    await asyncio.sleep(0.1)
                SCAN_STATE["running"] = False
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  适配器重启等意外，歇一下再来
            SCAN_STATE["running"] = False
            print(f"[warn] 后台监听中断：{e}", flush=True)
            await asyncio.sleep(5)


async def _scan_once(lock: dict) -> str | None:
    from bleak import BleakScanner

    m = _match(lock)
    found: list = []

    def adv_cb(dev, adv):
        if m(dev, adv):
            found.append(dev)

    async with BleakScanner(detection_callback=adv_cb):
        await asyncio.sleep(SCAN_TIMEOUT)
    return found[0].address if found else None


def _parse_battery(raw: bytes) -> str:
    """与原 App 一致：定位符后的字节转 hex 字符串，再按十进制解释。"""
    def hexdec(pos: int, n: int):
        if pos < 0 or pos + n > len(raw):
            return None
        try:
            return int(raw[pos:pos + n].hex().upper(), 10)
        except ValueError:
            return None

    aa = ab = None
    for i, b in enumerate(raw):
        if b == 0xAA:
            aa = hexdec(i + 1, 2)
        elif b == 0xAB:
            ab = hexdec(i + 1, 1)
    if ab is not None:
        return f"电量≈{round(100.0 * (ab - 40) / 24)}%"
    if aa is not None:
        return f"电量码={aa}"
    return f"锁回报 {raw.hex(' ')}"


async def _do_open(client, lock: dict) -> str:
    """订阅通知 → 写开锁报文 → 等锁回包。"""
    wchar = lock["characteristic"].lower()
    nchar = (lock.get("notify_characteristic")
             or yunmei.notify_char(lock["characteristic"])).lower()
    packet = yunmei.build_unlock_packet(lock["secret"])

    got: asyncio.Queue = asyncio.Queue()

    def on_notify(_, data: bytearray):
        got.put_nowait(bytes(data))

    try:  # 通知特征用来拿"开门成功 + 电量"，个别锁没有就跳过
        await client.start_notify(nchar, on_notify)
    except Exception:
        pass
    await client.write_gatt_char(wchar, packet, response=True)
    try:
        raw = await asyncio.wait_for(got.get(), timeout=6)
    except asyncio.TimeoutError:
        return "已下发开锁指令（锁未回报，通常也已生效）"
    return "开门成功 " + _parse_battery(raw)


async def _get_conn(label: str, addr: str):
    """keep_connected=True 时复用长连接（开门可压到 <0.5s）；断开自动重建。"""
    from bleak import BleakClient

    c = _conns.get(label)
    if c is not None and getattr(c, "is_connected", False):
        return c
    if c is not None:
        try:
            await c.disconnect()
        except Exception:
            pass
    c = BleakClient(addr, timeout=DIRECT_TIMEOUT)
    await c.connect()
    _conns[label] = c
    return c


async def ble_open(lock: dict) -> str:
    """开门总入口：先让后台扫描交还射频，再执行。

    combo 网卡（蓝牙+WiFi 二合一，正是"蓝牙wifi网卡"）在持续扫描状态下
    发起 BLE 连接很容易失败，所以这里必须等后台监听真的停下来。
    """
    busy = _get_busy()
    was_idle = not busy.is_set()
    busy.set()
    try:
        if was_idle and SCAN_STATE["running"]:
            # 等 monitor 退出 BleakScanner；最多 1s，之后照样继续
            for _ in range(20):
                if not SCAN_STATE["running"]:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.1)   # 给适配器一点 settle 时间
        return await _ble_open_inner(lock)
    finally:
        busy.clear()


async def _ble_open_inner(lock: dict) -> str:
    """依次尝试：已知地址直连 → 近期广播缓存 → 完整扫描。"""
    from bleak import BleakClient

    label = lock["label"]
    errs: list[str] = []

    for addr in _candidates(lock):
        client = None
        keep = False
        try:
            if KEEP_CONNECTED:
                client = await _get_conn(label, addr)
                keep = True
            else:
                client = BleakClient(addr, timeout=DIRECT_TIMEOUT)
                await client.connect()
            msg = await _do_open(client, lock)
            _save_learned(label, addr)
            return msg if keep else f"{msg}（直连 {addr}）"
        except Exception as e:  # noqa: BLE001  地址轮换很常见，逐个试
            errs.append(f"{addr}:{e}")
            _conns.pop(label, None)
        finally:
            # 长连接模式保留连接；否则无论成功失败都要断开，别攒一堆僵尸连接
            if not keep and client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    addr = await _scan_once(lock)
    if addr is None:
        hint = "；直连尝试：" + "; ".join(errs) if errs else ""
        raise RuntimeError(
            f"{SCAN_TIMEOUT:g}s 内没扫到锁（锁在广播休眠？蓝牙适配器正常？"
            f"only_if_name_contains 配错？）{hint}")
    async with BleakClient(addr, timeout=15) as c:
        msg = await _do_open(c, lock)
    _save_learned(label, addr)
    return f"{msg}（扫描命中 {addr}）"


def run_ble_open(lock: dict) -> str:
    future = asyncio.run_coroutine_threadsafe(ble_open(lock), loop)
    return future.result(timeout=SCAN_TIMEOUT + DIRECT_TIMEOUT + 40)


# ------------------------------------------------------- 兜底：云端动态开门密码

_acc_lock = threading.Lock()


def cloud_password(lock: dict) -> str:
    """蓝牙不灵时的第二条路：向云端要一个数字开门密码，在锁键盘上输入即可。

    需要配置里的 _account（用户名 + 口令 MD5）。这条完全不碰蓝牙，
    所以网卡蓝牙被 WiFi 抢占、或锁暂时不在范围时依然可用。
    """
    acc = CFG.get("_account") or {}
    if not acc.get("username") or not acc.get("password_md5"):
        raise RuntimeError("配置缺少 _account，无法取云端密码（重跑一次 --dump-config）")
    with _acc_lock:                     # 云 API 很轻量，但别并发重复登录
        c = yunmei.YunmeiClient()
        c.login(acc["username"], acc["password_md5"], password_is_md5=True)
        for s in c.schools():
            if s["schoolNo"] == acc.get("school_no"):
                c.use_school(s)
                break
        else:
            raise RuntimeError("配置里的 school_no 已失效")
        return c.dynamic_password(lock["lock_no"])

# ---------------------------------------------------------------- 图标 / PWA


def make_icon(size: int = 512) -> bytes:
    """内存生成桌面图标（纯标准库）：深色圆角方 + 渐变圆钮 + 白色钥匙孔。"""
    cx = cy = (size - 1) / 2
    r_out = size * 0.40
    kb_cy, kb_r = cy - size * 0.10, size * 0.075
    kb_top, kb_bot, kb_wt, kb_wb = cy + size * 0.00, cy + size * 0.20, size * 0.035, size * 0.062
    corner = size * 0.22

    px = bytearray()
    for y in range(size):
        for x in range(size):
            # 圆角方遮罩
            dx = max(corner - x, x - (size - 1 - corner), 0)
            dy = max(corner - y, y - (size - 1 - corner), 0)
            if dx * dx + dy * dy > corner * corner:
                px += b"\x00\x00\x00\x00"
                continue
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d <= r_out:  # 圆钮：中心亮、边缘暗
                t = min(1.0, d / r_out)
                v = int(58 * (1 - t) + 22 * t)
                col = (v, v, v)
                if d > r_out - max(1.0, size * 0.006):
                    col = (90, 90, 90)
            else:
                col = (17, 17, 17)
            # 钥匙孔：圆头 + 下宽的柱
            in_knob = (x - cx) ** 2 + (y - kb_cy) ** 2 <= kb_r * kb_r
            in_bar = False
            if kb_top <= y <= kb_bot:
                prog = (y - kb_top) / max(1e-6, kb_bot - kb_top)
                half = kb_wt + (kb_wb - kb_wt) * prog
                in_bar = abs(x - cx) <= half
            if in_knob or in_bar:
                col = (245, 245, 245)
            px += bytes(col) + b"\xff"

    raw = b"".join(b"\x00" + px[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


ICON_PNG = None  # 首次请求时生成


PAGE = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name=theme-color content="#111">
<meta name=mobile-web-app-capable content="yes">
<meta name=apple-mobile-web-app-capable content="yes">
<meta name=apple-mobile-web-app-status-bar-style content="black-translucent">
<meta name=apple-mobile-web-app-title content="开门">
<link rel="apple-touch-icon" href="/icon.png">
<link rel="icon" href="/icon.png">
<link rel="manifest" href="/manifest.webmanifest?t=__T__">
<title>开门</title><style>
html,body{height:100%;margin:0;background:#111;color:#eee;font-family:system-ui,-apple-system,sans-serif;
display:flex;align-items:center;justify-content:center;flex-direction:column;gap:22px;
-webkit-tap-highlight-color:transparent;overscroll-behavior:none;touch-action:manipulation;user-select:none}
#b{width:min(70vw,320px);height:min(70vw,320px);border-radius:50%;border:0;
font-size:clamp(28px,8.5vw,42px);font-weight:600;letter-spacing:.2em;
background:radial-gradient(circle at 34% 28%,#3a3a3a,#161616);color:#fff;
box-shadow:0 10px 40px #000,inset 0 0 0 1px #444;transition:transform .08s,filter .2s}
#b:active{transform:scale(.94)}#b.ok{filter:brightness(1.5)}#b.bad{filter:sepia(1) hue-rotate(-50deg) saturate(4)}
#s{min-height:1.5em;color:#9ab;font-size:15px;padding:0 24px;text-align:center}
</style></head><body>
<button id=b>开门</button><div id=s>就绪</div><script>
const T="__T__", AUTO=__AUTO__;
let busy=0;
async function go(){
 if(busy)return; busy=1; const btn=b, st=s;
 btn.disabled=1; btn.className=''; st.textContent='连接门锁…';
 try{
   const r=await fetch('api/open?t='+encodeURIComponent(T),{method:'POST'});
   const j=await r.json();
   st.textContent=(j.ok?'✔ ':'✘ ')+(j.msg||'');
   btn.className=j.ok?'ok':'bad';
   if(navigator.vibrate)navigator.vibrate(j.ok?[40,60,40]:[120,60,120]);
 }catch(e){st.textContent='✘ 网络错误：手机和要开门的小主机要在同一局域网'; btn.className='bad';}
 setTimeout(()=>{btn.disabled=0;btn.className='';busy=0},1000);
}
addEventListener('keydown',e=>{if(e.code==='Space'||e.code==='Enter'){e.preventDefault();go()}});
b.onclick=go;
if(AUTO && !location.search.includes('noauto')) go();   // 打开即开门；链接加 &noauto 可只看不动
</script></body></html>"""


def render_page(token: str, auto: bool) -> bytes:
    return (PAGE.replace("__T__", _safe_token(token))
                .replace("__AUTO__", "true" if (auto and ALLOW_AUTO) else "false")).encode()


def _safe_token(tok: str) -> str:
    """把 token 净化成可安全嵌进 <script> 字符串字面量的形式。

    这里没用 html.escape：`<script>` 是 raw text 元素，浏览器**不会**还原 HTML 实体，
    所以含 `&` 的 token 会被 html.escape 变成 `&amp;`，导致手机端 token 永远对不上、
    门打不开（很难查）。改成白名单校验：token 只允许字母数字/下划线/短横线
    （--dump-config 生成的就是十六进制，天然满足），从根上排除引号和尖括号，
    既没有 XSS，也不会被转义破坏。
    """
    return tok if TOKEN_SAFE.match(tok or "") else ""


def render_manifest(token: str) -> bytes:
    tok = _safe_token(token)
    start = "/?t=" + quote(tok, safe="") if tok else "/"
    return json.dumps({
        "name": "开门", "short_name": "开门",
        "start_url": start, "scope": "/",
        "display": "standalone", "orientation": "portrait",
        "background_color": "#111111", "theme_color": "#111111",
        "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png",
                   "purpose": "any maskable"}],
    }, ensure_ascii=False).encode()


def lan_ips() -> list[str]:
    """尽力列出本机 IPv4，供打印手机要访问的地址。"""
    ips: list[str] = []
    try:  # 默认出口网卡
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:  # rest：逐网卡（走 `ip` 命令，不依赖第三方库）
        import subprocess
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                             capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
            if m and m.group(1) not in ips and not m.group(1).startswith("127."):
                ips.append(m.group(1))
    except Exception:
        pass
    return ips


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _token(self, q) -> str | None:
        tok = (q.get("t") or [""])[0]
        return tok if any(hmac.compare_digest(tok, t) for t in CFG["tokens"]) else None

    def do_GET(self):
        u = urlparse(self.path)
        # keep_blank_values 必须开：否则 `?t=x&noauto` 这种无值开关会被 parse_qs 整个丢掉
        q = parse_qs(u.query, keep_blank_values=True)
        if u.path == "/":
            raw = (q.get("t") or [""])[0]
            ok = self._token(q) is not None
            # 只把校验通过的 token 固化进页面，避免把别人的输入回显出去。
            # 自动开门默认开（配置 allow_auto=false 关掉；URL 加 &noauto 单次关掉）
            auto = ok and "noauto" not in q
            self._send(200, render_page(raw if ok else "", auto), "text/html; charset=utf-8")
        elif u.path == "/manifest.webmanifest":
            tok = (q.get("t") or [""])[0] if self._token(q) else ""
            self._send(200, render_manifest(tok), "application/manifest+json; charset=utf-8")
        elif u.path == "/icon.png":
            global ICON_PNG
            if ICON_PNG is None:
                ICON_PNG = make_icon(512)
            self._send(200, ICON_PNG, "image/png")
        elif u.path == "/api/status":
            with _state_lock:
                last = _last_result
            self._send(200, json.dumps({
                "ok": True, "locks": list(LOCKS), "default": DEFAULT_LOCK,
                "last_result": last, "keep_connected": KEEP_CONNECTED,
                "seen": {k: round(time.monotonic() - v[1], 1) for k, v in _seen.items()},
            }, ensure_ascii=False).encode())
        else:
            self._send(404, b'{"ok":false,"msg":"not found"}')

    def do_POST(self):
        global _last_open, _last_result
        u = urlparse(self.path)
        q = parse_qs(u.query, keep_blank_values=True)
        if u.path not in ("/api/open", "/api/dynpwd"):
            return self._send(404, b'{"ok":false}')
        if self._token(q) is None:
            self.log_security()
            return self._send(403, b'{"ok":false,"msg":"bad token"}')

        label = (q.get("lock") or [DEFAULT_LOCK])[0]
        lock = LOCKS.get(label)
        if not lock:
            return self._send(404, json.dumps(
                {"ok": False, "msg": f"未知门锁 {label}"}, ensure_ascii=False).encode())

        if u.path == "/api/dynpwd":
            # 兜底通道：不碰蓝牙、不占冷却，纯粹替手机跑一次云 API
            try:
                pwd = cloud_password(lock)
                self._send(200, json.dumps({"ok": True, "msg": pwd}, ensure_ascii=False).encode())
            except Exception as e:  # noqa: BLE001
                self._send(502, json.dumps({"ok": False, "msg": str(e)},
                                           ensure_ascii=False).encode())
            return

        # 先解析门锁再进冷却，避免写错参数就把冷却用掉
        with _state_lock:
            if time.time() - _last_open < COOLDOWN:
                return self._send(429, json.dumps(
                    {"ok": False, "msg": "冷却中，稍等一下"}, ensure_ascii=False).encode())
            _last_open = time.time()

        try:
            msg = run_ble_open(lock)
            with _state_lock:
                _last_result = f"{time.strftime('%H:%M:%S')} {label} {msg}"
            self._send(200, json.dumps({"ok": True, "msg": msg}, ensure_ascii=False).encode())
        except Exception as e:  # noqa: BLE001
            # 蓝牙这条路断了，就顺手把云端动态密码带回去，别让人站在门口抓瞎
            extra = ""
            try:
                extra = f"｜可在锁键盘输入动态密码 {cloud_password(lock)}"
            except Exception:
                pass
            with _state_lock:
                _last_result = f"{time.strftime('%H:%M:%S')} {label} 失败:{e}"
            self._send(502, json.dumps({"ok": False, "msg": f"{e}{extra}"},
                                       ensure_ascii=False).encode())

    def log_security(self):
        print(f"[warn] 非法 token 尝试来自 {self.client_address[0]}", flush=True)

    def log_message(self, fmt, *args):  # 静音默认访问日志（里面会有 token）
        pass


# ---------------------------------------------------------------- 启动


def print_urls(token: str) -> None:
    host, port = BIND_ADDR.split(":")
    print("\n手机要打开的地址（手机和这套系统必须在同一局域网/Wi-Fi）：")
    for ip in lan_ips():
        print(f"    http://{ip}:{port}/?t={token}")
    print("\n若上面没有你寝室网段的地址，手动查：ip -4 addr  然后替换上面的 IP。")
    print(f"端口放通：sudo ufw allow from 192.168.0.0/16 to any port {port} proto tcp")
    print("打开后 → 浏览器菜单 →「添加到主屏幕」→ 桌面就有了开门图标。\n")


async def _scan_all(seconds: float):
    from bleak import BleakScanner

    found: dict[str, dict] = {}

    def cb(dev, adv):
        a = dev.address
        cur = found.get(a)
        rssi = getattr(adv, "rssi", None)
        if cur is None or (rssi is not None and (cur["rssi"] is None or rssi > cur["rssi"])):
            found[a] = {
                "name": dev.name or adv.local_name or "",
                "rssi": rssi,
                "uuids": [u.lower() for u in (adv.service_uuids or [])],
            }

    async with BleakScanner(detection_callback=cb):
        await asyncio.sleep(seconds)
    return found


def cmd_scan(seconds: float) -> int:
    """诊断：这台机器的蓝牙到底能不能喊到锁。部署前先跑这个，别装完服务才发现问题。"""
    want = {l["service"].lower(): l["label"] for l in LOCKS.values()}
    name_hint = [s.lower() for s in (CFG.get("only_if_name_contains") or [])]
    print(f"扫描 {seconds:g} 秒……让人在门口用官方 App 点一次开门更容易抓到广播。")
    try:
        found = asyncio.run(_scan_all(seconds))
    except ModuleNotFoundError:
        print("\n✘ 没装 bleak，还没走到「能不能扫到锁」这一步：")
        print("    cd ~/yunmei && ./venv/bin/pip install bleak")
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"\n✘ 扫描失败：{e}")
        print("  逐项排查：")
        print("    1) 适配器是否被识别      hciconfig -a   （要能看到 hci0）")
        print("    2) 是否被 rfkill 关掉    rfkill list    （Soft/Hard blocked 都要是 no）")
        print("    3) bluetoothd 没在跑     sudo systemctl status bluetooth")
        print("    4) 服务里 D-Bus 被拒     sudo usermod -aG bluetooth $(whoami)  然后重新登录")
        print("       （bleak 默认走 BlueZ D-Bus；只有绕过 bluez 直连 HCI 才需要")
        print("        sudo setcap 'cap_net_raw,cap_net_admin+eip' <python 绝对路径>）")
        print("    5) WiFi 与蓝牙抢射频      先 ifconfig wlan0 down 再扫一次对比")
        return 2

    if not found:
        print("\n✘ 一个 BLE 设备都没扫到 = 蓝牙链路本身不通（不是锁的问题）。按上面 1~4 排查。")
        return 2

    hits = [(a, d) for a, d in found.items() if any(u in want for u in d["uuids"])]
    print(f"\n共扫到 {len(found)} 个 BLE 设备。")
    if hits:
        print("✔ 命中配置里的门锁服务：")
        for a, d in sorted(hits, key=lambda kv: -(kv[1]["rssi"] or -999)):
            label = want[next(u for u in d["uuids"] if u in want)]
            r = d["rssi"]
            tip = "信号强" if (r is not None and r > -70) else \
                  ("信号偏弱，建议把适配器用 USB 延长线挪近门口" if r is not None else "无 RSSI")
            print(f"    {a}  {label}  rssi={r}  {tip}")
        print("\n结论：蓝牙覆盖 OK，可以按方案部署。")
        return 0

    print("✘ 没扫到配置里那把锁的服务 UUID。可能原因：")
    print("    · 锁在休眠（多数校园锁不连续广播）→ 让人在门口用 App 点一次开门，同时再扫")
    print("    · 服务器离门太远 / 隔了防火门 → 用 USB 延长线把适配器挪到靠门处")
    print("    · lockServiceUuid 没填对 → 重跑 --dump-config")
    near = [(a, d) for a, d in found.items() if (d["rssi"] or -999) > -75][:12]
    if near:
        print("\n附近信号较强的设备（若里面有名字像锁的，就是它，把它的 service 填进配置）：")
        for a, d in sorted(near, key=lambda kv: -(kv[1]["rssi"] or -999)):
            print(f"    {a}  name={d['name']!r}  rssi={d['rssi']}  uuids={d['uuids'][:3]}")
    return 1


def cmd_selftest(skip_ble: bool, ble_seconds: float = 5.0) -> int:
    """一条命令回答"这套东西在我机器上到底能不能跑起来"——**绝不开门**。

    每项失败都附带下一步该敲什么命令，所以现场不用回来问我。
    返回失败项数量（0 = 全部通过）。
    """
    fails: list[str] = []
    warns: list[str] = []

    def say(mark: str, name: str, detail: str = "") -> None:
        print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))

    def fail(name: str, hint: str) -> None:
        fails.append(name)
        say("✘", name, hint)

    def warn(name: str, hint: str) -> None:
        warns.append(name)
        say("!", name, hint)

    def good(name: str, detail: str = "") -> None:
        say("✓", name, detail)

    print("\n== 云莓开门服务的就绪自检（只读诊断，不会开门）==")

    # 1 Python
    if sys.version_info < (3, 9):
        fail("Python 版本", f"{sys.version.split()[0]} 太旧，装 python3.9+（用了 dict|None 语法）")
    else:
        good("Python 版本", sys.version.split()[0])

    # 2 bleak
    try:
        import bleak  # noqa: F401
        good("bleak 已安装", getattr(bleak, "__version__", "?"))
        has_bleak = True
    except ModuleNotFoundError:
        has_bleak = False
        fail("bleak 未安装", "cd ~/yunmei && ./venv/bin/pip install bleak")

    # 3 配置文件
    if not CONFIG_PRESENT:
        fail("配置文件", f"{CONFIG_PATH} 不存在 → "
                         f"python yunmei.py 学号 密码 --dump-config {CONFIG_PATH}")
    else:
        try:
            raw = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
            good("配置文件可解析", CONFIG_PATH)
        except json.JSONDecodeError as e:
            raw = {}
            fail("配置文件 JSON 语法", str(e))
        if raw:
            toks = raw.get("tokens") or []
            bad = [t for t in toks if not TOKEN_SAFE.match(str(t))]
            if not toks:
                fail("tokens", "配置里没有 tokens")
            elif bad:
                fail("tokens 合规", f"含非法字符：{bad}（只允许字母数字 _ -，长度6-80）")
            else:
                good(f"tokens（{len(toks)} 个）", "各自对应一个手机桌面图标")

            locks = raw.get("locks") or []
            if not locks:
                fail("locks", "配置里没有锁，重跑 --dump-config")
            else:
                miss = [l.get("label", "?") for l in locks
                        if not all(l.get(k) for k in ("label", "secret", "service",
                                                     "characteristic"))]
                if miss:
                    fail("锁参数完整性", f"这些锁缺字段：{miss}")
                else:
                    good(f"锁（{len(locks)} 把）", "、".join(l["label"] for l in locks))

            if not (raw.get("_account") or {}).get("password_md5"):
                warn("云端动态密码兜底", "缺 _account，BLE 失败时给不出动态密码；"
                                        "重跑 --dump-config 即可补上")
            else:
                good("云端动态密码兜底可用")

    # 4 监听地址
    try:
        host, port = BIND_ADDR.split(":")
        int(port)
        good("监听地址", BIND_ADDR)
        if re.match(r"^(\d+\.\d+|.*\*.*\*)", host) and host not in ("0.0.0.0", "::"):
            warn("bind 不是明确地址", f"{host} 可能不是你要的网卡")
    except ValueError:
        fail("bind 格式", f'应为 "0.0.0.0:8791"，当前 {BIND_ADDR!r}')
        host, port = "0.0.0.0", "8791"

    # 5 端口占用
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host if host != "0.0.0.0" else "", int(port)))
        good("端口空闲", port)
        running = False
    except OSError:
        running = True
        say("i", f"端口 {port} 已被占用", "多半是服务已在跑，继续探活")

    # 6 服务探活
    if running:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/status", timeout=3) as r:
                st = json.loads(r.read().decode("utf-8"))
            good("服务已在运行且可响应",
                 f"锁={st.get('locks')} 最近={str(st.get('last_result'))[:40]}")
            seen = st.get("seen") or {}
            if not seen:
                warn("还没听到锁的广播",
                     "后台监听没学到地址：人在门口让人用官方 App 点一次开门再查")
            else:
                good("已听到锁的广播",
                     ", ".join(f"{k}:{v}s前" for k, v in list(seen.items())[:3]))
        except Exception as e:  # noqa: BLE001
            fail("服务探活", f"端口被占但 /api/status 不通：{e}")

    # 7 蓝牙适配器 + 能否听到锁
    if not has_bleak:
        say("i", "蓝牙检查", "跳过（没装 bleak）")
    elif skip_ble:
        say("i", "蓝牙检查", "按 --skip-ble 跳过")
    else:
        print(f"  … 扫描蓝牙 {ble_seconds:g} 秒（人在门口让室友用官方 App 点一次开门，最容易抓到）")
        want = {l["service"].lower(): l["label"] for l in LOCKS.values()}
        try:
            found = asyncio.run(_scan_all(ble_seconds))
        except Exception as e:  # noqa: BLE001
            fail("蓝牙扫描", f"{e} → 依次查：hciconfig -a / rfkill list / "
                            f"sudo systemctl status bluetooth")
        else:
            if not found:
                fail("蓝牙适配器", "一个 BLE 设备都没扫到 = 适配器/服务层不通，"
                                  "与锁无关。查 rfkill list、systemctl status bluetooth")
            else:
                good("蓝牙适配器可扫描", f"{len(found)} 个设备")
                hits = [(a, d) for a, d in found.items() if any(u in want for u in d["uuids"])]
                if hits:
                    a, d = max(hits, key=lambda kv: kv[1]["rssi"] or -999)
                    r = d["rssi"]
                    msg = f"{a} rssi={r}"
                    if r is not None and r < -80:
                        warn("锁信号很弱", msg + " → 用 USB 延长线把适配器挪到靠门处")
                    else:
                        good("能听到配置里的锁", msg)
                else:
                    warn("没听到配置里的锁",
                         "锁多半在休眠（不连续广播）；让人在门口开一次锁的同时再跑一次自检")

    # 8 手机地址
    ips = lan_ips()
    if ips:
        tok = (CFG.get("tokens") or ["<token>"])[0]
        good("本机局域网地址", ", ".join(ips))
        print("\n  手机要打开的地址：")
        for ip in ips:
            print(f"      http://{ip}:{port}/?t={tok}")
        print("  （手机连寝室 Wi-Fi 后打开 → 菜单 → 添加到主屏幕）")
    else:
        warn("没探测到局域网 IPv4", "手动执行 ip -4 addr 查一个填进地址里")

    print(f"\n结论：{'✔ 就绪，去手机上添加到主屏幕' if not fails else f'✘ 有 {len(fails)} 项待处理'}")
    if warns:
        print(f"   另有 {len(warns)} 项提醒（不影响基本可用）：{'；'.join(warns)}")
    return len(fails)


def main() -> None:
    global loop
    ap = argparse.ArgumentParser(description="云莓局域网一键开门服务")
    ap.add_argument("--print-urls", action="store_true", help="启动后打印手机访问地址")
    ap.add_argument("--scan", nargs="?", const=8.0, type=float, metavar="秒",
                    help="诊断模式：扫描附近 BLE，确认能不能喊到锁（默认 8 秒）")
    ap.add_argument("--selftest", action="store_true",
                    help="就绪自检：查依赖/配置/端口/服务/蓝牙，不开门")
    ap.add_argument("--skip-ble", action="store_true", help="自检时跳过蓝牙扫描")
    ap.add_argument("--ble-seconds", type=float, default=5.0,
                    help="自检时蓝牙扫描时长（秒），默认 5；赶时间可设 3")
    a = ap.parse_args()

    if a.selftest:
        raise SystemExit(1 if cmd_selftest(a.skip_ble, a.ble_seconds) else 0)

    if a.scan is not None:
        raise SystemExit(cmd_scan(a.scan))

    validate_for_serve()

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    if BG_SCAN:
        asyncio.run_coroutine_threadsafe(_monitor(), loop)

    host, port = BIND_ADDR.split(":")
    srv = ThreadingHTTPServer((host, int(port)), Handler)
    print(f"云莓开门服务已启动 监听 {BIND_ADDR}  门锁: {list(LOCKS)}  "
          f"后台监听:{BG_SCAN} 长连接:{KEEP_CONNECTED}", flush=True)
    if a.print_urls:
        print_urls(CFG["tokens"][0])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
