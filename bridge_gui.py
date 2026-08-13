#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
桥接控制台 — 桌面客户端（多服务器版）

管理 Windows <-> 多台 Linux 服务器 的反向 SSH 隧道与目录挂载。
只用 Python 标准库（tkinter），无需安装任何依赖。

用法:  pythonw bridge_gui.py                  无控制台窗口
       python  bridge_gui.py                  带控制台，便于看报错
       pythonw bridge_gui.py --auto-tunnel    启动后自动建立标记了 auto_tunnel 的隧道
       pythonw bridge_gui.py --minimized      最小化启动
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
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

APP_NAME = "桥接控制台"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE_DIR, "bridge-config.json")
STATUS_ROOT = os.path.join(BASE_DIR, "status")

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


def apply_theme(name):
    """把配色写进模块级常量 —— 界面代码到处直接引用它们"""
    t = THEMES.get(name) or THEMES["dark"]
    g = globals()
    for k, v in t.items():
        g["C_" + k] = v
    g["THEME"] = name if name in THEMES else "dark"


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


def sshd_status():
    """返回 (ok, 文案)"""
    if IS_WIN:
        rc, out = run(["powershell", "-NoLogo", "-NonInteractive", "-Command",
                       "(Get-Service sshd -ErrorAction SilentlyContinue).Status"])
        st = out.strip()
        zh = {"Running": "运行中", "Stopped": "已停止",
              "StartPending": "启动中", "StopPending": "停止中"}
        return (st == "Running", zh.get(st, st) or "未安装")
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


def set_window_icon(win):
    """给窗口贴上应用图标。找不到图标文件就安静跳过。"""
    try:
        ico = os.path.join(BASE_DIR, "assets", "ferry.ico")
        if IS_WIN and os.path.exists(ico):
            win.iconbitmap(default=ico)         # default= 让所有子窗口一起继承
            return
        png = os.path.join(BASE_DIR, "assets", "ferry.png")
        if os.path.exists(png):
            img = tk.PhotoImage(file=png)       # Tk 8.6 起原生认 PNG
            _ICON_REF.append(img)
            win.iconphoto(True, img)
    except Exception:  # noqa: BLE001
        pass


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
        ps = ("$g=(Get-LocalGroup -SID 'S-1-5-32-544').Name;"
              "$a=[bool](Get-LocalGroupMember -Group $g -EA SilentlyContinue|"
              "Where-Object{$_.Name -like \"*\\$env:USERNAME\"});"
              "if($a){$f='C:\\ProgramData\\ssh\\administrators_authorized_keys'}"
              "else{New-Item -ItemType Directory -Force -Path \"$env:USERPROFILE\\.ssh\"|Out-Null;"
              "$f=\"$env:USERPROFILE\\.ssh\\authorized_keys\"};"
              f"$k='{d['srv_pub']}';"
              "if((Test-Path $f) -and (Select-String -Path $f -SimpleMatch $k -Quiet)){'SKIP'}"
              "else{Add-Content $f $k -Encoding ASCII;'ADDED'};"
              "if($a){icacls $f /inheritance:r /grant \"${g}:F\" /grant 'SYSTEM:F'|Out-Null};"
              "Restart-Service sshd -EA SilentlyContinue")
        rc, out = run(["powershell", "-NoLogo", "-NonInteractive", "-Command", ps], 60)
        if "ADDED" in out:
            tips.append("服务器公钥已授权到本机")
        elif "SKIP" in out:
            tips.append("服务器公钥已存在，跳过")
        else:
            tips.append("⚠️ 授权公钥失败，可能需要管理员权限运行本程序")
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

class InviteDialog(tk.Toplevel):
    """粘贴接入码 —— 添加服务器的默认方式"""

    def __init__(self, parent, fonts):
        super().__init__(parent)
        self.result = None
        self.title("粘贴接入码")
        self.transient(parent)
        self.resizable(False, False)
        self.configure(bg=C_CARD)

        f_base, f_sub, f_mono, f_big = fonts
        wrap = tk.Frame(self, bg=C_CARD, padx=22, pady=18)
        wrap.pack(fill="both", expand=True)

        tk.Label(wrap, text="粘贴接入码", bg=C_CARD, fg=C_TEXT,
                 font=f_big).pack(anchor="w")
        tk.Label(wrap, bg=C_CARD, fg=C_MUTED, font=f_sub, justify="left",
                 text="在服务器上执行  bridge-invite  获取，把输出的整段粘到下面。\n"
                      "它会自动完成：授权服务器公钥、保存登录密钥、写 SSH 别名。").pack(
            anchor="w", pady=(4, 12))

        self.txt = tk.Text(wrap, width=64, height=7, font=f_mono, wrap="char",
                           relief="flat", bd=1, highlightthickness=1,
                           highlightbackground=C_LINE, bg=C_INPUT, fg=C_TEXT,
                           insertbackground=C_TEXT, selectbackground=C_SEL)
        self.txt.pack(fill="both", expand=True)
        self.txt.focus_set()

        tk.Label(wrap, bg=C_CARD, fg=C_WARN, font=f_sub, justify="left",
                 text="⚠️ 接入码包含服务器登录凭据，勿外传。").pack(anchor="w", pady=(8, 0))

        bar = tk.Frame(wrap, bg=C_CARD)
        bar.pack(fill="x", pady=(14, 0))
        tk.Button(bar, text="取消", command=self.destroy, relief="flat", bd=0,
                  bg=C_CARD, fg=C_MUTED, activebackground=C_CARD, cursor="hand2",
                  padx=14, pady=6, font=f_base).pack(side="right")
        tk.Button(bar, text="接入", command=self._go, relief="flat", bd=0,
                  bg=C_ACCENT, fg="#ffffff", activebackground=C_ACCENT_H,
                  activeforeground="#fff", cursor="hand2", padx=22, pady=6,
                  font=f_base).pack(side="right", padx=(0, 8))
        tk.Button(bar, text="从剪贴板读取", command=self._paste, relief="flat", bd=0,
                  bg=C_CARD, fg=C_ACCENT, activebackground=C_CARD, cursor="hand2",
                  padx=10, pady=6, font=f_sub).pack(side="left")

        self.grab_set()
        self.wait_window(self)

    def _paste(self):
        try:
            self.txt.delete("1.0", "end")
            self.txt.insert("1.0", self.clipboard_get())
        except Exception:  # noqa: BLE001
            messagebox.showinfo("粘贴接入码", "剪贴板里没有文本。", parent=self)

    def _go(self):
        raw = self.txt.get("1.0", "end").strip()
        if not raw:
            return
        try:
            self.result = parse_invite(raw)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("粘贴接入码", f"接入码无效：{exc}", parent=self)
            return
        self.destroy()


# ================================================================ 服务器编辑对话框

class RemoteBrowser(tk.Toplevel):
    """服务器端目录选择器。

    只调 bridge-ls（单层列目录），永远不递归 —— 在挂载目录上做全树遍历
    会把隧道堵死，所有使用者一起卡。
    """

    def __init__(self, parent, srv, start):
        super().__init__(parent)
        self.result = None
        self.srv = srv
        self.cwd = start or "/"
        self.q = queue.Queue()
        self.title(f"选择 {srv.label} 上的位置")
        self.transient(parent)
        self.minsize(460, 340)

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(1, weight=1)

        top = ttk.Frame(frm)
        top.grid(row=0, column=0, sticky="we")
        self.path_var = tk.StringVar(value=self.cwd)
        ttk.Entry(top, textvariable=self.path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="转到", width=6,
                   command=lambda: self._load(self.path_var.get().strip())).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="上一层", width=8, command=self._up).pack(side="left", padx=(6, 0))

        self.lst = tk.Listbox(frm, activestyle="none", highlightthickness=0,
                              bg=C_INPUT, fg=C_TEXT, selectbackground=C_SEL,
                              selectforeground=C_TEXT, bd=0, relief="flat")
        self.lst.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.lst.bind("<Double-1>", lambda _e: self._enter())

        self.hint = ttk.Label(frm, text="", style="Sub.TLabel")
        self.hint.grid(row=2, column=0, sticky="w", pady=(6, 0))

        bar = ttk.Frame(frm)
        bar.grid(row=3, column=0, sticky="e", pady=(10, 0))
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="选定当前目录", command=self._pick).pack(side="right")
        ttk.Button(bar, text="新建子目录…", command=self._mkdir).pack(side="right", padx=(0, 6))

        self._load(self.cwd)
        self.grab_set()
        self.wait_window(self)

    # -- 后台取目录，主线程轮询，避免 SSH 往返把窗口卡住
    def _load(self, path):
        self.lst.delete(0, "end")
        self.lst.insert("end", "  读取中…")
        self.hint.configure(text="")

        def work():
            rc, out = self.srv.on_server(
                f"bridge-ls -c {machine_id()} {shlex_quote(path)}", 30)
            self.q.put((rc, out))
        threading.Thread(target=work, daemon=True).start()
        self.after(120, self._drain)

    def _drain(self):
        try:
            rc, out = self.q.get_nowait()
        except queue.Empty:
            self.after(120, self._drain)
            return
        self.rows = []
        cwd = None
        for line in out.splitlines():
            if line.startswith("CWD|"):
                cwd = line[4:].strip()
            elif line.startswith("D|"):
                parts = line.split("|")
                self.rows.append((parts[1], parts[2] if len(parts) > 2 else "free"))
        if cwd is None:
            self.lst.delete(0, "end")
            self.hint.configure(text=(out.strip().split("|")[-1] or "读取失败")[:90])
            return
        self.cwd = cwd
        self.path_var.set(cwd)
        self.lst.delete(0, "end")
        for name, flag in self.rows:
            self.lst.insert("end", f"  {name}" + ("      〔已挂载〕" if flag == "mounted" else ""))
        if not self.rows:
            self.hint.configure(text="（空目录 —— 可以直接「选定当前目录」）")
        else:
            self.hint.configure(text="双击进入下一层；「已挂载」的目录不能重复占用")

    def _join(self, name):
        return (self.cwd.rstrip("/") + "/" + name) or "/"

    def _enter(self):
        sel = self.lst.curselection()
        if sel and getattr(self, "rows", None) and sel[0] < len(self.rows):
            self._load(self._join(self.rows[sel[0]][0]))

    def _up(self):
        parent = os.path.dirname(self.cwd.rstrip("/")) or "/"
        self._load(parent)

    def _mkdir(self):
        name = simpledialog.askstring("新建子目录", "目录名：", parent=self)
        if not name:
            return
        name = name.strip().strip("/")
        if not name:
            return
        # 只是选个位置，真正的目录由 bridge-mount 挂载时创建
        self.result = self._join(name)
        self.destroy()

    def _pick(self):
        sel = self.lst.curselection()
        if sel and getattr(self, "rows", None) and sel[0] < len(self.rows):
            name, flag = self.rows[sel[0]]
            if flag == "mounted":
                messagebox.showwarning(APP_NAME, f"{name} 已经挂着别的目录了，换一个。", parent=self)
                return
            self.result = self._join(name)
        else:
            self.result = self.cwd
        self.destroy()


