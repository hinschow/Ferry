# 桥接控制台 — 部署与使用手册

把**本地电脑的目录挂到远程 Linux 服务器**，并让服务器**在你本机原生执行命令**。
服务器上的 AI / 脚本因此能直接读写你的项目文件，重活（搜索、构建、测试）则下放到你本机的 CPU/GPU 跑。

- 支持 **Windows / macOS / Linux** 客户端
- 一台服务器可同时接入**多台本地机器**，互不干扰
- 全程走**出站** SSH，本地无需开放任何入站端口给公网

---

## 一、它是怎么工作的

```
                        ┌──────────────── 你的电脑 ────────────────┐
                        │  sshd (只监听 127.0.0.1:22)              │
                        │  桌面客户端 bridge_gui.py                │
                        └───────────────┬──────────────────────────┘
                                        │ ① 出站 SSH，反向隧道
                                        │    -R 2222:127.0.0.1:22
                                        ▼
┌───────────────────────── 服务器 ─────────────────────────────────┐
│  127.0.0.1:2222  ──②── sshfs 挂载 ──▶ /root/mnt/<机器名>/<目录>  │
│                  ──③── bridge-run ──▶ 在你本机执行命令           │
│                  ──④── 状态 JSON 写进已挂载的状态目录            │
└──────────────────────────────────────────────────────────────────┘
```

1. **隧道**：客户端主动连服务器，把服务器的 `127.0.0.1:2222` 接到你本机的 `22` 端口
2. **挂载**：服务器通过这条隧道用 sshfs 挂你的目录
3. **执行**：服务器通过同一条隧道回连，在你本机跑命令（Windows 走 PowerShell，Mac/Linux 走 bash）
4. **状态**：服务器每 3 秒把状态写进一个挂载目录 → 客户端**读本地文件**即可，零 SSH 握手

> 关键点：**隧道由你发起**。你的电脑不需要有公网 IP，也不用在路由器上做端口映射。

---

## 二、接入一台服务器（推荐做法）

### 第 1 步：在服务器上装工具（每台服务器只做一次）

```bash
git clone https://github.com/hinschow/Ferry.git ~/Ferry
cd ~/Ferry && bash bridge-install.sh
```

装好的是 `bridge-*` 那一套命令（**下一步要用的 `bridge-invite` 就在里面**）、
一把本服务器专用的 SSH 密钥，以及 `/root/mnt`、`/root/.winbridge` 两个目录。

需要 **root**、**sshfs**、**`/dev/fuse`**。sshfs 没装脚本会自己装；
容器里若没有 `/dev/fuse` 会直接报错退出 —— FUSE 挂载是这套东西的前提。

> 这台服务器装过了就跳过。判断办法：`bridge-check` 能跑出东西就是装好了。

### 第 2 步：在服务器上发一张接入码

```bash
bridge-invite --name 我的Mac
```

它会打印一段接入码（`FERRY1:...`）。

**每台机器一张，互不影响。** 接入码为每台机器单独生成一把密钥并追加到
`authorized_keys`，不会覆盖已有的：

```bash
bridge-invite --name mac-mini        # 给 Mac
bridge-invite --name win-desktop     # 给 Windows，两者独立并存
bridge-invite --list                 # 看已发放的和是否有效
bridge-invite --show mac-mini        # 接入码弄丢了？重新显示
bridge-invite --revoke mac-mini      # 只吊销这一台，其它照常
```

> 重名会被直接拦住并给出三条去路（换名/重新显示/先吊销再发），
> 不会出现两台机器共用一把钥匙的情况。
>
> ⚠️ 接入码包含服务器的登录凭据，只发给你信任的机器。

### 第 3 步：在本地电脑的控制台里粘贴

打开控制台 → 左栏「**＋ 添加**」→ 选「是（粘贴接入码）」→ 粘贴整段 → 完成。

客户端会自动做完这些：授权服务器公钥到本机 · 保存登录密钥 ·
写好 SSH 别名 · 加入服务器列表 · 建立隧道。**Windows 和 macOS 流程完全一样。**

