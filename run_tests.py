"""一次跑完所有测试，并报告实跑的断言数量。

用法：
    python run_tests.py            # 全跑
    python run_tests.py smoke      # 只跑冒烟（名字前缀匹配）

说明：这套测试**不需要蓝牙、不需要真锁、不需要联网**，在任意装了 Python 3.9+
的机器上都能跑（包括 Windows）。开门动作被替换成假客户端，所以永远不会真开门。
"""
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = [
    ("smoke", "test_smoke.py", "HTTP 接口 / PWA / 鉴权 / 冷却 / 兜底通道"),
    ("ble", "test_ble_logic.py", "BLE 开门核心逻辑（直连、回落、报文、电量、让出射频）"),
    ("guard", "test_config_guard.py", "配置护栏 + --scan 诊断 + --selftest 就绪自检"),
]


def main() -> int:
    want = sys.argv[1:] 
    suites = [s for s in SUITES if not want or any(s[0].startswith(w) for w in want)]
    if not suites:
        print(f"没有匹配 {want} 的测试套件；可选：{[s[0] for s in SUITES]}")
        return 2

    total = 0
    failed = []
    for key, fname, desc in suites:
        print(f"\n{'='*60}\n▶ {fname}  —— {desc}\n{'='*60}")
        try:
            p = subprocess.run([sys.executable, "-X", "utf8", str(HERE / fname)],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=600)
            out = (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            print("✘ 超时（>600s），当成失败")
            failed.append(f"{fname}(超时)")
            continue
        print(out.strip())
        n = len(re.findall(r"^PASS\b", out, re.M))
        total += n
        if p.returncode != 0:
            failed.append(fname)
            print(f"→ 失败（exit={p.returncode}）")
        else:
            print(f"→ 通过，{n} 项断言")

    print(f"\n{'='*60}")
    if failed:
        print(f"✘ 失败套件：{failed}")
    print(f"合计实跑断言 = {total}" if not failed else f"已跑断言 = {total}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
