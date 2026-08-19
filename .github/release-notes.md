解压后双击 **Ferry.exe**（Windows）即可。

客户端是 Electron 外壳 + Python agent：界面走本地 HTTP，逻辑全在
`client/ferry_core.py`。**这台机器需要装 Python 3**（agent 用它跑）。

**接入服务器**：先在服务器上装工具（`git clone` 后 `bash bridge-install.sh`），
再执行 `bridge-invite --name <机器名>` 发一张接入码，客户端左栏「＋ 添加」
粘贴即可。完整说明见 README。

不想用 Electron 也行：`cd client && python ferry_agent.py --open`，浏览器打开就是同一个界面。

### 已知提示

- **Windows**：未签名，SmartScreen 会拦一次 —— 点「更多信息 → 仍要运行」。
- **配置位置**：`%APPDATA%\Ferry\`（Win）/ `~/Library/Application Support/Ferry/`（Mac）
  / `~/.config/ferry/`（Linux）。从旧版升级会自动迁移。
- **macOS 首次打开**若提示「来自身份不明的开发者」：
  `xattr -dr com.apple.quarantine Ferry.app`