class MountDialog(tk.Toplevel):
    """挂载一个文件夹：本机目录 + 服务器上放在哪，两边都可以自己选。"""

    def __init__(self, parent, srv, local="", server="", title="挂载文件夹"):
        super().__init__(parent)
        self.result = None
        self.srv = srv
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        self.v_local = tk.StringVar(value=local)
        self.v_server = tk.StringVar(value=server)
        # 用户手动改过服务器位置后，就别再跟着本机目录自动变了
        self.server_touched = bool(server)

        ttk.Label(frm, text="本机目录").grid(row=0, column=0, sticky="w")
        e1 = ttk.Entry(frm, textvariable=self.v_local, width=46)
        e1.grid(row=0, column=1, sticky="we", padx=(10, 6))
        ttk.Button(frm, text="浏览…", width=8, command=self._pick_local).grid(row=0, column=2)
        ttk.Label(frm, text="要挂到服务器上的本机文件夹",
                  style="Sub.TLabel").grid(row=1, column=1, sticky="w", padx=(10, 0))

        ttk.Label(frm, text="服务器位置").grid(row=2, column=0, sticky="w", pady=(12, 0))
        e2 = ttk.Entry(frm, textvariable=self.v_server, width=46)
        e2.grid(row=2, column=1, sticky="we", padx=(10, 6), pady=(12, 0))
        ttk.Button(frm, text="浏览…", width=8,
                   command=self._pick_server).grid(row=2, column=2, pady=(12, 0))
        self.lbl_hint = ttk.Label(frm, text="", style="Sub.TLabel")
        self.lbl_hint.grid(row=3, column=1, sticky="w", padx=(10, 0))

        self.v_local.trace_add("write", lambda *_a: self._sync_default())
        self.v_server.trace_add("write", lambda *_a: self._refresh_hint())
        e2.bind("<Key>", lambda _e: setattr(self, "server_touched", True))

        bar = ttk.Frame(frm)
        bar.grid(row=9, column=0, columnspan=3, sticky="e", pady=(18, 0))
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="恢复默认位置", command=self._reset).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="挂载", command=self._ok).pack(side="right")

        self._refresh_hint()
        (e1 if not local else e2).focus_set()
        self.grab_set()
        self.wait_window(self)

    # ---- 默认位置：服务器报回来的挂载根 + 路径转成的名字
    def _default_target(self):
        local = self.v_local.get().strip()
        if not local:
            return ""
        return f"{self.srv.srv_mnt_root}/{default_mount_dir(local)}"

    def _sync_default(self):
        if not self.server_touched:
            self.v_server.set(self._default_target())
        self._refresh_hint()

    def _reset(self):
        self.server_touched = False
        self.v_server.set(self._default_target())

    def _refresh_hint(self):
        target = self.v_server.get().strip()
        if not target:
            self.lbl_hint.configure(text="留空则用默认位置")
        elif target == self._default_target():
            self.lbl_hint.configure(text="默认位置（每台机器一个独立的挂载根，不会互相撞名）")
        else:
            self.lbl_hint.configure(text="自定义位置 —— 必须是绝对路径，且是空目录或还不存在")

    def _pick_local(self):
        path = filedialog.askdirectory(title="选择本机文件夹", mustexist=True, parent=self)
        if path:
            self.v_local.set(os.path.normpath(path))

    def _pick_server(self):
        if not self.srv.port_ok:
            messagebox.showinfo(APP_NAME, "隧道没连上，浏览不了服务器目录。\n可以直接手填绝对路径。",
                                parent=self)
            return
        cur = self.v_server.get().strip()
        start = os.path.dirname(cur.rstrip("/")) if cur.startswith("/") else self.srv.srv_mnt_root
        dlg = RemoteBrowser(self, self.srv, start or "/")
        if dlg.result:
            self.server_touched = True
            self.v_server.set(dlg.result)

    def _ok(self):
        local = self.v_local.get().strip()
        target = self.v_server.get().strip()
        if not local:
            messagebox.showwarning(APP_NAME, "先选一个本机目录。", parent=self)
            return
        local = os.path.normpath(local)
        if not os.path.isdir(local):
            messagebox.showwarning(APP_NAME, f"本机目录不存在：\n{local}", parent=self)
            return
        if Server.is_internal(local):
            messagebox.showinfo(APP_NAME, "这是控制台的内部状态目录，不需要手动挂载。", parent=self)
            return
        if target and not target.startswith("/"):
            messagebox.showwarning(APP_NAME, "服务器位置必须是绝对路径，以 / 开头。", parent=self)
            return
        if target == self._default_target():
            target = ""            # 跟默认一样就别记死，换机器名时还能自动跟着走
        self.result = (local, target)
        self.destroy()


