# 贡献指南

感谢你愿意关注 `WeChatEmoji`。无论是修复问题、补文档、提建议，还是帮忙验证使用体验，都是非常有价值的贡献。

## 你可以如何参与

- 提交 Bug 报告，帮助我们定位问题
- 提交功能建议，帮助项目变得更好用
- 优化文档、文案、示例和界面提示
- 提交代码修复或新功能实现
- 帮忙测试不同微信环境、飞书页面变化和打包结果

## 提交 Issue 前

- 先阅读 [README.md](README.md) 和现有说明文件，确认不是使用方式问题
- 先搜索已有 Issue，避免重复提交
- 如果是安全问题，请不要公开提交，请改走 [SECURITY.md](SECURITY.md) 中的流程

## 本地开发建议

### 运行环境

- Windows
- Python 3.11+（数据库完整性验证需要 `sqlite3.deserialize`）
- 已安装微信客户端
- 如需验证飞书上传，请安装 Playwright Chromium

### 安装依赖

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 运行方式

```bash
python main.py
```

### 基本检查

提交前建议至少做这些检查：

```bash
python -m py_compile main.py config.py gui.py key_extractor.py wechat_extractor.py bridge_common.py feishu_uploader.py tauri_bridge.py stage_playwright_runtime.py sniff_feishu.py
```

如果你修改了 Tauri 侧桥接，也建议补做一次：

```bash
cargo check --manifest-path tauri_app/src-tauri/Cargo.toml
```

如果你改了打包逻辑，也欢迎补充验证：

```bash
build.bat
```

## 提交 PR 的建议

- 保持单个 PR 聚焦一个主题，便于 review
- 说明变更动机、影响范围和验证方式
- 如果涉及 UI，请附上截图或录屏
- 如果涉及上传逻辑，请明确说明你验证的是个人表情、企业表情包，还是两者都验证了
- 不要提交敏感信息、Cookie、登录态文件、导出数据或调试残留

## 代码与文档风格

- 优先写清晰、直接、便于维护的代码
- 不为了“炫技巧”牺牲稳定性和可读性
- 新增行为变化时，请同步更新 README 或相关文档
- 与安全、隐私、上传流程相关的修改，请尽量写清风险和边界
- Python 模块文件名保持 `snake_case`
- 文本文件默认使用 UTF-8 编码，遵循仓库中的 `.editorconfig` 与 `.gitattributes`
- 可以使用仓库中的 `pyproject.toml` 作为静态检查与格式化约定基础

## 沟通方式

欢迎友好、直接、尊重事实的讨论。不同意见是正常的，我们更关心的是问题是否被描述清楚、方案是否对用户更好。

再次感谢你的参与。
