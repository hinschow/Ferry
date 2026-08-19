#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ferry 核心层 —— 隧道、挂载、接入码、平台差异，不含任何界面代码。

tkinter 客户端与 HTTP agent（Electron/浏览器界面用）共用这一份实现。
这些逻辑是一路踩坑换来的（ssh.exe 接管道会死锁、PowerShell 编码、
Windows 服务 API 的 64 位句柄截断……），换界面绝不能重写一遍。

"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

APP_NAME = "桥接控制台"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _find_install_dir():
    r"""配置和状态目录放在哪。

    默认就在脚本旁边。但仓库常常是 clone 在安装目录里面的
    （D:\...\bridge-console\repo\），从 repo 那份启动就会读到一个空配置，
    界面上表现为"一台服务器都没有"，还看不出为什么 —— 用户实际踩过。
    所以往上找一层：父目录有 bridge-config.json 就用父目录。
    """
    here = BASE_DIR
    if os.path.exists(os.path.join(here, "bridge-config.json")):
        return here
    up = os.path.dirname(here)
    if up and up != here and os.path.exists(os.path.join(up, "bridge-config.json")):
        return up
    return here


INSTALL_DIR = _find_install_dir()
CFG_PATH = os.path.join(INSTALL_DIR, "bridge-config.json")
STATUS_ROOT = os.path.join(INSTALL_DIR, "status")

DEFAULT_CFG = {
    "servers": [],
    "active": None,
    "poll_local": 2,
    "poll_remote": 3,
    "font_size": 11,
    "font_family": None,      # None = 按平台自动选择
    "mono_family": None,
    "theme": "dark",          # dark | light
    "log_open": True,
}

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# 配色。CARD2 是卡片里的次级块（展开区、选中行），比卡片再深/浅一档。
THEMES = {
    "dark": dict(
        BG="#15171c", CARD="#1d2027", CARD2="#262a33", LINE="#333945",
        TEXT="#e6e8ec", MUTED="#8b93a1", ACCENT="#4c8dff", ACCENT_H="#3a7ae8",
        OK="#3ecf8e", BAD="#f2555a", WARN="#e3a008", NA="#6b7280",
        SEL="#2d3646", INPUT="#14161a",
    ),
    "light": dict(
        BG="#f5f6f8", CARD="#ffffff", CARD2="#f2f4f7", LINE="#e4e7ec",
        TEXT="#101828", MUTED="#667085", ACCENT="#175cd3", ACCENT_H="#1249ab",
        OK="#12855a", BAD="#d0342c", WARN="#b45309", NA="#98a2b3",
        SEL="#e6efff", INPUT="#ffffff",
    ),
}


# 用了 `from ferry_core import *` 的模块要登记进来 —— import * 拿到的是
# 导入那一刻的副本，不登记的话后续 apply_theme 改的颜色传不过去。
_THEME_SINKS = []


def register_theme_sink(ns):
    """ns 传调用方的 globals()。登记后随 apply_theme 一起更新。"""
    if ns not in _THEME_SINKS:
        _THEME_SINKS.append(ns)
    ns.update({k: v for k, v in globals().items()
               if k.startswith("C_") or k == "THEME"})


def apply_theme(name):
    """把配色写进模块级常量 —— 界面代码到处直接引用它们"""
    t = THEMES.get(name) or THEMES["dark"]
    vals = {"C_" + k: v for k, v in t.items()}
    vals["THEME"] = name if name in THEMES else "dark"
    globals().update(vals)
    for ns in _THEME_SINKS:
        ns.update(vals)


apply_theme("dark")           # 先给个默认，读到配置后再按用户选择覆盖

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
PLATFORM = "windows" if IS_WIN else ("macos" if IS_MAC else "linux")


# ================================================================ 平台差异

def default_fonts():
    if IS_WIN:
        return "Microsoft YaHei UI", "Consolas"
    if IS_MAC:
        return "PingFang SC", "Menlo"
    return "Noto Sans CJK SC", "DejaVu Sans Mono"


def local_user():
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def shlex_quote(v):
    """给远端 shell 用的单引号包裹"""
    return "'" + str(v).replace("'", "'\\''") + "'"


