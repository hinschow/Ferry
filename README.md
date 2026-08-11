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

## 二、服务器端部署（每台服务器只做一次）

前置：Ubuntu/Debian、root 权限、支持 FUSE（`/dev/fuse` 存在）。

```bash
# 1. 把 bridge-install.sh 传到服务器
scp bridge-install.sh root@服务器IP:/root/

# 2. 执行（参数是占位用的，客户端连上后会自动上报真实信息）
bash /root/bridge-install.sh yourname
```

安装脚本会：装 `sshfs` → 建目录与配置 → **生成该服务器专属密钥** → 装好全部命令行工具 →
**打印出你在本地电脑上要执行的命令**（含公钥、服务器 IP，可直接复制）。

把打印出来的**公钥**记下来，下一步要用。

---

## 三、本地电脑接入

### Windows

```powershell
# 管理员 PowerShell，在 bridge-console 目录里执行
powershell -ExecutionPolicy Bypass -File setup-windows.ps1 `
  -PubKey "服务器打印的那行 ssh-ed25519 ..." `
  -ServerHost 服务器IP -Alias myserver `
  -Identity ~/.ssh/你连服务器用的私钥 `
  -LoopbackOnly -AutoStart
```

一条命令完成：装 OpenSSH Server → 授权公钥 → 收窄 sshd 只监听回环 → 加 SSH 别名 → 建开机自启。

### macOS

```bash
# 先打开：系统设置 → 通用 → 共享 → 远程登录
bash setup-mac.sh \
  --pubkey "服务器打印的那行 ssh-ed25519 ..." \
  --host 服务器IP --alias myserver \
  --identity ~/.ssh/你连服务器用的私钥 \
  --autostart
```

> macOS 需要 tkinter：`brew install python-tk`

### 验证

```bash
ssh -N myserver     # 不报错、光标停住 = 隧道通了
```

---

## 四、启动客户端

| 系统 | 启动方式 |
|---|---|
| Windows | 双击 `start-windows.bat` |
| macOS | 双击 `start-mac.command`（或 `python3 bridge_gui.py`） |

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

| 区域 | 用途 |
|---|---|
| **服务器** | 多台服务器并列，各自独立的隧道/状态/挂载。「保活」开启后断线自动重连（指数退避 3→60 秒） |
| **挂载目录** | 「添加文件夹…」选任意目录挂到服务器；双击一行切换挂载/卸载 |
| **运行日志** | 实时事件 |
| **重载** | 程序有更新时变橙色，点一下就地重启 |
| **紧急断开** | 停全部隧道 + 关闭本机 sshd，服务器立即失去所有访问权 |

### 服务器端命令

所有命令都支持 `-c <机器名>` 指定目标机器；不带则用默认（`/root/.winbridge/current`）。

| 命令 | 说明 |
|---|---|
| `bridge-check [-c 机器]` | 检查隧道/免密/挂载/状态管道，不带 `-c` 检查全部 |
| `bridge-mounts [-c 机器]` | 列出挂载 |
| `bridge-mount -c 机器 '<本地路径>'` | 挂载（Windows 用 `D:\path`，Mac 用 `/Users/...`） |
| `bridge-umount -c 机器 <挂载点>` | 卸载 |
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

**服务器端什么都不用做。** 客户端会自动登记并领取一个不冲突的端口。

1. 把这个目录（干净的分发包）拷到新电脑
2. 跑 `setup-windows.ps1` 或 `setup-mac.sh`（换一个 `-Alias`）
3. 打开客户端 → 添加服务器 → 填 SSH 别名 → 启动隧道
4. 服务器上 `bridge-check` 会同时显示两台

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
| `remote port forwarding failed` | 端口被占。多半是同一台机器开了两条隧道，先关掉旧的 |
| 服务器命令卡住不返回 | SSH 复用连接坏了，`bridge-reset` |
| sshd 启动失败 | `ListenAddress` 必须写在 `Match` 块**之前**；改完先 `sshd -t` 校验 |
| 隧道通但连不上 sshd | 回环地址要写 `127.0.0.1`，**不能写 `localhost`**（Windows 会解析成 ::1） |
| Mac 上界面起不来 | 缺 tkinter：`brew install python-tk` |
| Mac 双击 `.command` 没反应 | 权限丢了：`chmod +x start-mac.command` |
| Mac 提示「无法打开，来自身份不明的开发者」 | 隔离标记：`xattr -dr com.apple.quarantine .` |
| Mac 挂载 `~/Documents` / `~/Desktop` / `~/Downloads` 报 `Operation not permitted` | macOS 隐私保护（TCC）拦了 sshd。给 `/usr/libexec/sshd-keygen-wrapper` 授予**完全磁盘访问权限**：系统设置 → 隐私与安全性 → 完全磁盘访问权限 → `+` → `Cmd+Shift+G` 输入该路径 |
| Mac 上 `bridge-daemon` 起不来 | macOS 无 `setsid`，已改为自动退回 `nohup`；若仍失败看 `bridge-daemon log <名>` |

---

## 十、目录说明

分发包（可直接拷给别人，不含任何个人数据）：

```
bridge_gui.py                 桌面客户端（三平台通用）
setup-windows.ps1             Windows 一键配置
setup-mac.sh                  macOS 一键配置
bridge-install.sh             服务器端一键安装
start-windows.bat             Windows 启动器
start-mac.command             macOS 启动器
bridge-config.example.json    配置参考（正常不用手改）
README.md                     本文件
```

运行后会自动生成（**这些是本机专属的，拷给别人前要删掉**）：

```
bridge-config.json            你的服务器列表与挂载
status/<服务器id>/            状态管道 + daemon 日志
```
