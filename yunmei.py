"""云莓智能（yunmeitech）宿舍锁 —— 云 API + BLE 开锁报文构造。

协议来源：对 zxy19/yunmei_unintelligent (Android) 与
zxy19/yunmei_unintelligent_pwa (Web Bluetooth) 两个第三方客户端的源码复现，
不依赖任何抓包。仅使用 Python 标准库。

关键结论（读码得出，直接决定方案选型）：
1. 开门是**纯蓝牙本地行为**，云端不参与。App 只做两件事：
   登录云 API 取到 lockSecret / serviceUuid / characteristicUuid，
   然后向锁写一个固定格式的报文。没有任何远程开锁 HTTP 接口。
2. 报文里那 6 位"密码"是客户端 Math.random() 生成的随机数，锁并不校验它，
   报文里也没有 challenge-response / 滚动码 / 时间戳 / 签名。
   => lockSecret 是**静态长期密钥**：拿到它，任何 BLE 设备任何时候都能开门。
3. 云 API 无证书绑定、无签名：form-urlencoded + 三个 token 头即可。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import urllib.request
from pathlib import Path

BASE_URL = "https://base.yunmeitech.com/"
DEFAULT_HEADERS = {
    "x-requested-with": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "okhttp/4.9.3",
}

# nRF UART 风格：写特征 ...0002，通知特征 ...0003（Android 端就是这么 replace 出来的）
NOTIFY_CHAR_FROM = "6E400002"
NOTIFY_CHAR_TO = "6E400003"


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def build_unlock_packet(lock_secret: str, pin: int | None = None) -> bytes:
    """构造写进 characteristic 的开锁报文。

    结构与参考实现逐字节一致：
        0xD0 | len | secret(ASCII) | 0xA5 | 6 位十进制随机数(低位在前) | 'I' 'D' '0' '1' | 0xA7
    len = len(secret) + 2 + 2 + 10   （参考实现原式，不是总长度）
    """
    if pin is None:
        pin = random.randrange(0, 1_000_000)
    secret = lock_secret.encode("ascii")

    out = bytearray()
    out.append(208)                       # 0xD0
    out.append(len(secret) + 2 + 2 + 10)  # len 字段
    out += secret
    out.append(165)                       # 0xA5
    for _ in range(6):                    # 6 位十进制，低位在前
        out.append(pin % 10)
        pin //= 10
    out += b"ID01"                        # 73 68 48 49
    out.append(167)                       # 0xA7
    return bytes(out)


def notify_char(write_char: str) -> str:
    """开锁后要订阅的通知特征：把写特征的 0002 换成 0003（服务端返回大写，这里大小写都兼容）。"""
    if not write_char:
        return ""
    out = write_char.replace(NOTIFY_CHAR_FROM, NOTIFY_CHAR_TO)
    if out == write_char:
        out = write_char.replace(NOTIFY_CHAR_FROM.lower(), NOTIFY_CHAR_TO.lower())
    return out


class YunmeiClient:
    """云莓智能云 API 的极简客户端。"""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 20.0):
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.timeout = timeout
        self.token: str | None = None
        self.user_id: str | None = None
        self.school_no: str | None = None

    # ---- 传输层：与原 App 一致，form-urlencoded，值不做 URL 编码，结尾带 & ----
    def _post(self, path: str, params: dict[str, str]) -> dict | list:
        body = "&".join(f"{k}={v}" for k, v in params.items()) + "&"
        headers = dict(DEFAULT_HEADERS)
        if self.token:
            headers["token_data"] = self.token
        if self.user_id:
            headers["token_userId"] = self.user_id
            headers["tokenUserId"] = self.user_id
        req = urllib.request.Request(
            self.base_url + path.lstrip("/"), data=body.encode(), headers=headers
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}

    # ---- 业务层 ----
    def login(self, username: str, password: str, password_is_md5: bool = False) -> None:
        res = self._post(
            "login",
            {"userName": username, "userPwd": password if password_is_md5 else md5(password)},
        )
        if not res.get("success"):
            raise RuntimeError(f"登录失败：{res.get('msg')}")
        o = res["o"]
        self.token = o["token"]
        self.user_id = o["userId"]

    def schools(self) -> list[dict]:
        res = self._post("userschool/getbyuserid", {"userId": self.user_id})
        out = []
        for it in res if isinstance(res, list) else []:
            sc = it.get("school", {})
            out.append(
                {
                    "schoolNo": it.get("schoolNo"),
                    "schoolName": sc.get("schoolName"),
                    "serverUrl": sc.get("serverUrl"),
                    "token": it.get("token"),
                }
            )
        return out

    def use_school(self, school: dict) -> None:
        """切到该学校的独立服务器（门锁数据在学校节点，不在 base）。"""
        self.base_url = school["serverUrl"]
        if not self.base_url.endswith("/"):
            self.base_url += "/"
        self.token = school["token"]
        self.school_no = school["schoolNo"]

    def locks(self) -> list[dict]:
        """返回开门所需的全部参数。"""
        res = self._post("dormuser/getuserlock",
                         {"schoolNo": self.school_no, "userId": self.user_id})
        locks = []
        for it in res if isinstance(res, list) else []:
            locks.append(
                {
                    "label": f"{it.get('buildName')}-{it.get('dormNo')}",
                    "lock_no": it.get("lockNo"),                 # 可能同时是 MAC
                    "secret": it.get("lockSecret"),              # 静态密钥
                    "service": it.get("lockServiceUuid"),
                    "characteristic": it.get("lockCharacterUuid"),
                    "notify_characteristic": notify_char(it.get("lockCharacterUuid") or ""),
                }
            )
        return locks

    def dynamic_password(self, lock_no: str) -> str:
        """向云端要一个**动态开门密码**（对应 App 里的"获取开门密码"）。

        这是整条链路里唯一的**纯云端**开锁途径：拿到 lockPwd 后在锁面板键盘上
        输入即可开门，不需要蓝牙。若你的锁带数字键盘，这条最省事。
        """
        res = self._post("lockpassword/getlockpwdbylockno", {"lockNo": lock_no})
        if isinstance(res, dict) and res.get("lockPwd"):
            return str(res["lockPwd"])
        raise RuntimeError(f"未取到开门密码：{res}")



def dump_config(path: str | Path, username: str, password: str,
                school_name: str | None = None, tokens: list[str] | None = None,
                password_is_md5: bool = False) -> dict:
    """联网拉一次门锁参数，生成 yunmei_server.py 用的配置文件。

    BLE 开门完全离线（只要 secret），所以配置里没有明文口令，只有 MD5，
    方便以后重新刷新 locks / 取动态密码。
    """
    c = YunmeiClient()
    c.login(username, password, password_is_md5=password_is_md5)
    schools = c.schools()
    if not schools:
        raise RuntimeError("账号下没有绑定学校")
    target = schools[0]
    if school_name:
        target = next((s for s in schools if school_name in (s["schoolName"] or "")), target)
    c.use_school(target)

    locks = []
    for l in c.locks():
        locks.append({
            "label": l["label"],
            "lock_no": l["lock_no"],
            "mac": l["lock_no"] if re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}",
                                                l["lock_no"] or "") else "",
            "secret": l["secret"],
            "service": l["service"],
            "characteristic": l["characteristic"],
            "notify_characteristic": l["notify_characteristic"],
        })

    cfg = {
        # 手机要靠局域网直连，默认监听所有网卡（只在寝室内网可达，别做公网端口映射）
        "bind": "0.0.0.0:8791",
        "tokens": tokens or [hashlib.sha256(os.urandom(16)).hexdigest()[:20]],
        "allow_auto": True,        # 打开页面即开门：配合桌面图标才是真"点一下"
        "bg_scan": True,           # 后台常驻监听，让点击走直连而不是现场扫描
        "keep_connected": False,   # 想压到亚秒级可设 true，个别适配器不稳
        "learn_mac": True,
        "only_if_name_contains": [],   # 同层多把同型号锁互扰时填蓝牙名前缀，如 ["YM"]
        "cooldown_sec": 5,
        "scan_timeout_sec": 10,
        "direct_timeout_sec": 6,
        "cache_fresh_sec": 30,
        "default_lock": locks[0]["label"] if locks else "",
        "locks": locks,
        "_account": {
            # 与原 App 的 SecureStorage 一致：明文用户名 + 口令 MD5，绝不存明文口令
            "username": username,
            "username_md5": md5(username),
            "password_md5": password if password_is_md5 else md5(password),
            "school_no": c.school_no,
            "server": c.base_url,
            "dumped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
    p = Path(path)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return cfg


def fetch_locks(username: str, password: str, school_name: str | None = None) -> list[dict]:
    """登录 -> 选学校 -> 列出门锁。password 传明文；若已是 MD5 用 fetch_locks_md5。"""
    c = YunmeiClient()
    c.login(username, password)
    return _locks_after_login(c, school_name)


def fetch_locks_md5(username: str, password_md5: str,
                    school_name: str | None = None) -> list[dict]:
    c = YunmeiClient()
    c.login(username, password_md5, password_is_md5=True)
    return _locks_after_login(c, school_name)


def _locks_after_login(c: "YunmeiClient", school_name: str | None) -> list[dict]:
    schools = c.schools()
    if not schools:
        raise RuntimeError("账号下没有绑定学校")
    target = schools[0]
    if school_name:
        target = next((s for s in schools if school_name in (s["schoolName"] or "")), target)
    c.use_school(target)
    return c.locks()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="云莓智能：拉取门锁参数 / 生成开锁报文 / 取动态密码")
    p.add_argument("username", nargs="?")
    p.add_argument("password", nargs="?")
    p.add_argument("--school", help="按学校名子串选择学校节点")
    p.add_argument("--secret", help="只离线生成 BLE 报文，不联网")
    p.add_argument("--pin", type=int, help="固定那 6 位随机数，便于复现")
    p.add_argument("--md5", action="store_true", help="口令已经是 MD5")
    p.add_argument("--dynpwd", metavar="LOCKNO", help="向云端取该锁的动态开门密码")
    p.add_argument("--raw", action="store_true", help="输出便于脚本解析的 KEY=VALUE 行")
    p.add_argument("--dump-config", metavar="PATH",
                   help="生成 yunmei_server.py 用的配置文件并退出")
    p.add_argument("--token", action="append", default=[],
                   help="给开门服务设置访问 token，可重复传（默认自动生成）")
    a = p.parse_args()

    if a.secret:
        pkt = build_unlock_packet(a.secret, a.pin)
        print(f"len={len(pkt)}  hex={pkt.hex(' ')}")
        raise SystemExit(0)

    if not a.username:
        p.print_help()
        raise SystemExit(0)

    if a.dump_config:
        cfg = dump_config(a.dump_config, a.username, a.password, a.school,
                          a.token or None, password_is_md5=a.md5)
        print(f"已写入 {a.dump_config}（权限 600）")
        print(f"访问 token：{cfg['tokens'][0]}")
        for l in cfg["locks"]:
            print(f'  {l["label"]}  service={l["service"]}  '
                  f'mac={l["mac"] or "（无，走扫描）"}')
        raise SystemExit(0)

    c = YunmeiClient()
    c.login(a.username, a.password, password_is_md5=a.md5)
    schools = c.schools()
    target = schools[0]
    if a.school:
        target = next((s for s in schools if a.school in (s["schoolName"] or "")), target)
    c.use_school(target)

    if a.dynpwd:
        print(c.dynamic_password(a.dynpwd))
        raise SystemExit(0)

    ls = c.locks()
    if a.raw:
        print(f"USERNAME_MD5={md5(a.username)}")
        print(f"SCHOOL_NO={c.school_no}")
        print(f"TOKEN={c.token}")
        print(f"USER_ID={c.user_id}")
        print(f"SERVER={c.base_url}")
        for l in ls:
            print("LOCK\t" + "\t".join([
                l["label"], l["lock_no"] or "", l["secret"] or "",
                l["service"] or "", l["characteristic"] or "",
                build_unlock_packet(l["secret"], a.pin).hex(),
            ]))
    else:
        print(json.dumps(ls, ensure_ascii=False, indent=2))
        for l in ls:
            print(f'{l["label"]} 报文 -> {build_unlock_packet(l["secret"], a.pin).hex(" ")}')