前提：本机 SSH 服务要开着（服务器靠它回连）
- **Windows** 管理员 PowerShell：`Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0; Start-Service sshd; Set-Service sshd -StartupType Automatic`
- **macOS**：系统设置 → 通用 → 共享 → 远程登录

---

## 三、其它部署方式

**命令行一键**（在**本地电脑**的终端里跑，上面三步全包）

```bash
python3 ferry-setup.py root@服务器IP
```

它一口气做完：装服务器端工具 → 把服务器公钥授权到本机 → 写好 `~/.ssh/config`
别名 → 写好客户端配置 → 打开控制台。**不需要接入码**。

前提是你本来就能 `ssh root@服务器IP` 上去（它靠这条现成的通道去装）。
接入码那条路是给「服务器你能登、但不想在本地终端折腾」的场景用的。

**完全手工**（不用接入码，想自己控制每一步时）

```bash
# ① 服务器：装工具，并记下它打印的公钥
git clone https://github.com/hinschow/Ferry.git ~/Ferry
cd ~/Ferry && bash bridge-install.sh

# ② Windows，管理员 PowerShell，一整行
powershell -ExecutionPolicy Bypass -File setup-windows.ps1 -PubKey "上一步的公钥" -ServerHost <IP> -Alias <别名> -LoopbackOnly -AutoStart

# ③ macOS
bash setup-mac.sh --pubkey "上一步的公钥" --host <IP> --alias <别名> --autostart
```

然后打开控制台 →「＋ 添加」→ 选「否（手工填写）」→ SSH 别名填上面的 `<别名>`。

---

## 四、启动客户端

| 系统 | 启动方式 |
|---|---|
| **Windows** | 双击 **`Ferry.exe`**（`setup-windows.ps1` 也会生成带图标的 `Ferry.lnk`，可右键固定到任务栏） |
| **macOS** | 双击 **`Ferry.app`**（`setup-mac.sh` 自动生成，可拖进「程序」或 Dock） |
| 兜底 | `start-windows.bat` / `start-mac.command` / `python3 bridge_gui.py` |

`Ferry.exe` 不在仓库里（11 MB 的二进制不适合进 git），自己构建一次即可：

```powershell
powershell -ExecutionPolicy Bypass -File build-windows-exe.ps1
```

它自带 Python 和 Tk，装不装 Python 的机器都能跑。**它仍然执行同目录的
`bridge_gui.py`** —— 所以控制台的「重载」自更新照常有效（把整个代码冻进去就没法自更新了）。

macOS 那边同理，`Ferry.app` 是个免编译的 bundle，跑一次就有：

```bash
bash make-mac-app.sh
```

图标要重新生成的话：`python3 tools/make-icons.py`（纯标准库，不依赖 Pillow）。

首次运行会引导你**添加服务器**，只需填一项：

```
SSH 别名        myserver      ← 唯一必填（就是上一步的 -Alias）
显示名称(可选)  留空则用别名
服务器地址(可选) 仅界面显示
私钥路径(可选)   留空则用 SSH 别名里配的
```

用户名、系统类型、隧道端口**不用填** —— 客户端连接时会自动上报，服务器自动分配端口。

然后点 **「启动隧道」**，看到「已连接」即可。

---

## 五、日常使用

### 客户端界面

界面分左右两栏：**左边服务器，右边挂载与日志**。

| 区域 | 用途 |
|---|---|
| **左栏 · 服务器** | 每台一行，点行选中，点右侧 ▸ 展开看隧道/服务器/状态管道的详情和「重连·编辑·删除」。行内的蓝色按钮直接连接/断开。选中的那台左侧有一道蓝竖条 |
| **右栏 · 挂载目录** | 只显示**当前选中服务器**的挂载。「添加文件夹…」→ 本机目录和服务器位置都能自己选；「更改位置…」改已有条目；双击一行切换挂载/卸载 |
| **右栏 · 运行日志** | 可折叠，折叠状态会记住 |
| **顶栏** | 本机 SSH 服务状态（全局，不属于任何一台服务器）· 主题切换 · 重载 · 紧急断开 |
| **重载** | 程序有更新时变橙色，点一下就地重启（隧道不断） |
| **紧急断开** | 停全部隧道 + 关闭本机 sshd，服务器立即失去所有访问权 |