class ServerDialog(tk.Toplevel):
    FIELDS = [
        ("ssh_alias",   "SSH 别名",       "~/.ssh/config 里的 Host 名，如 myserver（唯一必填项）"),
        ("name",        "显示名称(可选)", "留空则用别名"),
        ("host",        "服务器地址", "IP 或域名；SSH 别名缺配置时用它自动补全"),
        ("identity",    "私钥路径(可选)", "留空则用 SSH 别名里配置的"),
    ]
    # 用户名/系统/隧道端口由客户端启动隧道时自动上报给服务器，无需手填

    def __init__(self, parent, conf=None):
        super().__init__(parent)
        self.result = None
        self.conf = dict(conf or {})
        self.title("编辑服务器" if conf else "添加服务器")
        self.transient(parent)
        self.resizable(False, False)

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        self.vars = {}
        for i, (key, label, hint) in enumerate(self.FIELDS):
            ttk.Label(frm, text=label).grid(row=i * 2, column=0, sticky="w", pady=(6, 0))
            v = tk.StringVar(value=str(self.conf.get(key, "") or ""))
            self.vars[key] = v
            ttk.Entry(frm, textvariable=v, width=44).grid(
                row=i * 2, column=1, sticky="we", pady=(6, 0), padx=(10, 0))
            ttk.Label(frm, text=hint, style="Sub.TLabel").grid(
                row=i * 2 + 1, column=1, sticky="w", padx=(10, 0))

        self.auto = tk.BooleanVar(value=bool(self.conf.get("auto_tunnel", True)))
        ttk.Checkbutton(frm, text="保持连接（启动时自动建立，断线后持续自动重连）",
                        variable=self.auto).grid(row=99, column=1, sticky="w",
                                                 padx=(10, 0), pady=(12, 0))
        bar = ttk.Frame(frm)
        bar.grid(row=100, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="保存", command=self._save).pack(side="right")

        self.grab_set()
        self.wait_window(self)

    def _append_ssh_config(self, alias, host):
        """把缺失的 Host 块追加到 ~/.ssh/config（纯追加，先备份）"""
        try:
            cfg = os.path.join(os.path.expanduser("~"), ".ssh", "config")
            os.makedirs(os.path.dirname(cfg), exist_ok=True)
            if os.path.exists(cfg):
                shutil.copy2(cfg, cfg + ".bak")
            with open(cfg, "a", encoding="utf-8") as fh:
                fh.write(f"\nHost {alias}\n"
                         f"    HostName {host}\n"
                         f"    User root\n"
                         f"    ServerAliveInterval 30\n"
                         f"    ServerAliveCountMax 3\n"
                         f"    StrictHostKeyChecking accept-new\n")
            try:
                os.chmod(cfg, 0o600)
            except OSError:
                pass
            messagebox.showinfo(
                APP_NAME,
                f"已在 ~/.ssh/config 追加：\n\nHost {alias}\n    HostName {host}\n    User root\n\n"
                f"若该服务器用非 root 账户或需要指定私钥，请自行编辑该文件。",
                parent=self)
            return True
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"写入 ~/.ssh/config 失败：{exc}", parent=self)
            return False

    def _save(self):
        vals = {k: v.get().strip() for k, v in self.vars.items()}
        if not vals["ssh_alias"]:
            messagebox.showwarning(
                APP_NAME, "SSH 别名必填 —— 它必须是你 ~/.ssh/config 里已配置的 Host。",
                parent=self)
            return
        # 别名必须在 ~/.ssh/config 里真实存在，否则 SSH 会把它当主机名解析，
        # 用户会看到一串莫名其妙的连接失败。这里当场拦住。
        rc, gout = run(["ssh", "-G", vals["ssh_alias"]], 15)
        resolved = ""
        for line in gout.splitlines():
            if line.startswith("hostname "):
                resolved = line.split(None, 1)[1].strip()
                break
        if resolved == vals["ssh_alias"]:
            tip = (f"~/.ssh/config 里找不到 “Host {vals['ssh_alias']}”。\n\n"
                   f"SSH 会把别名当成真实主机名去解析，多半连不上。\n\n")
            if vals["host"]:
                tip += (f"要用你填的服务器地址 {vals['host']} 自动补一段配置吗？\n"
                        f"（会追加到 ~/.ssh/config，不影响已有内容）")
                if messagebox.askyesno(APP_NAME, tip, parent=self):
                    if not self._append_ssh_config(vals["ssh_alias"], vals["host"]):
                        return
                else:
                    return
            else:
                messagebox.showwarning(
                    APP_NAME, tip + "请先在 ~/.ssh/config 里配好该 Host，或填写「服务器地址」让我代劳。",
                    parent=self)
                return

        out = dict(self.conf)          # 保留已分配的 tunnel_port 等运行期字段
        out.update({
            "id": self.conf.get("id") or vals["ssh_alias"],
            "name": vals["name"] or vals["ssh_alias"],
            "ssh_alias": vals["ssh_alias"],
            "host": vals["host"],
            # tunnel_port 故意不在这里写：新建时若写成 None，
            # conf.get(k, default) 会返回 None 而不是默认值，拼出 -R None:...
            # 端口一律由 bridge-register 分配后回填。
            "win_user": local_user(),
            "identity": vals["identity"] or None,
            "auto_tunnel": bool(self.auto.get()),
            "enabled": True,
            "mounts": self.conf.get("mounts", []),
        })
        self.result = out
        self.destroy()


# ================================================================ 应用

