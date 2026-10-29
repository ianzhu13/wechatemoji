# 🎉 WeChatEmoji 🧸✨

> 把微信里的可爱表情一只只请出来，再整整齐齐送去飞书的小工具 🐣💬➡️📦➡️🛫

一个面向 Windows 的微信表情提取与飞书导入工具，适合做个人表情备份、整理和迁移。项目同时提供 GUI 和 CLI 两种使用方式，目标是把“提取表情这件麻烦事”变得更轻松一点。

## 🌈 项目简介

`WeChatEmoji` 是一个面向 Windows 的小工具，帮助你从本机微信客户端里提取自定义表情，并导出为常见图片格式，进一步支持导入飞书个人表情或企业表情包。

整个项目的目标很简单：

- 😺 少一点手工翻目录
- 🪄 少一点重复搬运
- 🎈 多一点“表情终于整理好了”的快乐

## ✨ 能做什么

- 🔍 自动发现本机微信账号与表情数据
- 🔐 提取并解密微信表情资源
- 🖼️ 导出为常见图片格式（PNG / GIF 等）
- 🧰 提供图形界面模式，适合日常使用
- ⌨️ 提供 CLI 模式，方便调试和自动化
- 🚀 支持导入飞书个人表情
- 🏢 支持导入飞书企业表情包
- 🩺 提供 `audit` 审计命令，帮助检查环境和链路状态

## 🤝 社区协作

如果你希望一起把这个仓库做得更稳、更好用，下面这些文件会很有帮助：

- [CONTRIBUTING.md](CONTRIBUTING.md)：如何提 Issue、提 PR、参与改进
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)：社区行为准则
- [SECURITY.md](SECURITY.md)：安全问题提报方式
- [SUPPORT.md](SUPPORT.md)：遇到问题时怎么寻求帮助
- [LICENSE](LICENSE)：MIT 许可证正文

工程规范方面，仓库也已经补齐了这些基础设施：

- `.editorconfig`：统一编码、缩进和换行风格
- `.gitattributes`：统一文本文件行尾与二进制文件属性
- `.github/workflows/ci.yml`：自动执行 Python 语法检查和 Tauri `cargo check`
- `.github/dependabot.yml`：帮助维护 Python / npm / Cargo / GitHub Actions 依赖

## 🧁 使用场景

- 📦 备份自己的微信收藏表情
- 🧹 整理散落在本机的微信表情资源
- 🛫 批量迁移到飞书继续使用
- 🧪 验证提取链路、上传链路和运行环境

## 🛠️ 环境要求

- Windows 系统
- 本机已安装并登录微信
- 提取时建议微信客户端处于运行状态
- Python 3.11+（源码运行时，数据库完整性验证需要 `sqlite3.deserialize`）

## 🚀 安装

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 安装 Playwright Chromium

如果你需要使用飞书导入能力，请继续安装：

```bash
python -m playwright install chromium
```

## 🎮 使用方式

### 图形界面模式（推荐）🐥

```bash
python main.py
```

适合大多数同学，点一点就能完成提取、预览和导入。

### 命令行模式 ⌨️

```bash
# 提取微信表情
python main.py --cli extract

# 提取指定微信账号的表情
python main.py --cli extract --wxid <wxid>

# 上传到飞书个人表情
python main.py --cli upload --mode personal

# 上传到飞书企业表情包
python main.py --cli upload --mode enterprise --pack-name "我的表情包"

# 先提取，再继续导入飞书
python main.py --cli full --mode personal

# 检查提取链路和上传环境
python main.py --cli audit
```

## 📦 导出与数据目录

密钥提取优先只读定位 WCDB `Config.Cipher`，保留旧版 `x'…'` 内存候选回退。
候选必须通过目标库逐页 HMAC、解密后的 SQLite 头检查和 `PRAGMA integrity_check`。
仅诊断提取链路时可运行（不会保存密钥、明文库或进程内存转储）：

```powershell
python key_extractor.py "<账号目录>\db_storage\emoticon\emoticon.db" --diagnose
```

