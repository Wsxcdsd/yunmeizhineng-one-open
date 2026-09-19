# 云莓智能锁 · 一键开门工具集

##快速上手
安装apk后登录账号，测试开锁.
退出软件长按桌面，添加卡片，选择“云莓一键开门”添加到桌面。
点击卡片开门。
设置中选择图片自定义卡片封面。


针对云莓智能（yunmeitech）宿舍蓝牙锁的**开门便利化**工具，含两套互相独立的方案。
协议结论来自对公开参考项目 [zxy19/yunmei_unintelligent](https://github.com/zxy19/yunmei_unintelligent)
与 [zxy19/yunmei_unintelligent_pwa](https://github.com/zxy19/yunmei_unintelligent_pwa) 的读码分析：

- 开门是**纯蓝牙本地行为**：报文 `d0 | len | lockSecret | a5 | 6位随机数 | "ID01" | a7`，无防重放设计，`lockSecret` 为静态长期密钥；
- 云端只负责下发钥匙（`login → getlock`），另有一个云端动态密码接口（锁键盘手输）。

## 目录内容

| 路径 | 说明 |
|---|---|
| [`android-widget-mod/`](android-widget-mod/修改说明.md) | 「云莓不智能」Android 修改包：桌面小部件一键开门（共存版，包名 `.mod` 与官方 App 并存），附 GitHub Actions 自动打包 |
| `yunmei.py` | 云 API 客户端 + 开锁报文构造（纯标准库零依赖）：登录、拉锁参数、取动态密码 |
| `yunmei_server.py` | 局域网开门服务：手机浏览器点按钮 → 服务器代为 BLE 开门（需 `bleak`），含 PWA 桌面图标、`--scan`/`--selftest` 诊断 |
| `部署操作手册.md` | 服务器方案的照抄式部署手册 |
| `桌面一键开门方案.md` / `开门自动化方案.md` | 方案选型与技术分析文档 |
| `setup.sh` / `yunmei.service` | 服务器方案一键部署与 systemd 模板 |
| `test_*.py` / `run_tests.py` | 三套共 71 项断言（无需蓝牙/真锁/联网） |

## 快速开始

- **有 Android 开发条件**：走 [`android-widget-mod/`](android-widget-mod/修改说明.md) 的桌面小部件路线（推荐，最方便）。
- **有一台能连上蓝牙的常驻小主机**：走 `部署操作手册.md` 的局域网服务路线。
  ```bash
  python yunmei.py 学号 密码 --dump-config yunmei_config.json   # 生成配置（含永久钥匙，务必 chmod 600）
  python yunmei_server.py --scan        # 先确认蓝牙能喊到锁
  python yunmei_server.py --print-urls  # 启动并打印手机访问地址
  ```

## 安全须知（请务必阅读）

1. `yunmei_config.json` 内含**永久开门密钥**（`lockSecret`），已列入 `.gitignore`，
   **绝不能提交仓库或分享给他人**；
2. 本项目仅限**对自己的宿舍门锁**做便利化使用，请遵守所在学校/单位管理规定；
3. 不要做公网端口映射；对外暴露接口等同于把门锁钥匙放到网络上；
4. 门锁是共享资产：保留官方 App 与实体钥匙作为兜底，别拆掉任何原有开启方式。

## 免责声明

本项目仅供学习研究与个人自用，使用者需自行承担使用风险并确保符合当地法规与校方规定；
与锁具厂商、学校后勤无关。若厂商更新协议（增加签名/滚动码），相关功能可能失效。
