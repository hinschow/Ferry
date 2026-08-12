解压后双击 **Ferry.exe**（Windows）或 **Ferry.app**（macOS）即可。

压缩包里已经带了 `bridge_gui.py` 和图标 —— 启动器只是外壳，真正跑的是同目录的
`bridge_gui.py`，所以控制台的「重载」自更新照常有效。

**接入服务器**：先在服务器上装工具（`git clone` 后 `bash bridge-install.sh`），
再执行 `bridge-invite --name <机器名>` 发一张接入码，客户端左栏「＋ 添加」粘贴即可。
完整说明见 README。

### 已知提示

- **Windows**：未签名，SmartScreen 会拦一次 —— 点「更多信息 → 仍要运行」。
  PyInstaller 打的包也常被杀毒软件误报，需要时加白名单，或改用包里的
  `start-windows.bat`（直接跑 Python，不经打包）。
- **Windows 包是 x64**，ARM 版 Windows 请自己跑 `build-windows-exe.ps1`。
- **macOS**：`Ferry.app` 是免编译的脚本包，需要带 Tk 8.6+ 的 Python：
  `brew install python-tk`（系统自带的 Tk 8.5.9 会直接崩）。
- **macOS 首次打开**若提示「来自身份不明的开发者」：
  `xattr -dr com.apple.quarantine Ferry.app`