日志只包含阶段信息和计数：`regions_read` 为成功读取的去重区域数，`legacy_matches` /
`cipher_matches` 为两种格式匹配数，`target_candidates` 为 salt 匹配或首页验证关联的去重候选数，
`page1_verified` / `integrity_verified` 为首页验证和全库完整验证通过数。salt 关联本身不表示成功。
`bytes_read` 累计各轮扫描；`read_failures` / `partial_reads` 记录失败或部分成功的读取调用。
Config.Cipher 对象偏移与掩码依赖微信版本；当前页面格式限定为 SQLCipher 4、4096 字节页、
80 字节保留区和加密文件头。需要 64 位 Python，以及读取微信进程的权限。
只验证主数据库文件，不合并 WAL；微信正在写入时完整性验证可能失败，可在空闲时重试。

离线回归测试：`python -m unittest discover -s tests -v`（只使用合成夹具）。
登录跳转的浏览器回归需要已安装 Playwright Chromium，否则会跳过；测试页面在本地拦截，
不会登录真实账号或上传文件。

若扫码后只有头像和灰色占位，说明消息列表还未加载完成；工具会等待实际会话项或聊天输入区可见后继续。
更新代码后请关闭旧程序及其打开的浏览器，再运行 `python main.py`，已导出的表情可以继续使用。

个人表情导入会跳过无输入框的通知会话，并复用已打开的自定义表情面板。
自动定位失败时，手动打开一次「表情 → 自定义 → 添加」即可由工具接管同一张图片。
文件提交后结果未确认时会停止本批，不重复提交；界面分别显示成功、失败或未确认、未尝试数量。
登录超时、上传出错或中断时，飞书浏览器会保留供检查；关闭该浏览器窗口或点击「停止上传」后，
工具才会清理浏览器并允许开始下一次导入。窗口保留期间不会继续提交文件。
上传确认只读取可见通知和自定义表情列表变化，聊天正文中的“上传成功”不作为上传证据。

为了避免把运行状态写到 `exe` 同级目录，项目默认会把用户可写数据放到本机用户目录下。

- 表情导出目录：`%LOCALAPPDATA%\wechatemoji\output\emojis`
- 调试信息目录：`%LOCALAPPDATA%\wechatemoji\output\debug`
- 临时文件目录：`%LOCALAPPDATA%\wechatemoji\output\tmp`

## 🏗️ 打包 EXE

项目已经准备好了打包脚本：

```bash
build.bat
```

这个脚本会完成以下动作：

- 🎬 检查并整理 Playwright Chromium runtime
- 🧹 清理正在运行的旧版程序
- 📦 使用 PyInstaller 打包
- ✅ 校验 `wechatemoji.exe` 和前端资源是否正确输出

打包后的主程序通常位于：

```text
dist\wechatemoji\wechatemoji.exe
```

## 🗂️ 项目结构

```text
.
├─ main.py                     # 程序入口
├─ gui.py                      # PyWebView 图形界面桥接
├─ config.py                   # 路径与运行配置
├─ wechat_extractor.py         # 微信表情提取核心
├─ key_extractor.py            # 微信密钥提取
├─ feishu_uploader.py          # 飞书上传逻辑
├─ bridge_common.py            # GUI / 桥接公共逻辑
├─ tauri_bridge.py             # Tauri 桥接
├─ web/
│  ├─ index.html               # 前端页面
│  └─ tauri-bridge.js          # Tauri 前端桥接
├─ tauri_app/                  # Tauri 桌面端工程
├─ build.bat                   # 一键打包脚本
└─ wechatemoji.spec            # PyInstaller 配置
```

## 🧠 工作原理

1. 🕵️ 定位微信数据目录与表情数据库
2. 🔑 提取数据库解密所需信息
3. 🧩 读取并整理表情记录
4. 🖼️ 还原导出为可直接查看/上传的图片文件
5. 🚀 按需导入飞书个人表情或企业表情包

## ⚠️ 注意事项

- 仅建议用于个人数据整理、备份与迁移 🌿
- 飞书上传能力依赖浏览器自动化，页面结构变化可能影响稳定性 🧪
- 企业表情包上传请遵循所在组织的规范要求 🏢
- 如果你是从源码运行，请先确认 Playwright Chromium 已安装完成 🛠️

## 💖 致谢


也感谢 **姚康利领导** 给予的创作空间，让我们可以把想法做成真正落地的小工具 🌟🙌

## 📄 License

[MIT License](LICENSE)