def machine_id():
    """本机标识 —— 服务器用它区分不同的本地机器。

    注意：这与「客户端里某台服务器的 id」是两个概念，
    早先混用导致注册成了新机器、分配了错误端口。
    """
    override = CFG.get("client_id")
    if override:
        return str(override)
    import socket
    name = (os.environ.get("COMPUTERNAME") or socket.gethostname() or "client")
    safe = "".join(c for c in name if c.isalnum() or c in "._-")[:40]
    return safe.lower() or "client"


def _port22_open():
    """本机 22 端口是否在监听 —— 无需任何权限，跨平台通用"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 22), timeout=3):
            return True
    except OSError:
        return False


_SVC = {}          # ctypes 句柄缓存，避免每次重新声明


def _win_service_state(name="sshd"):
    """直接问 Windows 服务管理器，不起任何进程。返回状态字符串或 None。

    这里原本是每 2 秒起一个 PowerShell 查 Get-Service —— 实测单次 543ms，
    一小时能创建 1300+ 个 powershell/conhost 进程，是本机后台进程的大头。
    换成服务 API 之后单次 0.07ms，快约 7500 倍且零进程。
    """
    try:
        import ctypes
        from ctypes import wintypes
        adv = _SVC.get("adv")
        if adv is None:
            adv = ctypes.WinDLL("advapi32", use_last_error=True)
            # 64 位下必须声明 restype：不声明的话返回值按 c_int 截断，
            # SC_HANDLE 是 64 位指针，一截就废，表现为「服务不存在」
            adv.OpenSCManagerW.restype = wintypes.HANDLE
            adv.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
            adv.OpenServiceW.restype = wintypes.HANDLE
            adv.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
            adv.CloseServiceHandle.argtypes = [wintypes.HANDLE]

            class SERVICE_STATUS(ctypes.Structure):
                _fields_ = [("dwServiceType", wintypes.DWORD),
                            ("dwCurrentState", wintypes.DWORD),
                            ("dwControlsAccepted", wintypes.DWORD),
                            ("dwWin32ExitCode", wintypes.DWORD),
                            ("dwServiceSpecificExitCode", wintypes.DWORD),
                            ("dwCheckPoint", wintypes.DWORD),
                            ("dwWaitHint", wintypes.DWORD)]

            adv.QueryServiceStatus.argtypes = [wintypes.HANDLE,
                                               ctypes.POINTER(SERVICE_STATUS)]
            _SVC["adv"], _SVC["st"] = adv, SERVICE_STATUS
        scm = adv.OpenSCManagerW(None, None, 0x0001)          # SC_MANAGER_CONNECT
        if not scm:
            return None
        try:
            h = adv.OpenServiceW(scm, name, 0x0004)           # SERVICE_QUERY_STATUS
            if not h:
                # 1060 = ERROR_SERVICE_DOES_NOT_EXIST
                return "NotInstalled" if ctypes.get_last_error() == 1060 else None
            try:
                st = _SVC["st"]()
                if not adv.QueryServiceStatus(h, ctypes.byref(st)):
                    return None
                return {1: "Stopped", 2: "StartPending", 3: "StopPending",
                        4: "Running"}.get(st.dwCurrentState)
            finally:
                adv.CloseServiceHandle(h)
        finally:
            adv.CloseServiceHandle(scm)
    except Exception:  # noqa: BLE001
        return None


def sshd_status():
    """返回 (ok, 文案)"""
    if IS_WIN:
        zh = {"Running": "运行中", "Stopped": "已停止", "StartPending": "启动中",
              "StopPending": "停止中", "NotInstalled": "未安装"}
        st = _win_service_state()
        if st is None:
            # API 走不通（权限/异常系统）时退回 sc.exe —— 仍比 PowerShell 便宜 36 倍
            rc, out = run(["sc.exe", "query", "sshd"], 15)
            st = ("Running" if "RUNNING" in out else
                  "Stopped" if "STOPPED" in out else "NotInstalled")
        return (st == "Running", zh.get(st, st))
    if IS_MAC:
        # 直接探本地 22 端口：不需要 sudo（systemsetup 在新版 macOS 要 sudo，非交互会失败）
        if _port22_open():
            return True, "运行中"
        return False, "未开启（系统设置→通用→共享→远程登录）"
    for unit in ("ssh", "sshd"):
        rc, out = run(["systemctl", "is-active", unit], timeout=10)
        if "active" in out and "inactive" not in out:
            return True, "运行中"
    return (_port22_open(), "运行中" if _port22_open() else "未运行")


def sshd_start():
    """把本机 sshd 拉起来。返回 (成功, 提示)。

    Windows 上启动服务要管理员权限，控制台本身是普通用户跑的 ——
    所以走 Start-Process -Verb RunAs 弹一次 UAC，而不是直接失败。
    """
    if IS_WIN:
        ps = ("Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden "
              "-ArgumentList '-NoLogo','-NonInteractive','-Command',"
              "'Start-Service sshd; Set-Service sshd -StartupType Automatic'")
        rc, out = run(["powershell", "-NoLogo", "-NonInteractive", "-Command", ps], 90)
        if rc != 0:
            return False, "启动失败（UAC 被取消？）：管理员 PowerShell 跑 Start-Service sshd"
        ok, text = sshd_status()
        return ok, ("sshd 已启动，并设为开机自启" if ok else f"仍未运行：{text}")
    if IS_MAC:
        return (False, "Mac 需手动开：系统设置 → 通用 → 共享 → 远程登录")
    rc, out = run(["systemctl", "start", "ssh"], 25)
    ok, text = sshd_status()
    return ok, ("sshd 已启动" if ok else "需要 root：sudo systemctl start ssh")


def sshd_stop():
    """返回 (成功, 提示文案)"""
    if IS_WIN:
        rc, out = run(["powershell", "-NoLogo", "-NonInteractive", "-Command",
                       "Stop-Service sshd"], timeout=25)
        return (rc == 0,
                "sshd 已关闭。恢复：管理员 PowerShell 执行 Start-Service sshd"
                if rc == 0 else "关闭 sshd 需要管理员权限，请手动执行 Stop-Service sshd")
    if IS_MAC:
        return (False,
                "Mac 需手动关闭：系统设置 → 通用 → 共享 → 关闭「远程登录」"
                "（或 sudo systemsetup -setremotelogin off）")
    rc, out = run(["systemctl", "stop", "ssh"], timeout=25)
    return (rc == 0, "sshd 已关闭" if rc == 0 else "需要 root 权限，请手动 systemctl stop ssh")


# ---------------------------------------------------------------- 挂载条目
#
# 配置里的挂载条目有两种形态，都要认：
#   老版本  "D:\\code-test"                                  只有本机路径
#   新版本  {"local": "D:\\code-test", "server": "/data/x"}  server 为空表示用默认位置

def mount_local(entry):
    if isinstance(entry, dict):
        return str(entry.get("local") or "")
    return str(entry or "")


def mount_target(entry):
    if isinstance(entry, dict):
        return str(entry.get("server") or "")
    return ""


def mount_entry(local, server=""):
    return {"local": local, "server": server} if server else local


def default_mount_dir(path):
    """默认挂载点目录名 —— 必须和服务器端 bridge_mount_name 算出同一个结果"""
    name = re.sub(r"_+", "_", re.sub(r"[\\/: ]", "_", str(path))).strip("_")
    return name or "mount"


_ICON_REF = []          # PhotoImage 必须留引用，否则会被回收，图标变空白




def from_sftp_path(src):
    """sshfs 来源串 -> 本机路径。Windows 要还原盘符，POSIX 原样。"""
    if ":" in src:
        tail = src.split(":", 1)[1]
    else:
        tail = src
    if IS_WIN:
        if tail.startswith("/"):
            tail = tail[1:]
        return tail.replace("/", "\\")
    return tail


# ================================================================ 配置

def migrate_legacy(loaded, servers):
    """把旧的单服务器配置自动升级成 servers 列表"""
    if servers or "ssh_alias" not in loaded:
        return servers
    return [{
        "id": loaded.get("ssh_alias", "server1"),
        "name": loaded.get("ssh_alias", "server1"),
        "ssh_alias": loaded.get("ssh_alias", ""),
        "host": loaded.get("server_host", ""),
        "tunnel_port": loaded.get("tunnel_port", 2222),
        "win_user": loaded.get("win_user", local_user()),
        "identity": None,
        "mounts": list(loaded.get("saved_paths", [])),
        "auto_tunnel": True,
        "enabled": True,
    }]


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding="utf-8") as fh:
                loaded = json.load(fh)
            cfg.update(loaded)
            cfg["servers"] = migrate_legacy(loaded, cfg.get("servers") or [])
        except Exception:  # noqa: BLE001
            pass
    return cfg


def save_cfg(cfg):
    try:
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        shutil.move(tmp, CFG_PATH)
    except Exception:  # noqa: BLE001
        pass


CFG = load_cfg()
apply_theme(CFG.get("theme", "dark"))


# ================================================================ 命令执行

def run(args, timeout=20):
    """返回 (rc, 输出)。

    必须用临时文件而不是管道：Windows 版 ssh.exe 在 stdout 接管道时会挂死不返回。
    stdin=DEVNULL 同样必要，否则 ssh 会一直等父进程的标准输入。
    """
    try:
        with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
            rc = subprocess.call(args, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
                                 timeout=timeout, creationflags=NO_WINDOW)
            fo.seek(0)
            fe.seek(0)
            out = fo.read().decode("utf-8", "replace") + fe.read().decode("utf-8", "replace")
        return rc, out
    except subprocess.TimeoutExpired:
        return -1, "超时"
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


# ================================================================ 接入码

def parse_invite(text):
    """解析服务器生成的接入码，返回 dict 或抛异常"""
    import base64, zlib
    t = "".join(text.split())
    if not t.startswith("FERRY1:"):
        raise ValueError("这不是接入码（应以 FERRY1: 开头）")
    raw = zlib.decompress(base64.urlsafe_b64decode(t[7:]))
    d = json.loads(raw.decode("utf-8"))
    for k in ("host", "srv_pub", "cli_key"):
        if not d.get(k):
            raise ValueError(f"接入码缺少字段: {k}")
    return d


def apply_invite(d, log=lambda *_a, **_k: None):
    """把接入码落地：授权服务器公钥、保存登录私钥、写 SSH 别名。
    返回 (alias, 提示列表)。全部操作都是追加式，且先备份。"""
    import re as _re
    tips = []
    home = os.path.expanduser("~")
    sshdir = os.path.join(home, ".ssh")
    os.makedirs(sshdir, exist_ok=True)
    try:
        os.chmod(sshdir, 0o700)
    except OSError:
        pass

    host = d["host"]
    alias = "ferry-" + _re.sub(r"[^A-Za-z0-9]", "-", host).strip("-")

    # 1) 本机登录服务器用的私钥
    keypath = os.path.join(sshdir, f"ferry-{_re.sub(r'[^A-Za-z0-9]', '-', host).strip('-')}")
    with open(keypath, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(d["cli_key"] if d["cli_key"].endswith("\n") else d["cli_key"] + "\n")
    try:
        os.chmod(keypath, 0o600)
    except OSError:
        pass
    tips.append(f"已保存登录密钥 {keypath}")

    # 2) 服务器公钥授权到本机（服务器要靠它回连）
    if IS_WIN:
        # Windows sshd 的 StrictModes 很严：authorized_keys 只要还带着继承来的
        # 权限就会被静默拒绝 —— 表现是服务器回连 Permission denied，日志里
        # 什么都看不出来。所以两个分支都必须 icacls /inheritance:r。
        # 目录也要显式创建：sshd 还没装时 C:\ProgramData\ssh 是不存在的。
        ps = ("$g=(Get-LocalGroup -SID 'S-1-5-32-544').Name;"
              "$a=[bool](Get-LocalGroupMember -Group $g -EA SilentlyContinue|"
              "Where-Object{$_.Name -like \"*\\$env:USERNAME\"});"
              "if($a){$d='C:\\ProgramData\\ssh';"
              "$f=\"$d\\administrators_authorized_keys\"}"
              "else{$d=\"$env:USERPROFILE\\.ssh\";$f=\"$d\\authorized_keys\"};"
              "New-Item -ItemType Directory -Force -Path $d|Out-Null;"
              "if(-not (Test-Path $f)){New-Item -ItemType File -Path $f|Out-Null};"
              f"$k='{d['srv_pub']}';"
              "if(Select-String -Path $f -SimpleMatch $k -Quiet){'SKIP'}"
              "else{Add-Content $f $k -Encoding ASCII;'ADDED'};"
              "if($a){icacls $f /inheritance:r /grant \"${g}:F\" /grant 'SYSTEM:F'|Out-Null}"
              "else{icacls $f /inheritance:r /grant \"${env:USERNAME}:F\" "
              "/grant 'SYSTEM:F'|Out-Null};"
              "if(Get-Service sshd -EA SilentlyContinue){'HASSSHD';"
              "Restart-Service sshd -EA SilentlyContinue}else{'NOSSHD'}")
        rc, out = run(["powershell", "-NoLogo", "-NonInteractive", "-Command", ps], 60)
        if "ADDED" in out:
            tips.append("服务器公钥已授权到本机（已收紧文件权限）")
        elif "SKIP" in out:
            tips.append("服务器公钥已存在，跳过")
        else:
            tips.append("⚠️ 授权公钥失败，可能需要管理员权限运行本程序")
        if "NOSSHD" in out:
            tips.append("⚠️ 本机还没装 SSH 服务 —— 服务器无法回连，挂载会失败。"
                        "顶栏「启动」按钮可以装好后一键拉起")
    else:
        ak = os.path.join(sshdir, "authorized_keys")
        cur = open(ak, encoding="utf-8").read() if os.path.exists(ak) else ""
        if d["srv_pub"].split()[1] in cur:
            tips.append("服务器公钥已存在，跳过")
        else:
            with open(ak, "a", encoding="utf-8") as fh:
                fh.write(("" if not cur or cur.endswith("\n") else "\n") + d["srv_pub"] + "\n")
            try:
                os.chmod(ak, 0o600)
            except OSError:
                pass
            tips.append("服务器公钥已授权到本机")

    # 3) SSH 别名
    cfg = os.path.join(sshdir, "config")
    cur = open(cfg, encoding="utf-8").read() if os.path.exists(cfg) else ""
    if _re.search(rf"^\s*Host\s+{_re.escape(alias)}\s*$", cur, _re.M):
        tips.append(f"SSH 别名 {alias} 已存在")
    else:
        if os.path.exists(cfg):
            shutil.copy2(cfg, cfg + ".bak")
        with open(cfg, "a", encoding="utf-8") as fh:
            fh.write(f"\nHost {alias}\n"
                     f"    HostName {host}\n"
                     f"    User {d.get('user', 'root')}\n"
                     f"    Port {d.get('port', 22)}\n"
                     f"    IdentityFile {keypath}\n"
                     f"    IdentitiesOnly yes\n"
                     f"    ServerAliveInterval 30\n"
                     f"    ServerAliveCountMax 3\n"
                     f"    StrictHostKeyChecking accept-new\n")
        try:
            os.chmod(cfg, 0o600)
        except OSError:
            pass
        tips.append(f"SSH 别名 {alias} → {d.get('user','root')}@{host}")

    return alias, tips


# ================================================================ 单台服务器

class Server:
    """封装一台服务器的隧道、状态与挂载"""

    def __init__(self, conf, log_fn):
        self.conf = conf
        self.log = log_fn
        self.lock = threading.Lock()

        self.tunnel_proc = None
        self.tunnel_since = None
        self.want_up = False
        self.port_ok = False
        self.mounts = {}
        self.state = {
            "tunnel": {"ok": None, "text": "未连接"},
            "server": {"ok": None, "text": "未检测"},
            "source": None,
            "fresh_s": None,
            "uptime": None,
        }
        self._last_ssh = 0.0
        self._probes = 0
        self._warned = False
        self._retry_at = 0.0      # 下次允许重连的时刻（指数退避）
        self._retry_n = 0         # 连续失败次数
        self._port_conflict = 0   # 连续「服务器端口被占用」次数
        self._spawn_lock = threading.Lock()
        self._err = None          # 隧道进程的 stderr 临时文件
        self._fixed_pipe = False  # 是否已尝试补挂状态管道

    @property
    def sid(self):
        return self.conf.get("id", "?")

    @property
    def alias(self):
        return self.conf.get("ssh_alias") or self.conf.get("host", "")

    @property
    def label(self):
        return self.conf.get("name") or self.sid

    @property
    def port(self):
        """隧道端口。注意不能用 conf.get(k, default) —— key 存在但值为 None 时
        它会返回 None 而不是默认值（新建服务器就是这种情况）。"""
        try:
            v = int(self.conf.get("tunnel_port") or 0)
        except (TypeError, ValueError):
            v = 0
        return v or None

    @property
    def srv_mnt_root(self):
        """服务器上本机的挂载根。老服务器不回报这个，退回历史默认值。"""
        return self.conf.get("srv_mnt_root") or f"/root/mnt/{machine_id()}"

    @property
    def srv_status_mp(self):
        return self.conf.get("srv_status_mp") or f"/root/.winbridge/status/{machine_id()}"

    @property
    def status_dir(self):
        return os.path.join(STATUS_ROOT, self.sid)

    @staticmethod
    def is_internal(win_path):
        """状态同步用的管道目录，属于内部实现，不展示给用户"""
        try:
            return os.path.normcase(os.path.abspath(win_path)).startswith(
                os.path.normcase(os.path.abspath(STATUS_ROOT)))
        except Exception:  # noqa: BLE001
            return False

    @property
    def user_mounts(self):
        return {k: v for k, v in self.mounts.items() if not self.is_internal(k)}

    @property
    def status_mounted(self):
        return any(self.is_internal(k) for k in self.mounts)

    def ensure_status_dir(self):
        try:
            os.makedirs(self.status_dir, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

    def on_server(self, cmd, timeout=40):
        return run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                    "-o", "ClearAllForwardings=yes", self.alias, cmd], timeout)

    def register(self, realloc=False):
        """把本机身份上报给服务器，换回分配的隧道端口。

        realloc=True 时让服务器忽略存着的端口重新挑一个 —— 存的那个
        被别人占住时，不这样做就会一直拿回同一个端口、一直建不起隧道。

        用户不需要手填用户名/系统/端口 —— 这些本机自己就知道。
        服务器端幂等：已登记则返回原端口。
        """
        name = machine_id()
        args = [
            "bridge-register",
            "--name", shlex_quote(name),
            "--os", PLATFORM,
            "--user", shlex_quote(local_user()),
            "--label", shlex_quote(self.conf.get("name") or name),
            "--tool-dir", shlex_quote(BASE_DIR),
            "--status-local", shlex_quote(self.status_dir),
        ]
        if realloc:
            args.append("--realloc")
        rc, out = self.on_server(" ".join(args), 30)
        if rc != 0:
            msg = " / ".join([l.strip() for l in out.splitlines() if l.strip()][-3:])
            return None, (msg or f"ssh 返回 {rc}，无输出")
        port = None
        for line in out.splitlines():
            if line.startswith("PORT="):
                try:
                    port = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
            # 服务器那边的路径由它自己说了算 —— 角色账户下不是 /root/...，
            # 客户端拼死路径会去创建没权限的目录（实测 pc3 就栽在这）
            elif line.startswith("MNT_ROOT="):
                self.conf["srv_mnt_root"] = line.split("=", 1)[1].strip()
            elif line.startswith("STATUS_MP="):
                self.conf["srv_status_mp"] = line.split("=", 1)[1].strip()
        if port is None:
            return None, "服务器未返回端口: " + out.strip()[:120]
        if port != self.conf.get("tunnel_port"):
            self.conf["tunnel_port"] = port
        return port, None

    # ---- 隧道
    def tunnel_spawn(self):
        with self._spawn_lock:
            return self._tunnel_spawn_locked()

    def _tunnel_spawn_locked(self):
        p = self.tunnel_proc
        if p is not None and p.poll() is None:
            # 已经有一条活着的了。再起一条只会撞自己的端口，
            # 然后把 tunnel_proc 指向新的那条，旧的变成占着端口的孤儿。
            self.log(f"[{self.label}] 隧道已在运行，跳过重复建立", "warn")
            return
        port = self.port
        if port is None:
            # 还没在服务器上登记过 —— 先领一个端口再建隧道
            self.log(f"[{self.label}] 尚未分配隧道端口，先向服务器登记…")
            port, err = self.register()
            if err or not port:
                raise RuntimeError(f"无法取得隧道端口：{err or '服务器未返回'}")
            self.log(f"  已分配端口 {port}")
        args = ["ssh", "-N",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
                "-R", f"{port}:127.0.0.1:22"]
        ident = self.conf.get("identity")
        if ident:
            args += ["-i", os.path.expanduser(ident)]
        args.append(self.alias)
        # stderr 落到临时文件：进程退出时读出来放进日志，
        # 否则用户只看到 "code 255" 却不知道是主机不可达、认证失败还是转发被拒
        self._err = tempfile.NamedTemporaryFile(prefix="bridge-tunnel-", suffix=".log",
                                                delete=False, mode="w+b")
        self.tunnel_proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=self._err,
            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        self.tunnel_since = time.time()
        self.log(f"[{self.label}] 隧道命令: {' '.join(args[-3:])}")

    def tunnel_kill(self):
        p = self.tunnel_proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.tunnel_proc = None
        self.tunnel_since = None

    def tunnel_state(self):
        p = self.tunnel_proc
        if p is not None and p.poll() is None:
            up = ""
            if self.tunnel_since:
                secs = int(time.time() - self.tunnel_since)
                h, rem = divmod(secs, 3600)
                m, s = divmod(rem, 60)
                up = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            self._port_conflict = 0
            return {"ok": True, "text": f"已连接 {up}".strip(), "owner": "self"}
        if p is not None:
            code = p.returncode
            self.tunnel_proc = None
            self.tunnel_since = None
            detail = self._read_tunnel_err()
            self.log(f"[{self.label}] 隧道进程退出 (code {code})", "warn")
            for line in detail:
                self.log(f"    {line}", "error")
            blob = " ".join(detail)
            for prefix, why in self.FAKE_IP_HINTS:
                if prefix in blob:
                    self.log(f"    ⚠️ 出现 {prefix}x 段地址：{why}", "error")
                    self.log("       → 代理劫持了这条连接，放行该地址或关掉 TUN 模式", "error")
                    break
            if "remote port forwarding failed" in blob.lower():
                # 服务器上这个端口已经被占了。第一次多半是自己上一条隧道
                # 还没释放（sshd 要等旧连接完全关闭），等一下就好；
                # 连着两次还这样，就是真被别人占了，得换端口。
                self._port_conflict += 1
                self.log(f"    服务器 {self.alias} 的端口 {self.port} 已被占用"
                         f"（第 {self._port_conflict} 次）", "error")
                if self._port_conflict == 1:
                    self.log("    可能是上一条隧道还没释放，稍后重试…", "warn")
                else:
                    self.log("    将向服务器申请换一个端口", "warn")
            if code == 255 and not detail:
                self.log("    SSH 通用错误。终端手动跑一次看详情： "
                         f"ssh -N -v {self.alias}", "warn")
        if self.port_ok:
            return {"ok": True, "text": "已连接（外部）", "owner": "external"}
        return {"ok": False, "text": "未连接", "owner": None}

    FAKE_IP_HINTS = (
        ("198.18.", "RFC2544 测试网段 —— 几乎可以确定是代理/VPN 的 fake-IP 模式"),
        ("198.19.", "RFC2544 测试网段 —— 代理 fake-IP"),
        ("240.", "保留网段 —— 部分代理用它做 fake-IP"),
        ("100.64.", "CGNAT 网段 —— Tailscale/运营商 NAT，或代理"),
    )

    def diagnose(self, extra=""):
        """连接失败时把「实际生效的配置」打出来 —— 别名解析到哪、用哪把钥匙、
        以及目标 IP 是否被代理劫持。用户最常见的坑就是这两类。"""
        self.log(f"[{self.label}] ── 连接诊断 ──", "warn")

        rc, out = run(["ssh", "-G", self.alias], 15)
        cfg = {}
        for line in out.splitlines():
            k, _, v = line.strip().partition(" ")
            if k and k not in cfg:
                cfg[k] = v
        if rc != 0 or not cfg:
            self.log(f"    ssh -G {self.alias} 失败：别名可能不存在于 ~/.ssh/config", "error")
            return

        host = cfg.get("hostname", "?")
        self.log(f"    别名 {self.alias} → {cfg.get('user','?')}@{host}:{cfg.get('port','22')}")
        if host == self.alias:
            self.log(f"    ❌ ~/.ssh/config 里没有 'Host {self.alias}' 配置块！", "error")
            self.log("       SSH 把别名当成真实主机名去解析了，所以连不上。", "error")
            want = self.conf.get("host") or "<服务器IP>"
            self.log(f"       补上配置：", "error")
            self.log(f"         Host {self.alias}", "error")
            self.log(f"             HostName {want}", "error")
            self.log(f"             User root", "error")
            return
        ident = cfg.get("identityfile", "")
        if ident:
            path = os.path.expanduser(ident.strip("'\""))
            mark = "存在" if os.path.exists(path) else "❌ 文件不存在"
            if path.endswith(".pub"):
                mark = "❌ 这是公钥，应填私钥（去掉 .pub）"
            self.log(f"    私钥 {ident}  [{mark}]")

        # 目标 IP 是否被劫持
        try:
            import socket
            real = socket.gethostbyname(host)
        except Exception:  # noqa: BLE001
            real = ""
        if real and real != host:
            self.log(f"    {host} 解析为 {real}")
        probe = real or host
        for prefix, why in self.FAKE_IP_HINTS:
            if probe.startswith(prefix):
                self.log(f"    ⚠️ 目标 IP {probe} 落在 {prefix}x 段：{why}", "error")
                self.log("       → 让代理放行这个地址，或临时关掉 TUN/增强模式再试", "error")
                break

        if extra:
            for prefix, why in self.FAKE_IP_HINTS:
                if prefix in extra:
                    self.log(f"    ⚠️ 错误信息里出现 {prefix}x 段地址：{why}", "error")
                    self.log("       → 代理把连接劫持走了，放行该地址或关掉 TUN 模式", "error")
                    break

        self.log(f"    手动验证： ssh -N -v {self.alias}", "warn")

    def _read_tunnel_err(self):
        """取隧道进程的 stderr（最多 6 行有效信息）"""
        f = getattr(self, "_err", None)
        if f is None:
            return []
        try:
            f.flush()
            f.seek(0)
            raw = f.read().decode("utf-8", "replace")
            f.close()
            os.unlink(f.name)
        except Exception:  # noqa: BLE001
            raw = ""
        finally:
            self._err = None
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        # 过滤掉无信息量的噪音
        drop = ("Warning: Permanently added", "debug1:", "debug2:", "debug3:")
        lines = [l for l in lines if not l.startswith(drop)]
        return lines[-6:]

    # ---- 状态
    def read_status_file(self):
        fp = os.path.join(self.status_dir, ".bridge-status.json")
        try:
            with open(fp, encoding="utf-8") as fh:
                data = json.load(fh)
            age = time.time() - float(data.get("ts", 0))
            if 0 <= age < 12:
                return data, age
        except Exception:  # noqa: BLE001
            pass
        return None, None

    def poll(self):
        """优先读本地状态文件（零 SSH）；读不到再降级 SSH 探测（限流）"""
        data, age = self.read_status_file()
        if data is not None:
            with self.lock:
                self.port_ok = bool(data.get("port_ok"))
                self.state["server"] = {"ok": True, "text": "已连接"}
                self.state["source"] = "local"
                self.state["fresh_s"] = age
                self.state["uptime"] = (data.get("uptime") or "").replace("@", '"') or None
                found = {}
                for m in data.get("mounts", []):
                    src, mp = m.get("src", ""), m.get("mount", "")
                    if src and mp:
                        found[from_sftp_path(src)] = mp
                self.mounts = found
            self._probes = 0
            self._warned = False
            self._fixed_pipe = False
            return

        now = time.time()
        if now - self._last_ssh < 15:
            with self.lock:
                self.state["source"] = "stale"
                self.state["fresh_s"] = None
            return
        self._last_ssh = now
        self._probes += 1
        if self._probes == 3 and not self._warned:
            self._warned = True
            self.log(f"[{self.label}] 状态文件读不到，回退 SSH 探测；"
                     f"检查服务器是否挂载了状态目录 "
                     f"{os.path.join('status', self.sid)}", "warn")
        self.poll_ssh()

    def poll_ssh(self):
        port = self.port or 0
        cmd = (f"win-statusd start >/dev/null 2>&1; "
               f"ss -tln 2>/dev/null | grep -q ':{port} ' && echo PORT_OK || echo PORT_NO; "
               f"uptime -p 2>/dev/null; echo ---; "
               f"bridge-mounts -c {machine_id()} 2>/dev/null")
        rc, out = self.on_server(cmd)
        with self.lock:
            if rc != 0:
                self.port_ok = False
                self.state["server"] = {"ok": False, "text": "不可达"}
                self.state["source"] = "ssh"
                self.state["fresh_s"] = None
                self.state["uptime"] = None
                self.mounts = {}
                return
            head, _, tail = out.partition("---")
            self.port_ok = "PORT_OK" in head
            self.state["server"] = {"ok": True, "text": "已连接"}
            self.state["source"] = "ssh"
            self.state["fresh_s"] = None
            self.state["uptime"] = next(
                (l.strip() for l in head.splitlines() if l.strip().startswith("up ")), None)
            found = {}
            for line in tail.splitlines():
                if "\t" in line:
                    mp, src = line.split("\t", 1)
                    found[from_sftp_path(src.strip())] = mp.strip()
            self.mounts = found


# ================================================================ 接入码对话框



# ================================================================ 服务器编辑对话框







# ================================================================ 应用
