#!/usr/bin/env bash
# 云莓开门服务 · 一键部署（全部动作都发生在 ~/yunmei 里）
#
# 设计约束（重要）：
#   · 只在 $HOME/yunmei 下新建文件，绝不碰你已有的任何项目（AstrBot / NapCat 等）
#   · 已存在的 yunmei_config.json **绝不覆盖**（里面是开门钥匙），需要覆盖请手动删
#   · 系统级改动只有一条：装 systemd unit（要用 sudo，且会先问你）
#   · 随时可整体回滚：sudo systemctl disable --now yunmei && rm -rf ~/yunmei /etc/systemd/system/yunmei.service
#
# 用法：
#   bash setup.sh              只建目录 + 装依赖 + 自检（不动 systemd）
#   bash setup.sh 学号 密码     再多做一步：拉锁参数生成配置
#   bash setup.sh --service    额外安装并启动 systemd 开机自启（会要 sudo 密码）
set -uo pipefail

DIR="$HOME/yunmei"
PYBIN="python3"
INSTALL_SERVICE=0
ARGS=()
for a in "$@"; do
  [[ "$a" == "--service" ]] && INSTALL_SERVICE=1 || ARGS+=("$a")
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$1"; }
die()  { printf '\n\033[1;31m✘ %s\033[0m\n' "$1"; exit 1; }

say "0. 环境检查"
command -v "$PYBIN" >/dev/null || die "找不到 python3（ubuntu 上：sudo apt install python3-venv python3-pip）"
"$PYBIN" -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' \
  || die "python3 版本过低（$("$PYBIN" -V)），需要 3.9+。ubuntu 20.04 默认即可。"
"$PYBIN" -m venv --help >/dev/null 2>&1 \
  || die "缺少 venv 模块：sudo apt install python3-venv"
"$PYBIN" -V

say "1. 建目录 $DIR（只在里面新建，已存在的配置不覆盖）"
mkdir -p "$DIR" || die "建目录失败，检查权限/磁盘"
cd "$DIR"

# 把脚本自己需要的文件从"当前所在目录"拷过来；已在目标位置的同名文件则刷新（代码可以随时更新）
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for f in yunmei.py yunmei_server.py CONFIG.example.json yunmei.service; do
  if [[ -f "$SRC/$f" && "$SRC" != "$DIR" ]]; then
    cp -f -- "$SRC/$f" "$DIR/$f" && echo "  已放置 $f"
  elif [[ -f "$DIR/$f" ]]; then
    echo "  已存在 $f"
  else
    die "缺少 $f，请把交付包里的文件放在同一目录后重跑"
  fi
done

if [[ -f yunmei_config.json ]]; then
  chmod 600 yunmei_config.json
  warn "yunmei_config.json 已存在，保持不动（开门钥匙，不覆盖）"
fi

say "2. 虚拟环境与依赖（venv 建在 $DIR/venv，不影响系统 Python）"
[[ -d venv ]] || "$PYBIN" -m venv venv || die "建 venv 失败"
./venv/bin/pip install -q --upgrade pip 2>/dev/null || warn "升级 pip 失败（多半没公网，忽略）"
./venv/bin/pip install -q bleak || die "安装 bleak 失败。没公网时可先在能上网的机器上下好 bleak 的 wheel 再拷过来装"
echo "  bleak $(./venv/bin/python -c 'import bleak;print(getattr(bleak,"__version__","?"))')"

if [[ ${#ARGS[@]} -ge 2 ]]; then
  say "3. 拉取锁参数生成配置"
  ./venv/bin/python yunmei.py "${ARGS[0]}" "${ARGS[1]}" --dump-config yunmei_config.json \
    || die "拉取失败。常见原因：学号/密码错、学校服务挂了、这台机器出不了公网。"
elif [[ ! -f yunmei_config.json ]]; then
  say "3. 跳过拉取（没给学号密码）"
  warn "还没有配置，下一步自检会报『配置文件不存在』，属正常。"
  echo "     生成配置：./venv/bin/python yunmei.py 学号 密码 --dump-config yunmei_config.json"
else
  say "3. 跳过拉取（已有配置）"
fi

say "4. 蓝牙能否听到锁（这一步决定方案成不成立）"
echo "    提示：让人在门口用官方 App 点一次开门，此刻最容易抓到广播。"
if ./venv/bin/python yunmei_server.py --scan; then
  echo "  ✔ 能听到锁"
else
  rc=$?
  [[ $rc -eq 2 ]] && warn "蓝牙链路本身不通 —— 一定要先解决，否则后面白搭（看上面的 hciconfig/rfkill 提示）"
  [[ $rc -eq 1 ]] && warn "暂时没听到锁 —— 锁可能在休眠。先继续，之后再验证"
fi

say "5. 就绪自检"
./venv/bin/python yunmei_server.py --selftest --skip-ble
rc=$?

if [[ $INSTALL_SERVICE -eq 1 ]]; then
  say "6. 安装 systemd 服务（唯一用到 sudo 的一步）"
  [[ -f yunmei_config.json ]] || die "还没有配置，装服务没意义；先跑一次 setup.sh 学号 密码"
  sed -e "s#__YUNMEI_HOME__#$DIR#g" -e "s/__YUNMEI_USER__/$(id -un)/" \
      yunmei.service | sudo tee /etc/systemd/system/yunmei.service >/dev/null \
    || die "写 /etc/systemd/system/yunmei.service 失败"
  sudo systemctl daemon-reload
  sudo systemctl enable --now yunmei || die "启动失败：journalctl -u yunmei -n 50"
  sleep 2
  systemctl is-active --quiet yunmei && echo "  ✔ 服务运行中" || warn "服务未 active，查：journalctl -u yunmei -n 50"
else
  say "6. 跳过 systemd（加 --service 参数即可安装开机自启）"
  echo "    想先手动试跑：./venv/bin/python yunmei_server.py --print-urls"
fi

say "完成"
echo "  手动前台试跑：cd $DIR && ./venv/bin/python yunmei_server.py --print-urls"
echo "  随时重新体检：cd $DIR && ./venv/bin/python yunmei_server.py --selftest"
echo "  整体卸载：    sudo systemctl disable --now yunmei; sudo rm -f /etc/systemd/system/yunmei.service; sudo systemctl daemon-reload; rm -rf $DIR"
exit $rc