class BridgeApp:
    def __init__(self, root, auto_tunnel=False, start_minimized=False):
        self.root = root
        self.auto_tunnel = auto_tunnel
        self.msgq = queue.Queue()
        self.servers = []
        self.sshd = {"ok": None, "text": "检测中"}
        self.busy = False
        self.update_ready = False

        self._build_ui()
        self.log(f"{APP_NAME} 启动")
        self._load_servers()
        self._rebuild_server_list()     # 建界面时还没加载服务器，这里补一次

        threading.Thread(target=self._poll_local_loop, daemon=True).start()
        threading.Thread(target=self._poll_remote_loop, daemon=True).start()
        threading.Thread(target=self._watch_version, daemon=True).start()
        self.root.after(200, self._drain)
        self.root.after(500, self._refresh_ui)

        if start_minimized:
            self.root.iconify()
        if not self.servers:
            self.root.after(700, self._first_run)
        elif self.auto_tunnel:
            threading.Thread(target=self._auto_boot, daemon=True).start()

    # ------------------------------------------------------------ 服务器集合

    def _load_servers(self):
        self.servers = []
        for conf in CFG.get("servers", []):
            srv = Server(conf, self.log)
            srv.ensure_status_dir()
            self.servers.append(srv)
        if self.servers:
            self.log("已加载 " + str(len(self.servers)) + " 台服务器: "
                     + "、".join(s.label for s in self.servers))

    def _first_run(self):
        messagebox.showinfo(
            APP_NAME,
            "首次使用：先添加一台服务器。\n\n"
            "前提是你已经能用 ssh <别名> 连上它\n"
            "（即 ~/.ssh/config 里配好了 Host）。\n\n"
            "接下来填写的「SSH 别名」就是那个 Host 名。")
        self.act_add_server()

    def current(self):
        """当前选中的服务器；选中项失效时退回第一台"""
        m = next((s for s in self.servers if s.sid == getattr(self, "sel_sid", None)), None)
        if m:
            return m
        return self.servers[0] if self.servers else None

    def _persist(self):
        CFG["servers"] = [s.conf for s in self.servers]
        cur = self.current()
        if cur:
            CFG["active"] = cur.sid
        save_cfg(CFG)

    # ------------------------------------------------------------ 界面

    def _build_ui(self):
        r = self.root
        r.title(APP_NAME)
        r.protocol("WM_DELETE_WINDOW", self._on_close)
        r.configure(bg=C_BG)

        fs = int(CFG.get("font_size", 11))
        df, dm = default_fonts()
        fam = CFG.get("font_family") or df
        mono = CFG.get("mono_family") or dm
        self.f_base, self.f_bold = (fam, fs), (fam, fs, "bold")
        self.f_head = (fam, fs + 8, "bold")
        self.f_sub = (fam, max(fs - 1, 8))
        self.f_big = (fam, fs + 3, "bold")
        self.f_mono = (mono, max(fs - 1, 9))

        w, h = int(78 * fs + 300), int(50 * fs + 220)
        r.geometry(f"{w}x{h}")
        # 最小宽度按「侧栏 + 挂载表 + 工具条」的实际需要给，再窄按钮就会被挤掉
        r.minsize(int(fs * 62 + 260), int(h * 0.7))
        r.option_add("*Font", self.f_base)

        st = ttk.Style()
        # clam 必须排第一：vista/winnative 会忽略 background，暗色主题下按钮
        # 和输入框会留一块刺眼的白
        for theme in ("clam", "vista", "winnative", "default"):
            if theme in st.theme_names():
                st.theme_use(theme)
                break
        st.configure(".", font=self.f_base, background=C_BG, foreground=C_TEXT)
        st.configure("TFrame", background=C_BG)
        st.configure("Card.TFrame", background=C_CARD, relief="flat")
        st.configure("Card2.TFrame", background=C_CARD2, relief="flat")
        st.configure("TLabel", background=C_CARD, foreground=C_TEXT)
        st.configure("Bg.TLabel", background=C_BG, foreground=C_TEXT)
        st.configure("Sub.TLabel", background=C_CARD, foreground=C_MUTED, font=self.f_sub)
        st.configure("SubBg.TLabel", background=C_BG, foreground=C_MUTED, font=self.f_sub)
        st.configure("Sub2.TLabel", background=C_CARD2, foreground=C_MUTED, font=self.f_sub)
        st.configure("Val2.TLabel", background=C_CARD2, foreground=C_TEXT)
        st.configure("Head.TLabel", background=C_BG, foreground=C_TEXT, font=self.f_head)
        st.configure("Big.TLabel", background=C_CARD, foreground=C_TEXT, font=self.f_big)

        st.configure("TButton", font=self.f_base, padding=(12, 7),
                     background=C_CARD2, foreground=C_TEXT,
                     bordercolor=C_LINE, lightcolor=C_CARD2, darkcolor=C_CARD2,
                     focuscolor=C_ACCENT, relief="flat")
        st.map("TButton",
               background=[("disabled", C_CARD), ("pressed", C_SEL), ("active", C_SEL)],
               foreground=[("disabled", C_NA)],
               lightcolor=[("active", C_SEL)], darkcolor=[("active", C_SEL)])
        st.configure("Tool.TButton", padding=(9, 5))
        st.configure("Accent.TButton", font=self.f_bold, padding=(11, 5),
                     background=C_ACCENT, foreground="#ffffff",
                     lightcolor=C_ACCENT, darkcolor=C_ACCENT)
        st.map("Accent.TButton",
               background=[("active", C_ACCENT_H), ("pressed", C_ACCENT_H)],
               lightcolor=[("active", C_ACCENT_H)], darkcolor=[("active", C_ACCENT_H)])

        st.configure("Treeview", font=self.f_base, rowheight=int(fs * 2.5),
                     background=C_CARD, fieldbackground=C_CARD,
                     foreground=C_TEXT, borderwidth=0, relief="flat",
                     bordercolor=C_CARD, lightcolor=C_CARD, darkcolor=C_CARD)
        st.map("Treeview", background=[("selected", C_SEL)],
               foreground=[("selected", C_TEXT)])
        st.configure("Treeview.Heading", font=self.f_sub, background=C_CARD2,
                     foreground=C_MUTED, relief="flat", padding=(6, 6),
                     borderwidth=0)
        st.map("Treeview.Heading", background=[("active", C_CARD2)])

        # 输入框：clam 下这几个键才管得住边框，少一个就会露出浅色描边
        st.configure("TEntry", fieldbackground=C_INPUT, foreground=C_TEXT,
                     insertcolor=C_TEXT, bordercolor=C_LINE,
                     lightcolor=C_LINE, darkcolor=C_LINE, padding=4)
        st.map("TEntry", bordercolor=[("focus", C_ACCENT)],
               lightcolor=[("focus", C_ACCENT)], darkcolor=[("focus", C_ACCENT)])
        st.configure("TCheckbutton", background=C_CARD, foreground=C_TEXT,
                     indicatorcolor=C_INPUT, focuscolor=C_ACCENT)
        st.map("TCheckbutton", indicatorcolor=[("selected", C_ACCENT)],
               background=[("active", C_CARD)])
        st.configure("TSeparator", background=C_LINE)
        st.configure("Vertical.TScrollbar", background=C_CARD2, troughcolor=C_BG,
                     bordercolor=C_BG, arrowcolor=C_MUTED, relief="flat")
        st.map("Vertical.TScrollbar", background=[("active", C_LINE)])
        # 弹窗默认底色跟着主题走，否则新窗口会闪出系统灰。
        # 下面几条是给 simpledialog 这类用经典 tk 控件的内置对话框兜底 ——
        # 不加的话暗色主题下会弹出一个白底黑字的框。自己 configure 过的控件不受影响。
        r.option_add("*Toplevel.background", C_CARD)
        r.option_add("*Label.background", C_CARD)
        r.option_add("*Label.foreground", C_TEXT)
        r.option_add("*Frame.background", C_CARD)
        r.option_add("*Entry.background", C_INPUT)
        r.option_add("*Entry.foreground", C_TEXT)
        r.option_add("*Entry.insertBackground", C_TEXT)
        r.option_add("*Button.background", C_CARD2)
        r.option_add("*Button.foreground", C_TEXT)
        r.option_add("*Button.activeBackground", C_SEL)
        r.option_add("*Button.activeForeground", C_TEXT)

        pad = int(fs * 1.5)
        outer = ttk.Frame(r, padding=(pad, pad, pad, int(pad * 0.6)))
        outer.pack(fill="both", expand=True)

        def card(parent, **kw):
            """统一的卡片容器：白底 + 细边 + 内边距"""
            box = tk.Frame(parent, bg=C_CARD, highlightbackground=C_LINE,
                           highlightthickness=1, bd=0)
            inner = ttk.Frame(box, style="Card.TFrame",
                              padding=kw.pop("padding", (pad, int(pad * 0.8))))
            inner.pack(fill="both", expand=True)
            return box, inner

        def flat_btn(parent, text, cmd, fg=C_MUTED, hover=None, bg=C_BG, **kw):
            return tk.Button(parent, text=text, command=cmd, relief="flat", bd=0,
                             bg=bg, fg=fg, activebackground=bg,
                             activeforeground=hover or C_ACCENT, cursor="hand2",
                             highlightthickness=0, font=self.f_sub, **kw)
        self.flat_btn = flat_btn

        # ═══════════ 顶栏：标题 · 本机 SSH · 全局动作 ═══════════
        top = ttk.Frame(outer)
        top.pack(fill="x", pady=(0, pad))
        ttk.Label(top, text="Ferry", style="Head.TLabel").pack(side="left")
        ttk.Label(top, text="桥接控制台", style="SubBg.TLabel").pack(
            side="left", padx=(8, 0), pady=(6, 0))

        self.btn_reload = flat_btn(top, "重载", self.act_reload, padx=10, pady=4)
        self.btn_reload.pack(side="right")
        self.btn_theme = flat_btn(top, "☀ 浅色" if THEME == "dark" else "☾ 深色",
                                  self.act_theme, padx=10, pady=4)
        self.btn_theme.pack(side="right", padx=(0, 4))
        self.btn_panic = flat_btn(top, "紧急断开", self.act_panic,
                                  fg=C_BAD, hover="#ff8b8f", padx=10, pady=4)
        self.btn_panic.pack(side="right", padx=(0, 10))

        # 本机 sshd 是全局的，不属于任何一台服务器，放顶栏
        self.dots, self.vals = {}, {}
        sshd_box = ttk.Frame(top)
        sshd_box.pack(side="right", padx=(0, int(pad * 1.6)))
        d = tk.Label(sshd_box, text="●", fg=C_NA, bg=C_BG, font=(fam, fs))
        d.pack(side="left")
        ttk.Label(sshd_box, text="本机 SSH", style="SubBg.TLabel").pack(side="left", padx=(4, 5))
        v = ttk.Label(sshd_box, text="—", style="Bg.TLabel")
        v.pack(side="left")
        self.dots["sshd"], self.vals["sshd"] = d, v
        # 没跑的时候才出现 —— 服务器全靠它回连，停着的话什么都挂不上
        self.btn_sshd = flat_btn(sshd_box, "启动", self.act_start_sshd,
                                 fg=C_WARN, hover=C_ACCENT, padx=6, pady=1)

        # ═══════════ 主体：左服务器 · 右挂载与日志 ═══════════
        sidew = int(fs * 25)
        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0, minsize=sidew)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self.btns = {}

        # ---------- 左栏 ----------
        left_box, left = card(body, padding=(int(pad * 0.7), int(pad * 0.7)))
        left_box.grid(row=0, column=0, sticky="nsew", padx=(0, pad))
        left_box.configure(width=sidew)
        # 里面的子控件是 pack 布局的，必须 pack_propagate ——
        # grid_propagate 对它们无效，侧栏会撑到内容那么宽，把右边挤没
        left_box.pack_propagate(False)

        lh = ttk.Frame(left, style="Card.TFrame")
        lh.pack(fill="x", pady=(0, int(pad * 0.5)))
        ttk.Label(lh, text="服务器", style="Big.TLabel").pack(side="left")
        b = flat_btn(lh, "＋ 添加", self.act_add_server, bg=C_CARD, padx=4, pady=2)
        b.pack(side="right")
        self.btns["s_add"] = b

        # 服务器多了要能滚，所以套一层 canvas
        wrap = tk.Frame(left, bg=C_CARD, highlightthickness=0)
        wrap.pack(fill="both", expand=True)
        self.srv_canvas = tk.Canvas(wrap, bg=C_CARD, highlightthickness=0, bd=0)
        self.srv_sc = ttk.Scrollbar(wrap, orient="vertical", command=self.srv_canvas.yview)
        self.srv_canvas.configure(yscrollcommand=self._srv_scroll_set)
        self.srv_canvas.pack(side="left", fill="both", expand=True)
        self.srv_host = ttk.Frame(self.srv_canvas, style="Card.TFrame")
        self._srv_win = self.srv_canvas.create_window((0, 0), window=self.srv_host, anchor="nw")
        self.srv_host.bind("<Configure>", lambda _e: self.srv_canvas.configure(
            scrollregion=self.srv_canvas.bbox("all")))
        self.srv_canvas.bind("<Configure>", lambda e: self.srv_canvas.itemconfigure(
            self._srv_win, width=e.width))
        for w in (self.srv_canvas, self.srv_host):
            w.bind("<MouseWheel>", self._srv_wheel)          # Windows / macOS
            w.bind("<Button-4>", self._srv_wheel)            # X11 上滚
            w.bind("<Button-5>", self._srv_wheel)

        self.srv_rows = {}          # sid -> 该行的控件引用
        self.expanded = set()       # 展开了详情的 sid
        self.sel_sid = CFG.get("active")
        self.dyn_btns = []          # 服务器行里动态生成的按钮，也要跟着忙碌态禁用

        # ---------- 右栏 ----------
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        box2, mbox = card(right)
        box2.grid(row=0, column=0, sticky="nsew")

        mh = ttk.Frame(mbox, style="Card.TFrame")
        mh.pack(fill="x", pady=(0, int(pad * 0.6)))
        ttk.Label(mh, text="挂载目录", style="Big.TLabel").pack(side="left")
        self.lbl_mount_of = ttk.Label(mh, text="", style="Sub.TLabel")
        self.lbl_mount_of.pack(side="left", padx=(10, 0))
        for key, text, cmd in (("del", "移除", self.act_del_mount),
                               ("edit", "更改位置…", self.act_edit_mount),
                               ("tog", "挂载/卸载", self.act_toggle),
                               ("add", "添加文件夹…", self.act_add_mount)):
            b = ttk.Button(mh, text=text, command=cmd,
                           style="Accent.TButton" if key == "add" else "Tool.TButton")
            b.pack(side="right", padx=(6, 0))
            self.btns[key] = b

        self.tree = ttk.Treeview(mbox, columns=("local", "mount", "state"),
                                 show="headings", height=6, selectmode="browse")
        cw = int(fs * 17)
        for c, t, wd, anc in (("local", "本机文件夹", cw, "w"),
                              ("mount", "服务器挂载点", cw, "w"),
                              ("state", "状态", int(cw * 0.34), "center")):
            self.tree.heading(c, text=t, anchor=anc)
            self.tree.column(c, width=wd, anchor=anc, stretch=True)
        self.tree.tag_configure("on", foreground=C_OK)
        self.tree.tag_configure("off", foreground=C_NA)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _e: self.act_toggle())
        ttk.Label(mbox, text="双击一行可切换挂载状态",
                  style="Sub.TLabel").pack(anchor="w", pady=(6, 0))

        # ---------- 右栏下半：运行日志（可折叠） ----------
        self.log_open = tk.BooleanVar(value=bool(CFG.get("log_open", True)))
        logbar = ttk.Frame(right)
        logbar.grid(row=1, column=0, sticky="ew", pady=(int(pad * 0.5), 0))
        self.btn_log = flat_btn(logbar, "", self._toggle_log, padx=0, pady=2, anchor="w")
        self.btn_log.pack(side="left")
        self.lbl_status = ttk.Label(logbar, text="", style="SubBg.TLabel")
        self.lbl_status.pack(side="right")

        self.logbox, lbox = card(right, padding=(int(pad * 0.6), int(pad * 0.5)))
        lwrap = ttk.Frame(lbox, style="Card.TFrame")
        lwrap.pack(fill="both", expand=True)
        self.txt = tk.Text(lwrap, height=8, wrap="none", font=self.f_mono, bg=C_CARD,
                           fg=C_TEXT, relief="flat", bd=0, state="disabled",
                           highlightthickness=0, insertbackground=C_TEXT)
        sc = ttk.Scrollbar(lwrap, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sc.set)
        sc.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("ts", foreground=C_NA)
        self.txt.tag_configure("warn", foreground=C_WARN)
        self.txt.tag_configure("error", foreground=C_BAD)
        self._apply_log_vis()

        self._rebuild_server_list()

    # ------------------------------------------------------------ 左栏：服务器

    def _srv_scroll_set(self, lo, hi):
        """内容装得下就把滚动条收起来 —— 侧栏本来就窄，别白占 15 像素"""
        self.srv_sc.set(lo, hi)
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.srv_sc.pack_forget()
        elif not self.srv_sc.winfo_ismapped():
            self.srv_sc.pack(side="right", fill="y", before=self.srv_canvas)

    def _srv_wheel(self, e):
        step = -1 if getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0 else 1
        self.srv_canvas.yview_scroll(step, "units")

    def _rebuild_server_list(self):
        """重建整个列表。只在服务器增删改名时调用 —— 每秒刷新只改文字，
        否则鼠标悬停和点击会被不断重建的控件吃掉。"""
        for w in self.srv_host.winfo_children():
            w.destroy()
        self.srv_rows.clear()
        self.dyn_btns = [b for b in self.dyn_btns if False]

        if not self.servers:
            ttk.Label(self.srv_host, text="还没有服务器\n点右上角「＋ 添加」",
                      style="Sub.TLabel", justify="left").pack(anchor="w", pady=8)
            return

        if self.sel_sid not in [s.sid for s in self.servers]:
            self.sel_sid = self.servers[0].sid

        fs = int(CFG.get("font_size", 11))
        for srv in self.servers:
            self.srv_rows[srv.sid] = self._build_srv_row(self.srv_host, srv, fs)
        self._paint_selection()

    def _build_srv_row(self, parent, srv, fs):
        sel = srv.sid == self.sel_sid
        bg = C_SEL if sel else C_CARD

        shell = tk.Frame(parent, bg=bg, highlightthickness=0)
        shell.pack(fill="x", pady=(0, 4))
        # 左侧一道竖条标出当前选中的服务器，比只换底色好认
        strip = tk.Frame(shell, bg=C_ACCENT if sel else bg, width=3)
        strip.pack(side="left", fill="y")
        inner = tk.Frame(shell, bg=bg)
        inner.pack(side="left", fill="both", expand=True)

        head = tk.Frame(inner, bg=bg, cursor="hand2")
        head.pack(fill="x", padx=8, pady=5)

        # 按钮和箭头先占位，名字最后才 expand —— 反过来的话长名字会把按钮挤没
        dot = tk.Label(head, text="●", fg=C_NA, bg=bg, font=(self.f_base[0], fs))
        dot.pack(side="left")
        caret = tk.Label(head, text="▸", bg=bg, fg=C_MUTED, font=self.f_sub, cursor="hand2")
        caret.pack(side="right", padx=(6, 0))
        conn = tk.Button(head, text="连接", relief="flat", bd=0, cursor="hand2",
                         bg=C_ACCENT, fg="#ffffff", activebackground=C_ACCENT_H,
                         activeforeground="#ffffff", font=self.f_sub,
                         padx=10, pady=3, highlightthickness=0,
                         command=lambda s=srv: self._srv_act(s, self.act_primary))
        conn.pack(side="right")
        self.dyn_btns.append(conn)

        namebox = tk.Frame(head, bg=bg)
        namebox.pack(side="left", fill="x", expand=True, padx=(5, 8))
        # width=1 让标签不按文字长度索要宽度，长名字会被裁掉而不是撑破整行
        name = tk.Label(namebox, text=srv.label, bg=bg, fg=C_TEXT,
                        font=self.f_bold, anchor="w", width=1)
        name.pack(fill="x")
        sub = tk.Label(namebox, text="", bg=bg, fg=C_MUTED, font=self.f_sub,
                       anchor="w", width=1)
        sub.pack(fill="x")

        # 点行选中，点箭头展开/收起 —— 两件事分开，免得选个服务器还得看见一堆细节
        for w in (inner, head, namebox, name, sub, dot):
            w.bind("<Button-1>", lambda _e, s=srv: self._select_srv(s.sid))
        caret.bind("<Button-1>", lambda _e, s=srv: self._toggle_expand(s.sid))

        detail = tk.Frame(inner, bg=C_CARD2)
        rows = {}
        for key, title in (("tunnel", "隧道"), ("server", "服务器"), ("pipe", "状态")):
            line = tk.Frame(detail, bg=C_CARD2)
            line.pack(fill="x", padx=10, pady=(4, 0))
            tk.Label(line, text=title, bg=C_CARD2, fg=C_MUTED, font=self.f_sub,
                     width=5, anchor="w").pack(side="left")
            val = tk.Label(line, text="—", bg=C_CARD2, fg=C_TEXT,
                           font=self.f_sub, anchor="w", justify="left")
            val.pack(side="left", fill="x", expand=True)
            rows[key] = val

        acts = tk.Frame(detail, bg=C_CARD2)
        acts.pack(fill="x", padx=8, pady=(8, 8))
        for text, cmd in (("重连", lambda s=srv: self._srv_act(s, self.act_tunnel_restart)),
                          ("编辑", lambda s=srv: self._srv_act(s, self.act_edit_server)),
                          ("删除", lambda s=srv: self._srv_act(s, self.act_del_server))):
            b = self.flat_btn(acts, text, cmd, bg=C_CARD2, padx=6, pady=2)
            b.pack(side="left", padx=(0, 8))
            self.dyn_btns.append(b)

        if srv.sid in self.expanded:
            detail.pack(fill="x")
            caret.configure(text="▾")

        return {"shell": shell, "inner": inner, "strip": strip, "head": head,
                "namebox": namebox, "name": name, "sub": sub, "dot": dot,
                "caret": caret, "detail": detail, "rows": rows,
                "conn": conn, "acts": acts}

    def _srv_act(self, srv, fn):
        """行内按钮先把该服务器选中，再执行动作 —— 动作都作用于 current()"""
        self._select_srv(srv.sid)
        fn()

    def _select_srv(self, sid):
        if sid == self.sel_sid:
            return
        self.sel_sid = sid
        self._paint_selection()
        self._refresh_mounts()
        self._persist()

    def _paint_selection(self):
        for sid, w in self.srv_rows.items():
            on = sid == self.sel_sid
            bg = C_SEL if on else C_CARD
            for k in ("shell", "inner", "head", "namebox", "name", "sub", "dot", "caret"):
                w[k].configure(bg=bg)
            w["strip"].configure(bg=C_ACCENT if on else bg)

    def _toggle_expand(self, sid):
        w = self.srv_rows.get(sid)
        if not w:
            return
        if sid in self.expanded:
            self.expanded.discard(sid)
            w["detail"].pack_forget()
            w["caret"].configure(text="▸")
        else:
            self.expanded.add(sid)
            w["detail"].pack(fill="x")
            w["caret"].configure(text="▾")

    # ------------------------------------------------------------ 日志折叠

    def _apply_log_vis(self):
        if self.log_open.get():
            self.logbox.grid(row=2, column=0, sticky="nsew", pady=(4, 0))
            self.btn_log.configure(text="▾ 运行日志")
        else:
            self.logbox.grid_forget()
            self.btn_log.configure(text="▸ 运行日志")

    def act_start_sshd(self):
        def job():
            self.log("正在启动本机 SSH 服务…")
            ok, msg = sshd_start()
            self.log(("  " + msg) if ok else ("  " + msg), "info" if ok else "error")
            if ok:
                self.sshd = {"ok": True, "text": "运行中"}
        self._work(job)

    def act_theme(self):
        """ttk 样式是建界面时一次性配好的，换主题最干净的办法是就地重启"""
        CFG["theme"] = "light" if THEME == "dark" else "dark"
        save_cfg(CFG)
        self.log(f"切换到{'浅色' if CFG['theme'] == 'light' else '深色'}主题，重启界面…")
        self._restart_self()

    def _toggle_log(self):
        self.log_open.set(not self.log_open.get())
        CFG["log_open"] = self.log_open.get()
        save_cfg(CFG)
        self._apply_log_vis()

    def act_primary(self):
        """主按钮：按当前状态决定是连接还是断开"""
        srv = self.current()
        if not srv:
            self.act_add_server()
            return
        if srv.state["tunnel"].get("ok"):
            self.act_tunnel_stop()
        else:
            self.act_tunnel_start()

    # ------------------------------------------------------------ 工具

    def log(self, msg, level="info"):
        self.msgq.put(("log", (time.strftime("%H:%M:%S"), str(msg), level)))

    def _drain(self):
        """唯一允许操作 Tk 的入口 —— 工作线程一律通过队列传消息进来。

        不要在工作线程里调 root.after()：after 本身就是 Tk 调用，
        macOS 的 Tk 对跨线程调用会直接崩溃（表现为「Python 意外退出」）。
        """
        try:
            while True:
                kind, payload = self.msgq.get_nowait()
                if kind == "log":
                    ts, msg, level = payload
                    self.txt.configure(state="normal")
                    self.txt.insert("end", ts + "  ", ("ts",))
                    self.txt.insert("end", msg + "\n", (level,) if level != "info" else ())
                    self.txt.see("end")
                    self.txt.configure(state="disabled")
                elif kind == "busy":
                    self._set_busy(payload)
                elif kind == "removed":
                    # 服务器是在工作线程里摘掉的，界面重建必须回到主线程做，
                    # 否则 Tk 会跨线程崩（macOS 上尤其必崩）
                    self.sel_sid = self.servers[0].sid if self.servers else None
                    self._persist()
                    self._rebuild_server_list()
                    self._refresh_mounts()
                    self.log(f"已移除 {payload}")
        except queue.Empty:
            pass
        self.root.after(200, self._drain)

    def _set_busy(self, busy):
        self.busy = busy
        for b in list(self.btns.values()) + list(self.dyn_btns):
            try:
                b.configure(state="disabled" if busy else "normal")
            except tk.TclError:
                pass            # 行重建后旧按钮已销毁，忽略
        self.root.configure(cursor="watch" if busy else "")

    def _work(self, fn):
        if self.busy:
            return
        self._set_busy(True)

        def wrapper():
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                self.log(f"操作异常: {exc}", "error")
            finally:
                self.msgq.put(("busy", False))     # 经队列回到主线程，不直接碰 Tk
        threading.Thread(target=wrapper, daemon=True).start()

    # ------------------------------------------------------------ 轮询

    def _poll_local_loop(self):
        streaks = {}
        while True:
            try:
                ok, text = sshd_status()
                self.sshd = {"ok": ok, "text": text}
                now = time.time()
                for srv in list(self.servers):
                    srv.state["tunnel"] = srv.tunnel_state()
                    up = srv.state["tunnel"]["ok"]
                    # keep_alive：不论隧道原先由谁启动，断了就接管重建
                    want = srv.want_up or srv.conf.get("auto_tunnel", False)
                    if want and not up:
                        streaks[srv.sid] = streaks.get(srv.sid, 0) + 1
                        if streaks[srv.sid] >= 2 and now >= srv._retry_at:
                            srv._retry_n += 1
                            delay = min(60, 3 * (2 ** min(srv._retry_n - 1, 4)))
                            self.log(f"[{srv.label}] 隧道断开，自动重连（第 {srv._retry_n} 次）…",
                                     "warn")
                            try:
                                srv.tunnel_kill()
                                srv.want_up = True
                                if srv._port_conflict >= 2:
                                    old_port = srv.port
                                    newp, rerr = srv.register(realloc=True)
                                    if rerr:
                                        self.log(f"  换端口失败: {rerr}", "error")
                                    elif newp and newp != old_port:
                                        self.log(f"  服务器改分配端口 {old_port} → {newp}")
                                        self._persist()
                                    srv._port_conflict = 0
                                srv.tunnel_spawn()
                            except Exception as exc:  # noqa: BLE001
                                self.log(f"[{srv.label}] 重连失败: {exc}", "error")
                                if srv._retry_n == 1:         # 只在首次失败时详细诊断
                                    try:
                                        srv.diagnose(str(exc))
                                    except Exception:  # noqa: BLE001
                                        pass
                                srv._retry_at = now + 60      # 配置类错误，别高频重试
                            srv._retry_at = now + delay
                            streaks[srv.sid] = 0
                    else:
                        streaks[srv.sid] = 0
                        if up and srv._retry_n:
                            self.log(f"[{srv.label}] 隧道已恢复")
                            srv._retry_n = 0
                            srv._retry_at = 0.0
                            threading.Thread(target=self._ensure_server_side,
                                             args=(srv,), daemon=True).start()
                        # 隧道在但状态管道一直读不到 —— 补做一次服务器端初始化
                        elif up and srv._probes >= 3 and not srv._fixed_pipe:
                            srv._fixed_pipe = True
                            self.log(f"[{srv.label}] 状态管道未就绪，正在补挂…", "warn")
                            threading.Thread(target=self._ensure_server_side,
                                             args=(srv,), daemon=True).start()
            except Exception as exc:  # noqa: BLE001
                self.log(f"本地轮询异常: {exc}", "error")
            time.sleep(CFG.get("poll_local", 2))

    def _poll_remote_loop(self):
        while True:
            for srv in list(self.servers):
                try:
                    srv.poll()
                except Exception as exc:  # noqa: BLE001
                    self.log(f"[{srv.label}] 轮询异常: {exc}", "error")
            time.sleep(CFG.get("poll_remote", 3))

    def _ensure_server_side(self, srv):
        """隧道通了之后，服务器端还要做两件事：把状态目录挂过去、把守护起来。
        「启动隧道」按钮和自动重连都必须走这里 —— 早先只有按钮做了，
        导致自动重连恢复的隧道永远是「状态管道未挂载」。"""
        try:
            mid = machine_id()
            rc, out = srv.on_server(
                # --status 让服务器自己决定挂载点：角色账户下不是 /root/...，
                # 客户端拼死路径会去建它没权限的目录
                f"bridge-mount -c {mid} {shlex_quote(srv.status_dir)} --status 2>&1; "
                f"bridge-statusd start -c {mid} 2>&1", 60)
            bad = [l for l in out.splitlines() if l.startswith("ERR|")]
            if bad:
                self.log(f"[{srv.label}] 状态管道挂载失败: {bad[0][4:]}", "error")
            return not bad
        except Exception as exc:  # noqa: BLE001
            self.log(f"[{srv.label}] 服务器端初始化异常: {exc}", "error")
            return False

    def _auto_boot(self):
        time.sleep(5)
        for srv in list(self.servers):
            if not srv.conf.get("auto_tunnel"):
                continue
            if srv.port_ok or (srv.tunnel_proc and srv.tunnel_proc.poll() is None):
                continue
            self.log(f"[{srv.label}] 自启：建立隧道…")
            try:
                srv.register()
                srv.tunnel_spawn()
                srv.want_up = True
                time.sleep(2)
                self._ensure_server_side(srv)
            except Exception as exc:  # noqa: BLE001
                self.log(f"[{srv.label}] 自启失败: {exc}", "error")

    def _watch_version(self):
        try:
            mine = os.path.abspath(__file__)
            base = os.path.getmtime(mine)
        except Exception:  # noqa: BLE001
            return
        while True:
            time.sleep(5)
            try:
                if os.path.getmtime(mine) > base + 1 and not self.update_ready:
                    self.update_ready = True
                    self.log("检测到客户端有新版本，点右上角「重载」应用", "warn")
            except Exception:  # noqa: BLE001
                pass

    def _restart_self(self):
        try:
            for srv in self.servers:
                srv.want_up = False       # 保留隧道进程，不在退出时杀
            rest = [a for a in sys.argv[1:] if a != "--auto-tunnel"]
            if getattr(sys, "frozen", False):
                # 打包成 Ferry.exe 之后，sys.executable 就是启动器本身，
                # 不能再往后面接脚本路径
                args = [sys.executable] + rest
            else:
                exe = sys.executable
                if exe.lower().endswith("python.exe"):
                    cand = exe[:-len("python.exe")] + "pythonw.exe"
                    if os.path.exists(cand):
                        exe = cand
                args = [exe, os.path.abspath(__file__)] + rest
            subprocess.Popen(args, cwd=BASE_DIR, creationflags=NO_WINDOW,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            self.root.after(400, self.root.destroy)
        except Exception as exc:  # noqa: BLE001
            self.log(f"重启失败: {exc}", "error")

    # ------------------------------------------------------------ 服务器动作

    def act_add_server(self):
        """默认走接入码；需要时可切到手工填写"""
        choice = messagebox.askyesnocancel(
            APP_NAME,
            "用接入码添加？（推荐）\n\n"
            "在服务器上执行  bridge-invite  会打印一段接入码，\n"
            "粘贴进来即可自动完成全部配置。\n\n"
            "  是   → 粘贴接入码\n"
            "  否   → 手工填写别名等信息\n"
            "  取消 → 不添加")
        if choice is None:
            return
        if choice:
            self._add_by_invite()
            return
        dlg = ServerDialog(self.root)
        if not dlg.result:
            return
        if any(s.sid == dlg.result["id"] for s in self.servers):
            messagebox.showwarning(APP_NAME, f"已存在同 ID 的服务器: {dlg.result['id']}")
            return
        srv = Server(dlg.result, self.log)
        srv.ensure_status_dir()
        self.servers.append(srv)
        self._persist()
        self.log(f"已添加服务器 {srv.label} ({srv.alias})")
        self.log(f"  → 记得在该服务器上把状态目录挂过去："
                 f"bridge-mount -c {machine_id()} "
                 f"{shlex_quote(os.path.join(STATUS_ROOT, srv.sid))} "
                 f"{srv.srv_status_mp}")
        self._refresh_servers()

    def _add_by_invite(self):
        dlg = InviteDialog(self.root, (self.f_base, self.f_sub, self.f_mono, self.f_big))
        if not dlg.result:
            return
        d = dlg.result

        def job():
            self.log(f"接入 {d['host']} …")
            try:
                alias, tips = apply_invite(d, self.log)
            except Exception as exc:  # noqa: BLE001
                self.log(f"  接入失败: {exc}", "error")
                return
            for t in tips:
                self.log(f"  {t}")

            if any(x.sid == alias for x in self.servers):
                self.log("  该服务器已在列表中", "warn")
                return
            conf = {"id": alias, "name": d.get("name") or d["host"],
                    "ssh_alias": alias, "host": d["host"], "identity": None,
                    "mounts": [], "auto_tunnel": True, "enabled": True}
            srv = Server(conf, self.log)
            srv.ensure_status_dir()
            self.servers.append(srv)
            self._persist()
            self.log(f"  已添加 {srv.label}")

            # 直接连上，省掉再点一次
            rc, out = run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                           "-o", "StrictHostKeyChecking=accept-new", alias, "echo ok"], 30)
            if rc != 0 or "ok" not in out:
                self.log(f"  连通性测试失败：{out.strip()[:140]}", "error")
                srv.diagnose(out)
                return
            self.log("  连通性正常，建立隧道…")
            try:
                srv.tunnel_spawn()
                srv.want_up = True
                time.sleep(2)
                self._ensure_server_side(srv)
                srv.poll()
                self.log("  ✅ 接入完成，可以「添加文件夹…」挂目录了")
            except Exception as exc:  # noqa: BLE001
                self.log(f"  建立隧道失败: {exc}", "error")
        self._work(job)

    def act_edit_server(self):
        srv = self.current()
        if not srv:
            return
        dlg = ServerDialog(self.root, srv.conf)
        if not dlg.result:
            return
        srv.conf.update(dlg.result)
        self._persist()
        self.log(f"已更新 {srv.label}")

    def act_del_server(self):
        srv = self.current()
        if not srv:
            return
        # 必须看 user_mounts 而不是 mounts —— 后者含状态管道那个内部挂载，
        # 只要隧道连着它就一直在，用 mounts 判断的话连着的服务器永远删不掉，
        # 而界面上的挂载表又是空的（表用的是 user_mounts），只能干瞪眼。
        busy = srv.user_mounts
        if busy:
            names = "\n".join("  · " + p for p in list(busy)[:6])
            more = f"\n  …… 还有 {len(busy) - 6} 个" if len(busy) > 6 else ""
            messagebox.showwarning(
                APP_NAME, f"{srv.label} 还挂着这些目录，请先卸载：\n\n{names}{more}")
            return
        if not messagebox.askyesno(APP_NAME, f"从列表移除 {srv.label}？\n（不会改动服务器本身）"):
            return

        def job():
            # 顺手把状态管道从服务器上卸掉，否则隧道一断它就成了死挂载，
            # 之后 bridge-check 会一直报异常
            if srv.port_ok and srv.status_mounted:
                try:
                    srv.on_server(
                        f"bridge-umount -c {machine_id()} "
                        f"{srv.srv_status_mp}", 25)
                except Exception:  # noqa: BLE001
                    pass
            srv.tunnel_kill()
            if srv in self.servers:
                self.servers.remove(srv)
            self.msgq.put(("removed", srv.label))
        self._work(job)
        self._refresh_servers()

    def act_tunnel_start(self):
        srv = self.current()
        if not srv:
            return
        if srv.tunnel_proc and srv.tunnel_proc.poll() is None:
            self.log(f"[{srv.label}] 隧道已在运行", "warn")
            return
        if srv.port_ok:
            self.log(f"[{srv.label}] 服务器端口已被其它进程占用", "warn")
            return

        def job():
            self.log(f"[{srv.label}] 向服务器登记本机…")
            port, err = srv.register()
            if err:
                self.log(f"  登记失败: {err}", "error")
                srv.diagnose(err)
                self.log(f"  若配置无误，检查服务器是否装了工具： "
                         f"ssh {srv.alias} 'which bridge-register'", "warn")
                return
            self.log(f"  已登记，分配端口 {port}")
            self._persist()

            self.log(f"[{srv.label}] 启动隧道 -> {srv.alias}")
            srv.tunnel_spawn()
            srv.want_up = True
            time.sleep(2)
            # 让服务器挂上状态目录并起守护，之后状态就走零 SSH 通道
            self._ensure_server_side(srv)
            srv.poll()
        self._work(job)

    def act_tunnel_stop(self):
        srv = self.current()
        if not srv:
            return

        def job():
            srv.want_up = False
            srv.tunnel_kill()
            self.log(f"[{srv.label}] 隧道已停止")
            time.sleep(1)
            srv.poll()
        self._work(job)

    def act_tunnel_restart(self):
        srv = self.current()
        if not srv:
            return

        def job():
            srv.tunnel_kill()
            time.sleep(1)
            self.log(f"[{srv.label}] 重连隧道…")
            srv.register()
            srv.tunnel_spawn()
            srv.want_up = True
            time.sleep(2)
            self._ensure_server_side(srv)
            srv.poll()
        self._work(job)

    # ------------------------------------------------------------ 挂载动作

    def _sel_mount(self):
        sel = self.tree.selection()
        return self.tree.item(sel[0], "values")[0] if sel else None

    def _entry_index(self, srv, local):
        """在配置里找这个本机路径对应的条目下标，没有返回 -1"""
        for i, e in enumerate(srv.conf.get("mounts", [])):
            if mount_local(e) == local:
                return i
        return -1

    def _save_entry(self, srv, local, target):
        entries = srv.conf.setdefault("mounts", [])
        i = self._entry_index(srv, local)
        if i >= 0:
            entries[i] = mount_entry(local, target)
        else:
            entries.append(mount_entry(local, target))
        self._persist()

    def act_add_mount(self):
        srv = self.current()
        if not srv:
            messagebox.showinfo(APP_NAME, "请先选中一台服务器。")
            return
        dlg = MountDialog(self.root, srv)
        if not dlg.result:
            return
        local, target = dlg.result
        self._save_entry(srv, local, target)
        self.log(f"[{srv.label}] 已添加 {local}")
        self._do_mount(srv, local, target)

    def act_edit_mount(self):
        """改挂载位置。已挂着的要先卸载，否则改了也不生效，白改一场。"""
        srv, local = self.current(), self._sel_mount()
        if not srv or not local:
            return
        if local in srv.mounts:
            messagebox.showinfo(APP_NAME, "该目录正挂载中。改位置需要先卸载，再重新挂。")
            return
        i = self._entry_index(srv, local)
        cur = srv.conf["mounts"][i] if i >= 0 else local
        dlg = MountDialog(self.root, srv, mount_local(cur), mount_target(cur), title="更改挂载位置")
        if not dlg.result:
            return
        new_local, target = dlg.result
        if i >= 0 and new_local != local:
            srv.conf["mounts"].pop(i)
        self._save_entry(srv, new_local, target)
        self.log(f"[{srv.label}] 已更新 {new_local} → {target or '默认位置'}")
        self._refresh_mounts()

    def act_del_mount(self):
        srv, local = self.current(), self._sel_mount()
        if not srv or not local:
            return
        if local in srv.mounts:
            messagebox.showwarning(APP_NAME, "该目录正在挂载中，请先卸载。")
            return
        i = self._entry_index(srv, local)
        if i >= 0:
            srv.conf["mounts"].pop(i)
            self._persist()
            self.log(f"[{srv.label}] 已从列表移除 {local}")
            self._refresh_mounts()

    def act_toggle(self):
        srv, local = self.current(), self._sel_mount()
        if not srv or not local:
            return
        if local in srv.mounts:
            self._do_umount(srv, local, srv.mounts[local])
        else:
            i = self._entry_index(srv, local)
            target = mount_target(srv.conf["mounts"][i]) if i >= 0 else ""
            self._do_mount(srv, local, target)

    def act_refresh(self):
        srv = self.current()
        if srv:
            self._work(srv.poll)

    def _do_mount(self, srv, path, target=""):
        def job():
            if not srv.port_ok:
                self.log(f"[{srv.label}] 隧道未连接，无法挂载", "error")
                return
            self.log(f"[{srv.label}] 挂载 {path} …")
            cmd = f"bridge-mount -c {machine_id()} {shlex_quote(path)}"
            if target:
                cmd += f" {shlex_quote(target)}"
            rc, out = srv.on_server(cmd, 60)
            line = out.strip().splitlines()[-1] if out.strip() else ""
            if line.startswith(("OK|", "ALREADY|")):
                self.log(f"  → {line.split('|', 1)[1]}")
            else:
                self.log(f"  失败: {line.split('|', 1)[-1] or out.strip()}", "error")
            srv.poll()
        self._work(job)

    def _do_umount(self, srv, path, mp):
        def job():
            self.log(f"[{srv.label}] 卸载 {path} …")
            rc, out = srv.on_server(
                f"bridge-umount -c {machine_id()} {shlex_quote(mp)}", 40)
            line = out.strip().splitlines()[-1] if out.strip() else ""
            ok = line.startswith("OK|")
            self.log("  已卸载" if ok else f"  失败: {line.split('|', 1)[-1] or out.strip()}",
                     "info" if ok else "error")
            srv.poll()
        self._work(job)

    # ------------------------------------------------------------ 全局动作

    def act_reload(self):
        if any(s.tunnel_proc and s.tunnel_proc.poll() is None for s in self.servers):
            if not messagebox.askyesno(APP_NAME, "重载会重启客户端。\n隧道进程会保留。\n\n继续？"):
                return
        self.log("重载客户端…")
        self._restart_self()

    def act_panic(self):
        if not messagebox.askyesno(
                APP_NAME,
                "将停止全部隧道并关闭 Windows SSH 服务。\n\n"
                "所有服务器会立即失去对本机的访问权。\n\n确定继续？", icon="warning"):
            return

        def job():
            self.log("紧急断开：停止全部隧道 + 关闭 sshd", "warn")
            for srv in self.servers:
                srv.want_up = False
                srv.tunnel_kill()
            ok, tip = sshd_stop()
            self.log("  " + tip, "info" if ok else "warn")
        self._work(job)

    # ------------------------------------------------------------ 刷新

    def _refresh_servers(self):
        """每秒刷新：只改已有控件的文字和颜色。服务器集合变了才整体重建。"""
        if set(self.srv_rows) != {s.sid for s in self.servers}:
            self._rebuild_server_list()

        for srv in self.servers:
            w = self.srv_rows.get(srv.sid)
            if not w:
                continue
            t, s = srv.state["tunnel"], srv.state["server"]
            ok = bool(t.get("ok"))
            w["dot"].configure(fg=C_OK if ok else (C_NA if t.get("ok") is None else C_BAD))
            if w["name"].cget("text") != srv.label:
                w["name"].configure(text=srv.label)

            bits = [srv.alias]
            if srv.port:
                bits.append(f":{srv.port}")
            n = len(srv.user_mounts)
            bits.append(f"· {n} 个挂载" if n else "· 未挂载")
            w["sub"].configure(text=" ".join(bits))

            if self.busy:
                w["conn"].configure(text="…", bg=C_NA, state="disabled")
            elif ok:
                w["conn"].configure(text="断开", bg=C_CARD2, fg=C_TEXT,
                                    activebackground=C_LINE, state="normal")
            else:
                w["conn"].configure(text="连接", bg=C_ACCENT, fg="#ffffff",
                                    activebackground=C_ACCENT_H, state="normal")

            if srv.sid in self.expanded:
                w["rows"]["tunnel"].configure(
                    text=t.get("text", "—"),
                    fg=C_OK if ok else (C_MUTED if t.get("ok") is None else C_BAD))
                w["rows"]["server"].configure(
                    text=s.get("text", "—"),
                    fg=C_OK if s.get("ok") else (C_MUTED if s.get("ok") is None else C_BAD))
                src, fresh = srv.state.get("source"), srv.state.get("fresh_s")
                if src == "local" and fresh is not None:
                    txt, col = f"实时同步 · {fresh:.0f}s 前", C_MUTED
                elif src == "ssh":
                    txt, col = "SSH 探测（管道未就绪）", C_WARN
                elif src == "stale":
                    txt, col = "等待状态…", C_WARN
                else:
                    txt, col = "—", C_MUTED
                if not srv.status_mounted and srv.port_ok:
                    txt, col = txt + " · 管道未挂载", C_WARN
                w["rows"]["pipe"].configure(text=txt, fg=col)

    def _refresh_mounts(self):
        srv = self.current()
        keep = self._sel_mount()
        self.tree.delete(*self.tree.get_children())
        if not srv:
            return
        planned = {}
        for e in srv.conf.get("mounts", []):
            planned.setdefault(mount_local(e), mount_target(e))
        rows = [p for p in dict.fromkeys(list(planned) + list(srv.mounts.keys()))
                if p and not Server.is_internal(p)]
        for path in rows:
            mp = srv.mounts.get(path)
            # 没挂上时显示「打算挂到哪」，比一个 — 有用得多
            where = mp or planned.get(path) or f"{srv.srv_mnt_root}/{default_mount_dir(path)}"
            self.tree.insert("", "end",
                             values=(path, where, "已挂载" if mp else "未挂载"),
                             tags=("on" if mp else "off",))
        if keep:
            for iid in self.tree.get_children():
                if self.tree.item(iid, "values")[0] == keep:
                    self.tree.selection_set(iid)
                    break

    def _refresh_ui(self):
        srv = self.current()

        v = self.sshd
        self.dots["sshd"].configure(
            fg=C_OK if v.get("ok") else (C_NA if v.get("ok") is None else C_BAD))
        self.vals["sshd"].configure(text=v.get("text", "—"))
        if v.get("ok") is False and not self.btn_sshd.winfo_ismapped():
            self.btn_sshd.pack(side="left", padx=(6, 0))
        elif v.get("ok") and self.btn_sshd.winfo_ismapped():
            self.btn_sshd.pack_forget()

        if srv:
            self.lbl_mount_of.configure(text=f"· {srv.label}")
            up = srv.state.get("uptime")
            self.lbl_status.configure(text=f"服务器已运行 {up}" if up else "")
        else:
            self.lbl_mount_of.configure(text="")
            self.lbl_status.configure(text="")

        if self.update_ready:
            self.btn_reload.configure(text="● 有更新，点此重载", fg=C_WARN)

        self._refresh_servers()
        self._refresh_mounts()
        self.root.after(1000, self._refresh_ui)

    # ------------------------------------------------------------ 关闭

    def _on_close(self):
        alive = [s for s in self.servers if s.tunnel_proc and s.tunnel_proc.poll() is None]
        if alive:
            names = "、".join(s.label for s in alive)
            if not messagebox.askyesno(APP_NAME, f"退出会断开这些隧道：\n{names}\n\n确定退出？"):
                return
        for s in self.servers:
            s.want_up = False
            s.tunnel_kill()
        self.root.destroy()


