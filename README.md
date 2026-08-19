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
                        │  Ferry 客户端（Electron + agent）        │
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

> **已经装过老版本？** 同一条命令就是升级，直接重跑：
> ```bash
> cd ~/Ferry && git pull && bash bridge-install.sh
> ```
> 它是幂等的：已有密钥跳过、`/root/.winbridge/config` 不覆盖、**隧道和挂载全程不断**。
> 还会顺带清掉老版本残留（单客户端时代的孤儿状态守护、改名前的 `win-*.legacy`
> 旧脚本、config 里已无人读取的 `WIN_*` 键 —— 旧 config 备份成 `config.bak`）。
>
> ⚠️ 老版本可以单文件 scp `bridge-install.sh` 过去，**新版本不行** ——
> 它从同目录的 `server/` 装，必须克隆整个仓库。不是 git clone 装的那台，
> `rm -rf ~/Ferry` 后重新 clone 即可。

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
> ⚠️ 接入码包含对应账户的登录凭据，只发给你信任的机器。

### 角色隔离（默认行为）

每张接入码会在服务器上建一个**专属账户** `ferry-<名字>`，该机器的挂载落在它自己的
家目录里。**别的角色读不到** —— 不是「权限不足」，是 FUSE 在没开 `allow_other` 时
只允许挂载者本人访问，连 `cat` 具体文件都会 Permission denied。

| 身份 | 能否读到某个角色的挂载 |
|---|---|
| 该角色自己 | ✅ |
| 另一个角色 | ❌ Permission denied |
| root 直接读 | ❌ Permission denied |
| root 经 `bridge-as <名字>` | ✅ |

管理员仍然看得到（`su` 过去即可），这是设计意图 —— 挡的是客户端之间，不是管理员。

```bash
bridge-check                 # 末尾会列出所有独立角色
bridge-as mac                # 切进某个角色，看它的挂载
bridge-as mac 'ls ~/mnt'     # 或直接执行一条命令
```

不想要隔离（所有客户端共用 `/root`，像 v1.0 那样）：

```bash
bridge-invite --name win --root
```

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
（已移除，用接入码流程）
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
powershell -ExecutionPolicy Bypass -File client/setup-windows.ps1 -PubKey "上一步的公钥" -ServerHost <IP> -Alias <别名> -LoopbackOnly -AutoStart

# ③ macOS
bash client/setup-mac.sh --pubkey "上一步的公钥" --host <IP> --alias <别名> --autostart
```

然后打开控制台 →「＋ 添加」→ 选「否（手工填写）」→ SSH 别名填上面的 `<别名>`。

---

## 四、启动客户端

客户端是 **Electron 外壳 + Python agent**：界面是网页（本地 HTTP），
隧道/挂载/接入码等全部逻辑在 `ferry_core.py`。这样换界面不必重写平台逻辑，
而且不装 Electron 也能用。

### 最省事：直接用浏览器

```bash
cd client
python ferry_agent.py --open
```

零构建、零依赖（Python 自带 http.server），浏览器打开就是完整界面。

### 要原生窗口和托盘图标

```bash
cd client/electron
npm install          # 约 100 MB
npm start            # 开发时直接跑
npm run build        # 打包成 client/_electron/Ferry-win32-x64/Ferry.exe
```

打出来的包约 270 MB（Electron 的体量），**仍然需要机器上有 Python 3** ——
agent 是 Python 写的。打包前记得先退出所有 Ferry 进程，否则文件被占用会报 `EBUSY`。

预编译的包在 **[Releases](../../releases)**，打 `v*` 标签自动构建。

### 配置放在哪

```
%APPDATA%\Ferry\                        Windows
~/Library/Application Support/Ferry/     macOS
~/.config/ferry/                         Linux
```

配置跟着用户走而不是跟着代码走 —— 打包应用的程序目录每次重装都会被覆盖，
放那儿必丢。从旧版升级时会自动从老位置迁过来，老文件保留不动。

首次运行点左栏「**＋ 添加**」粘贴接入码即可，不需要手填任何东西。

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
| `bridge-port-clean [-c 机器] [端口]` | 隧道端口被僵死会话占住时清理（需 root）。先探对端有没有 SSH banner，**活着的隧道绝不动** |
| `bridge-as <名字>` | 以某个角色的身份查看它的挂载（管理员） |
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
| Windows 上 `Ferry.exe` 被杀毒软件拦 | 未签名的 Electron 包常被误报。加白名单，或改用 `cd client && python ferry_agent.py --open` 走浏览器 |
| Mac 上 `Ferry.app` 提示「已损坏」 | 隔离标记：`xattr -dr com.apple.quarantine Ferry.app` |
| `remote port forwarding failed` | 服务器上那个端口被占。多半是上一条隧道的僵死会话还没释放 —— 控制台连续两次冲突会自动换端口；想立刻清掉在服务器上跑 `bridge-port-clean -c <机器>`（活着的隧道不会被动） |
| 服务器命令卡住不返回 | SSH 复用连接坏了，`bridge-reset` |
| sshd 启动失败 | `ListenAddress` 必须写在 `Match` 块**之前**；改完先 `sshd -t` 校验 |
| 隧道通但连不上 sshd | 回环地址要写 `127.0.0.1`，**不能写 `localhost`**（Windows 会解析成 ::1） |
| Mac 提示「无法打开，来自身份不明的开发者」 | 隔离标记：`xattr -dr com.apple.quarantine .` |
| Mac 挂载 `~/Documents` / `~/Desktop` / `~/Downloads` 报 `Operation not permitted` | macOS 隐私保护（TCC）拦了 sshd。给 `/usr/libexec/sshd-keygen-wrapper` 授予**完全磁盘访问权限**：系统设置 → 隐私与安全性 → 完全磁盘访问权限 → `+` → `Cmd+Shift+G` 输入该路径 |
| Mac 上 `bridge-daemon` 起不来 | macOS 无 `setsid`，已改为自动退回 `nohup`；若仍失败看 `bridge-daemon log <名>` |

---

## 十、目录说明

分发包（可直接拷给别人，不含任何个人数据）：

```
README.md                     本文件
bridge-install.sh             服务器端安装（在服务器上跑这个）
server/                       它装的 20 个 bridge-* 工具，唯一来源

client/                       本机上跑的全部
  ferry_core.py                 核心逻辑：隧道/挂载/接入码/平台差异
  ferry_agent.py                本地 HTTP agent，把核心暴露成 JSON API
  ui/                           网页界面（html + css + js）
  electron/                     Electron 外壳（main.js + package.json）
  setup-windows.ps1             装 sshd + 收紧到回环 + 建快捷方式
  setup-mac.sh                  同上，macOS
  assets/                       图标 + make-icons.py（纯标准库生成）

.github/workflows/            打 tag 自动构建并发布
```

按「在哪台机器上跑」分层：根目录那个 `bridge-install.sh` 和 `server/` 是服务器端，
`client/` 里的东西全部在你自己电脑上跑。

运行后会自动生成（**这些是本机专属的，拷给别人前要删掉**）：

```
bridge-config.json            你的服务器列表与挂载（首次运行由向导生成，不用手写）
status/<服务器id>/            状态管道 + daemon 日志
Ferry.exe / Ferry.app / Ferry.lnk   自己构建出来的启动器
```