**主题**：默认深色，顶栏「☀ 浅色 / ☾ 深色」可切换（会就地重启界面，隧道不断）。
也可以直接改 `bridge-config.json` 的 `"theme": "dark" | "light"`。

### 服务器端命令

所有命令都支持 `-c <机器名>` 指定目标机器；不带则用默认（`/root/.winbridge/current`）。

| 命令 | 说明 |
|---|---|
| `bridge-check [-c 机器]` | 检查隧道/免密/挂载/状态管道，不带 `-c` 检查全部 |
| `bridge-mounts [-c 机器]` | 列出挂载 |
| `bridge-mount -c 机器 '<本地路径>' [服务器挂载点]` | 挂载（Windows 用 `D:\path`，Mac 用 `/Users/...`）；第二个参数可指定挂到服务器的哪里，不给就用 `/root/mnt/<机器>/<自动命名>` |
| `bridge-umount -c 机器 <挂载点>` | 卸载 |
| `bridge-ls -c 机器 [目录]` | 列服务器上某个目录的下一层（控制台的「服务器位置」浏览器用它，只列一层，不递归） |
| `bridge-run -c 机器 [-d 目录] "命令"` | **在本机原生执行** |
| `bridge-grep -c 机器 -d 目录 <关键词>` | 用本机 ripgrep 全文搜索 |
| `bridge-git -c 机器 -d 目录 <git参数>` | git 在本机跑 |
| `bridge-daemon start <名> -c 机器 -d 目录 '<命令>'` | 长驻进程（dev server 等），脱离 SSH 会话存活 |
| `bridge-daemon log <名> -c 机器` | 看日志 |
| `bridge-reset` | SSH 复用连接卡死时清理 |

---

## 六、性能：什么走挂载，什么走 bridge-run

隧道有物理延迟，**单文件操作可用，全树遍历不可用**。实测（2286 文件的项目）：

| 操作 | 挂载 | bridge-run（本机原生） |
|---|---|---|
| 读一个文件 | 350–700 ms | — |
| 列一个目录 | ~560 ms | — |
| 重复读（缓存命中） | 6–10 ms | — |
| **全树扫描 / grep -r** | **> 2 分钟** ❌ | **~1 秒** ✅ |
| **git status** | **> 2 分钟** ❌ | **~1 秒** ✅ |
| 读 5 个文件 | 2813 ms | **1121 ms**（一次往返批量取） |

**规则**：

- ✅ 挂载 → 读写**已知路径**的单个文件
- ✅ `bridge-grep` / `bridge-git` / `bridge-run` → **搜索、git、构建、测试、装依赖**
- ❌ 绝不在挂载目录上跑 `find` / `grep -r` / `git status` —— 会堵死隧道，所有使用者一起卡

---

## 七、加第二台电脑

**服务器端不用再装一遍** —— 工具装过就是装过了，只要再发一张接入码。

1. 服务器上：`bridge-invite --name 新机器名`（名字别和已有的重复）
2. 新电脑上：`git clone https://github.com/hinschow/Ferry.git` 后打开控制台
3. 控制台 →「＋ 添加」→ 粘贴接入码 → 完成
4. 服务器上 `bridge-check` 会同时显示两台

隧道端口由服务器自动分配，不会撞车。

每台机器有独立的挂载根 `/root/mnt/<机器名>/`、独立状态目录、独立隧道端口，互不影响。

---

## 八、安全

- 隧道端口只绑在服务器的 **127.0.0.1**，公网扫不到
- 本机 sshd 建议用 `-LoopbackOnly` 收窄到只监听回环，局域网也看不到
- 授权的是**该服务器专属的一把新密钥**，与你其它密钥无关，删掉那一行即可撤销

⚠️ **务必清楚**：隧道打开后，**任何能登录那台服务器的人**都能通过它以你的账户身份进入你的电脑。
只对你信任的服务器开启。随时可断：客户端点「紧急断开」，或

```powershell
Stop-Service sshd            # Windows
sudo systemsetup -setremotelogin off   # macOS
```

---

## 九、排障