def check_tk():
    """Apple 自带的 Tcl/Tk 8.5.9 在现代 macOS（尤其 Apple Silicon）上会直接 abort。
    与其崩溃给用户看一个没有信息量的「Python 意外退出」，不如提前说清楚。"""
    try:
        ver = float(tk.TkVersion)
    except Exception:  # noqa: BLE001
        return
    if ver >= 8.6:
        return
    msg = [
        "",
        "=" * 68,
        f"  检测到 Tcl/Tk {tk.TkVersion} —— 这个版本在 macOS 上会导致程序崩溃。",
        "",
        f"  当前解释器: {sys.executable}",
        "",
        "  Apple 系统自带的 Tk 8.5.9 已废弃且有已知崩溃缺陷，",
        "  Xcode / 系统自带的 python3 都链接到它。",
        "",
        "  解决办法：装一个带正常 Tk 的 Python，然后用它启动",
        "",
        "      brew install python-tk",
        "      /opt/homebrew/bin/python3 bridge_gui.py",
        "",
        "  验证： /opt/homebrew/bin/python3 -c \"import tkinter; print(tkinter.TkVersion)\"",
        "  应显示 8.6 或更高。",
        "=" * 68,
        "",
    ]
    print("\n".join(msg), file=sys.stderr)
    sys.exit(2)


def main():
    auto = "--auto-tunnel" in sys.argv
    mini = "--minimized" in sys.argv
    check_tk()
    os.makedirs(STATUS_ROOT, exist_ok=True)
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
        # 任务栏要认出这是独立应用（而不是「Python」），得给它自己的 AppUserModelID
        windll.shell32.SetCurrentProcessExplicitAppUserModelID("online.ferry.console")
    except Exception:  # noqa: BLE001
        pass
    set_window_icon(root)
    BridgeApp(root, auto_tunnel=auto, start_minimized=mini)
    root.mainloop()


if __name__ == "__main__":
    main()
