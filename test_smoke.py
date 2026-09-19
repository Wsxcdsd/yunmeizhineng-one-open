"""冒烟测试：不依赖蓝牙，只验证 yunmei_server 的 HTTP 层。
检查：首页可取、status 正常、错误 token 被拒(403)、正确 token 走到 BLE 并优雅失败(502)、
冷却生效(429)、未知锁(404)。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG_PATH = HERE / "_smoke_config.json"
PORT = 8791
TOKEN = "smoke-token-123"

CFG_PATH.write_text(json.dumps({
    "bind": f"127.0.0.1:{PORT}",
    "tokens": [TOKEN],
    "allow_auto": True,
    "bg_scan": False,          # 测试机没有蓝牙适配器，关掉后台监听
    # 冷却设长，避免整套用例的耗时把冷却跑过期（否则时好时坏）
    "cooldown_sec": 600,
    "scan_timeout_sec": 2,
    "default_lock": "测试-305",
    "locks": [{
        "label": "测试-305", "lock_no": "AA:BB:CC:DD:EE:FF", "mac": "",
        "secret": "TESTSECRET", "service": "00006e40-1000-1000-8000-00805f9b34fb",
        "characteristic": "00006e40-0002-1000-8000-00805f9b34fb",
        "notify_characteristic": "00006e40-0003-1000-8000-00805f9b34fb",
    }],
}, ensure_ascii=False), encoding="utf-8")

env = dict(os.environ, YUNMEI_CONFIG=str(CFG_PATH), PYTHONIOENCODING="utf-8")
proc = subprocess.Popen([sys.executable, "-X", "utf8", str(HERE / "yunmei_server.py")],
                        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8")


def req(path, method="GET"):
    # 查询串里可能有中文/非 ASCII，必须百分号编码
    base, _, qs = path.partition("?")
    qs = urllib.parse.quote(qs, safe="=&")
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{base}?{qs}" if qs
                               else f"http://127.0.0.1:{PORT}{base}", method=method)
    try:
        with urllib.request.urlopen(r, timeout=40) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def req_bytes(path):
    """二进制请求（PNG 不能用文本解码来验签名）"""
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method="GET")
    with urllib.request.urlopen(r, timeout=40) as resp:
        return resp.status, resp.read()


try:
    # 等服务起来
    for _ in range(60):
        try:
            req("/api/status")
            break
        except Exception:
            time.sleep(0.3)

    ok = True

    def check(name, cond, detail=""):
        global ok
        print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
        ok = ok and cond

    s, b = req("/")
    check("首页可访问且含开门按钮", s == 200 and "开门" in b, f"status={s}")

    s, b = req("/api/status")
    j = json.loads(b)
    check("status 返回门锁列表", s == 200 and j["locks"] == ["测试-305"], b[:80])

    s, b = req("/api/open?t=wrong", "POST")
    check("错误 token 被拒 403", s == 403, f"status={s}")

    s, b = req("/api/open", "POST")
    check("无 token 被拒 403", s == 403, f"status={s}")

    s, b = req("/api/nope")
    check("未知路径 404", s == 404, f"status={s}")

    s, b = req(f"/api/open?t={TOKEN}&lock=不存在", "POST")
    check("未知门锁 404", s == 404, b[:60])

    s, b = req(f"/api/open?t={TOKEN}", "POST")
    check("正确 token 进入 BLE 流程且失败时优雅返回 502",
          s == 502 and json.loads(b)["ok"] is False, f"status={s} {b[:90]}")

    s, b = req(f"/api/open?t={TOKEN}", "POST")
    check("5 秒冷却生效 429", s == 429, f"status={s} {b[:60]}")

    s, b = req("/api/status")
    check("status 记录了最近一次结果", "测试-305" in json.loads(b)["last_result"],
          json.loads(b)["last_result"][:70])

    # ---- 桌面图标 / PWA / 一键即开 ----
    s, raw = req_bytes("/icon.png")
    check("图标可取且是合法 PNG", s == 200 and raw[:8] == b"\x89PNG\r\n\x1a\n",
          f"{len(raw)} bytes")
    # PNG 必须以 IEND 块收尾（长度域 0x00000000 + "IEND" + CRC）
    check("图标含完整 IHDR/IDAT/IEND 块",
          b"IHDR" in raw[:40] and b"IDAT" in raw and raw[-8:-4] == b"IEND",
          raw[-12:].hex())

    s, b = req(f"/manifest.webmanifest?t={TOKEN}")
    man = json.loads(b) if s == 200 else {}
    check("manifest 把 token 固化进 start_url（安装后免带参数）",
          s == 200 and man.get("start_url") == f"/?t={TOKEN}", str(man.get("start_url")))
    check("manifest 为 standalone 全屏", man.get("display") == "standalone")

    s, b = req("/manifest.webmanifest?t=WRONG")
    check("非法 token 不生成带凭据的 manifest",
          s == 200 and json.loads(b)["start_url"] == "/", "start_url=/")

    s, b = req(f"/?t={TOKEN}")
    check("默认打开即开门（allow_auto=true）",
          s == 200 and "true" in b.split("const T=")[1][:60], "AUTO=true")

    s, b = req("/")
    body = b.split("const T=")[1][:60] if "const T=" in b else ""
    check("无 token 时页面不带凭据、不自动开门",
          s == 200 and '""' in body and "false" in body, body.strip()[:40])

    s, b = req(f"/?t={TOKEN}&noauto")
    check("&noauto 可临时关闭自动开门", "false" in b.split("const T=")[1][:60])

    s, b = req("/?t=%22onmouseover%3Dalert(1)%22")
    check("非法 token 不回显原始输入（防 XSS）",
          s == 200 and "onmouseover" not in b, "已忽略")

    # token 要嵌进 <script> 字符串字面量，含 & " < 的会被白名单挡掉而不是被转义破坏
    s, b = req(f"/?t={TOKEN}")
    m = re.search(r"const T=\"([^\"]*)\"", b)
    check("合规 token 原样嵌入（不被 HTML 实体转义破坏）",
          bool(m) and m.group(1) == TOKEN, m.group(1) if m else "未匹配")
    check("页面里不含 HTML 实体形式的 token", "&amp;" not in b)

    # ---- 兜底通道：云端动态密码 ----
    # 此刻开门冷却仍在生效（前面点过），dynpwd 走的是独立通道，不该被冷却挡住；
    # 配置里没有 _account，应当清晰报错而不是去联网或崩掉。
    s, bo = req(f"/api/open?t={TOKEN}", "POST")
    check("前置条件：开门冷却此刻仍在生效", s == 429, f"open status={s}")

    s, b = req(f"/api/dynpwd?t={TOKEN}", "POST")
    j = json.loads(b) if s else {}
    check("dynpwd 是独立通道，不受开门冷却影响", s != 429, f"status={s}")
    check("未配 _account 时 dynpwd 优雅报错 502",
          s == 502 and j.get("ok") is False, f"status={s} {str(j)[:60]}")
    check("dynpwd 报错信息可行动", "account" in (j.get("msg") or ""), str(j)[:90])

    s, b = req("/api/dynpwd", "POST")
    check("dynpwd 也要鉴权", s == 403, f"status={s}")


    print("\n" + ("全部通过" if ok else "存在失败项"))
finally:
    proc.terminate()
    try:
        out = proc.communicate(timeout=5)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        out = ""
    CFG_PATH.unlink(missing_ok=True)
    if out and out.strip():
        print("--- 服务日志 ---")
        print(out.strip()[:800])

sys.exit(0 if ok else 1)