| 现象 | 原因 / 处理 |
|---|---|
| 客户端一直「等待状态…」 | 状态目录没挂上。点「重连」，或服务器上 `bridge-check -c <机器>` |
| 挂载目录报 I/O error | 隧道断了。点「启动隧道」；恢复后 sshfs 会自动重连 |
| 挂载时报「已存在且非空」 | 换个空目录或还不存在的路径。挂在非空目录上会把原内容整个盖住，所以直接拦掉 |
| 挂载时报「是系统目录」 | `/etc` `/usr` `/var` 这些不能当挂载点。放 `/root/mnt/...` 或自己新建的目录 |
| Windows 上 `Ferry.exe` 被杀毒软件拦 | PyInstaller 打的包常被误报。加白名单，或直接用 `Ferry.lnk` / `start-windows.bat` |
| Mac 上 `Ferry.app` 提示「已损坏」 | 隔离标记：`xattr -dr com.apple.quarantine Ferry.app` |
| `Ferry.app` 双击没反应 | 它靠相对位置找 `bridge_gui.py`。别单独把 .app 拖走，要搬就整个文件夹一起搬 |
| `remote port forwarding failed` | 端口被占。多半是同一台机器开了两条隧道，先关掉旧的 |
| 服务器命令卡住不返回 | SSH 复用连接坏了，`bridge-reset` |
| sshd 启动失败 | `ListenAddress` 必须写在 `Match` 块**之前**；改完先 `sshd -t` 校验 |
| 隧道通但连不上 sshd | 回环地址要写 `127.0.0.1`，**不能写 `localhost`**（Windows 会解析成 ::1） |
| **Mac 上启动即「Python 意外退出」** | **系统/Xcode 自带的 python3 链接的是 Apple 废弃的 Tk 8.5.9，在现代 macOS（尤其 Apple Silicon）上必崩。**执行 `brew install python-tk`，然后用 `/opt/homebrew/bin/python3 bridge_gui.py` 启动。验证：`python3 -c "import tkinter;print(tkinter.TkVersion)"` 应 ≥ 8.6 |
| Mac 上界面起不来（无崩溃） | 缺 tkinter：`brew install python-tk` |
| Mac 双击 `.command` 没反应 | 权限丢了：`chmod +x start-mac.command` |
| Mac 提示「无法打开，来自身份不明的开发者」 | 隔离标记：`xattr -dr com.apple.quarantine .` |
| Mac 挂载 `~/Documents` / `~/Desktop` / `~/Downloads` 报 `Operation not permitted` | macOS 隐私保护（TCC）拦了 sshd。给 `/usr/libexec/sshd-keygen-wrapper` 授予**完全磁盘访问权限**：系统设置 → 隐私与安全性 → 完全磁盘访问权限 → `+` → `Cmd+Shift+G` 输入该路径 |
| Mac 上 `bridge-daemon` 起不来 | macOS 无 `setsid`，已改为自动退回 `nohup`；若仍失败看 `bridge-daemon log <名>` |

---

## 十、目录说明

分发包（可直接拷给别人，不含任何个人数据）：

```
bridge_gui.py                 桌面客户端（三平台通用）
bridge-install.sh             服务器端安装（从 server/ 拷贝，无打包步骤）
ferry-setup.py                本地一条命令全自动接入（可选路径）

setup-windows.ps1             Windows：装 sshd + 收紧到回环 + 生成 Ferry.lnk
setup-mac.sh                  macOS：同上 + 生成 Ferry.app
build-windows-exe.ps1         构建 Ferry.exe
make-mac-app.sh               生成 Ferry.app
start-windows.bat             零配置启动（不想构建 exe 时用）
start-mac.command             零配置启动（不想生成 .app 时用）

assets/ferry.png|.ico|.icns   应用图标
tools/make-icons.py           重新生成图标（纯标准库）
server/                       服务器端工具源码（唯一来源，改完直接生效）
README.md                     本文件
```

运行后会自动生成（**这些是本机专属的，拷给别人前要删掉**）：

```
bridge-config.json            你的服务器列表与挂载（首次运行由向导生成，不用手写）
status/<服务器id>/            状态管道 + daemon 日志
Ferry.exe / Ferry.app / Ferry.lnk   自己构建出来的启动器
```
