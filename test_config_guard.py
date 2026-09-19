"""配置护栏：token 不合规 / 配置缺失时，服务要快速失败并给出可行动的提示。

这两种是现场最容易踩的（手改 token 加了特殊字符、忘了生成配置），
报错必须是"人话 + 下一步做什么"，而不是一屏 traceback。

临时配置写在**工作区内**（_cfg_guard_tmp），因为沙箱一般只放开工作区。
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TD = HERE / "_cfg_guard_tmp"
PORT = 8798
LOCK = [{"label": "L", "lock_no": "N", "mac": "", "secret": "S",
         "service": "00006e40-1000-1000-8000-00805f9b34fb",
         "characteristic": "00006e40-0002-1000-8000-00805f9b34fb"}]

ok = True


def check(name, cond, detail=""):
    global ok
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    ok = ok and bool(cond)


def write_cfg(name, obj):
    p = TD / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


def run_once(path):
    """用给定配置启动服务一次，返回 (exitcode, 合并输出)。"""
    env = dict(os.environ, YUNMEI_CONFIG=str(path), PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-X", "utf8", str(HERE / "yunmei_server.py")],
                       env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


FAKE_BLEAK = '''
import os


class _Adv:
    def __init__(s, uuids, rssi):
        s.service_uuids, s.local_name, s.rssi = uuids, "YM-lock", rssi


class _Dev:
    def __init__(s, addr):
        s.address, s.name = addr, "YM-lock"


class BleakScanner:
    def __init__(s, detection_callback=None, **kw):
        s.cb = detection_callback

    async def __aenter__(s):
        mode = os.environ["FAKE_BLEAK_MODE"]
        if mode == "raise":
            raise RuntimeError("host is down")
        if mode == "hit":
            s.cb(_Dev("AA:BB:CC:DD:EE:01"),
                 _Adv([os.environ["FAKE_SVC"]], int(os.environ.get("FAKE_RSSI", -55))))
        elif mode == "none":
            pass
        else:   # 有设备但不是目标锁（这里是标准电池服务）
            s.cb(_Dev("11:22:33:44:55:66"),
                 _Adv(["0000180f-0000-1000-8000-00805f9b34fb"], -60))
        return s

    async def __aexit__(s, *a):
        return False
'''


def run_scan(mode, cfg_path):
    """用注入的假 bleak 跑 --scan，返回 (exitcode, 输出)。"""
    fake_dir = TD / "fakebleak"
    fake_dir.mkdir(exist_ok=True)
    (fake_dir / "bleak.py").write_text(FAKE_BLEAK, encoding="utf-8")
    env = dict(os.environ, YUNMEI_CONFIG=str(cfg_path), PYTHONIOENCODING="utf-8",
               FAKE_BLEAK_MODE=mode, FAKE_SVC=LOCK[0]["service"], PYTHONPATH=str(fake_dir))
    p = subprocess.run([sys.executable, "-X", "utf8", str(HERE / "yunmei_server.py"), "--scan", "0.2"],
                       env=env, capture_output=True, text=True, encoding="utf-8", timeout=40)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def run_selftest(cfg_path, args=("--selftest", "--skip-ble"), extra_env=None):
    """跑 --selftest，注入假 bleak 以便覆盖蓝牙分支。"""
    fake_dir = TD / "fakebleak"
    fake_dir.mkdir(exist_ok=True)
    (fake_dir / "bleak.py").write_text(FAKE_BLEAK, encoding="utf-8")
    env = dict(os.environ, YUNMEI_CONFIG=str(cfg_path), PYTHONIOENCODING="utf-8",
               FAKE_BLEAK_MODE="hit", FAKE_SVC=LOCK[0]["service"], PYTHONPATH=str(fake_dir))
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, "-X", "utf8", str(HERE / "yunmei_server.py"), *args],
                       env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    TD.mkdir(exist_ok=True)

    # 1) token 含 & —— 旧实现会被 html 转义破坏，或撑破 JS 字符串字面量
    code, out = run_once(write_cfg("bad_token.json", {
        "bind": f"127.0.0.1:{PORT}", "tokens": ["abc&def"], "locks": LOCK}))
    check("含特殊字符的 token 被拒绝启动", code != 0, f"exit={code}")
    check("提示说明了允许的字符集", "字母" in out and "短横线" in out, out.strip()[:70])
    check("点名了不合规的 token", "abc&def" in out)

    # 2) token 太短，不足以当凭据
    code, out = run_once(write_cfg("short_token.json", {
        "bind": f"127.0.0.1:{PORT}", "tokens": ["abc"], "locks": LOCK}))
    check("过短的 token 被拒绝", code != 0 and "abc" in out, f"exit={code}")

    # 3) 配置文件不存在
    code, out = run_once(TD / "nope.json")
    check("缺配置时快速失败", code != 0, f"exit={code}")
    check("缺配置时给出 --dump-config 指引", "--dump-config" in out, out.strip()[:80])
    check("缺配置时不是一屏 traceback", "Traceback" not in out)

    # 4) JSON 写坏了
    broken = TD / "broken.json"
    broken.write_text('{"tokens": [', encoding="utf-8")
    code, out = run_once(broken)
    check("JSON 语法错误被明确指认", code != 0 and "JSON" in out, out.strip()[:70])

    # 5) 缺 locks
    code, out = run_once(write_cfg("no_locks.json", {
        "bind": f"127.0.0.1:{PORT}", "tokens": ["goodtoken1"]}))
    check("缺 locks 时快速失败并给指引",
          code != 0 and "--dump-config" in out, out.strip()[:70])

    # 6) 对照组：合规配置必须能正常起来，别把好事也拦了
    good = write_cfg("good.json", {
        "bind": f"127.0.0.1:{PORT}", "tokens": ["Good_tok-123"],
        "bg_scan": False, "locks": LOCK})
    env = dict(os.environ, YUNMEI_CONFIG=str(good), PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, "-X", "utf8", str(HERE / "yunmei_server.py")],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8")
    up = False
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=2) as r:
                up = r.status == 200
            break
        except Exception:
            time.sleep(0.2)
    check("合规配置正常启动并响应", up)
    proc.terminate()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    # ---- --scan 诊断的三条分支（用注入的假 bleak，不碰真蓝牙）----
    good = write_cfg("good.json", {
        "bind": f"127.0.0.1:{PORT}", "tokens": ["Good_tok-123"],
        "bg_scan": False, "locks": LOCK})

    code, out = run_scan("hit", good)
    check("--scan 命中目标锁时成功返回", code == 0, f"exit={code}")
    check("--scan 报出地址与信号强度", "AA:BB:CC:DD:EE:01" in out and "-55" in out,
          out.strip().splitlines()[-2][:80] if out.strip() else "")

    code, out = run_scan("other", good)
    check("扫到别的设备但未命中时返回 1", code == 1, f"exit={code}")
    check("未命中时给出可行动的原因清单",
          "休眠" in out and "延长线" in out, out.strip()[:90])

    code, out = run_scan("none", good)
    check("一个设备都没有时返回 2", code == 2, f"exit={code}")
    check("区分开「蓝牙链路不通」和「锁的问题」", "链路本身不通" in out, out.strip()[:70])

    code, out = run_scan("raise", good)
    check("适配器报错时给出 hciconfig/rfkill/setcap 排查项",
          code == 2 and "hciconfig" in out and "rfkill" in out and "setcap" in out,
          out.strip()[:90])

    # ---- --selftest 就绪自检 ----
    code, out = run_selftest(good)
    check("合规配置自检通过并返回 0", code == 0, f"exit={code}")
    check("自检给出手机访问地址", "http://" in out and "添加到主屏幕" in out)
    check("自检明确声明不会开门", "不会开门" in out)

    code, out = run_selftest(TD / "nope.json")
    check("缺配置时自检报失败项并非零退出",
          code != 0 and "配置文件" in out and "--dump-config" in out, f"exit={code}")

    # 蓝牙分支：注入假 bleak，能命中配置里的锁
    code, out = run_selftest(good, args=("--selftest", "--ble-seconds", "0.1"))
    check("自检确认能听到配置里的锁", "能听到配置里的锁" in out, out.strip()[-90:])

    code, out = run_selftest(good, args=("--selftest", "--ble-seconds", "0.1"),
                             extra_env={"FAKE_RSSI": "-92"})
    check("信号过弱时给出延长线建议", "信号很弱" in out and "延长线" in out)

    code, out = run_selftest(good, args=("--selftest", "--ble-seconds", "0.1"),
                             extra_env={"FAKE_BLEAK_MODE": "other"})
    check("没听到锁时归为提醒而非致命错误",
          "没听到配置里的锁" in out and "休眠" in out, out.strip()[-90:])

    code, out = run_selftest(good, args=("--selftest", "--ble-seconds", "0.1"),
                             extra_env={"FAKE_BLEAK_MODE": "none"})
    check("扫不到任何设备判定为适配器问题",
          "蓝牙适配器" in out and "与锁无关" in out, out.strip()[-90:])


try:
    main()
finally:
    shutil.rmtree(TD, ignore_errors=True)
    print("\n" + ("配置护栏全部通过" if ok else "存在失败项"))

sys.exit(0 if ok else 1)
