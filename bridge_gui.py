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
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

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
}

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
C_OK, C_BAD, C_NA, C_WARN = "#0f9960", "#d1435b", "#9aa1a9", "#c77700"

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
        self._err = None          # 隧道进程的 stderr 临时文件

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

    def register(self):
        """把本机身份上报给服务器，换回分配的隧道端口。

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
        if port is None:
            return None, "服务器未返回端口: " + out.strip()[:120]
        if port != self.conf.get("tunnel_port"):
            self.conf["tunnel_port"] = port
        return port, None

    # ---- 隧道
    def tunnel_spawn(self):
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
            return {"ok": True, "text": f"已连接 {up}".strip(), "owner": "self"}
        if p is not None:
            code = p.returncode
            self.tunnel_proc = None
            self.tunnel_since = None
            detail = self._read_tunnel_err()
            self.log(f"[{self.label}] 隧道进程退出 (code {code})", "warn")
            for line in detail:
                self.log(f"    {line}", "error")
            if code == 255 and not detail:
                self.log("    SSH 通用错误。终端手动跑一次看详情： "
                         f"ssh -N -v {self.alias}", "warn")
        if self.port_ok:
            return {"ok": True, "text": "已连接（外部）", "owner": "external"}
        return {"ok": False, "text": "未连接", "owner": None}

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
                     f"检查服务器是否挂载了状态目录 status\\{self.sid}", "warn")
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


# ================================================================ 服务器编辑对话框

class ServerDialog(tk.Toplevel):
    FIELDS = [
        ("ssh_alias",   "SSH 别名",       "~/.ssh/config 里的 Host 名，如 myserver（唯一必填项）"),
        ("name",        "显示名称(可选)", "留空则用别名"),
        ("host",        "服务器地址(可选)", "仅用于界面显示"),
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

    def _save(self):
        vals = {k: v.get().strip() for k, v in self.vars.items()}
        if not vals["ssh_alias"]:
            messagebox.showwarning(
                APP_NAME, "SSH 别名必填 —— 它必须是你 ~/.ssh/config 里已配置的 Host。",
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
        sel = self.tree_srv.selection()
        if not sel:
            return None
        sid = self.tree_srv.item(sel[0], "values")[0]
        return next((s for s in self.servers if s.sid == sid), None)

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

        fs = int(CFG.get("font_size", 11))
        df, dm = default_fonts()
        fam = CFG.get("font_family") or df
        mono = CFG.get("mono_family") or dm
        self.f_base, self.f_bold = (fam, fs), (fam, fs, "bold")
        self.f_head, self.f_sub = (fam, fs + 6, "bold"), (fam, max(fs - 1, 8))
        self.f_val, self.f_mono = (fam, fs + 1, "bold"), (mono, fs)

        w, h = int(64 * fs + 240), int(56 * fs + 200)
        r.geometry(f"{w}x{h}")
        r.minsize(int(w * 0.85), int(h * 0.8))
        r.option_add("*Font", self.f_base)

        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure(".", font=self.f_base)
        style.configure("TLabel", font=self.f_base)
        style.configure("TButton", font=self.f_base, padding=(10, 5))
        style.configure("TCheckbutton", font=self.f_base)
        style.configure("TLabelframe.Label", font=self.f_bold)
        style.configure("Treeview", font=self.f_base, rowheight=int(fs * 2.3))
        style.configure("Treeview.Heading", font=self.f_bold)
        style.configure("Head.TLabel", font=self.f_head)
        style.configure("Sub.TLabel", font=self.f_sub, foreground="#6b7280")
        style.configure("Val.TLabel", font=self.f_val)

        outer = ttk.Frame(r, padding=12)
        outer.pack(fill="both", expand=True)

        # ---- 标题栏
        head = ttk.Frame(outer)
        head.pack(fill="x", pady=(0, 10))
        ttk.Label(head, text=APP_NAME, style="Head.TLabel").pack(side="left")
        ttk.Label(head, text=f"({PLATFORM})", style="Sub.TLabel").pack(side="left", padx=(6, 0))
        self.lbl_sshd = ttk.Label(head, text="", style="Sub.TLabel")
        self.lbl_sshd.pack(side="left", padx=(14, 0))
        self.btn_reload = tk.Button(head, text="重载", command=self.act_reload,
                                    relief="flat", padx=12, pady=2, cursor="hand2")
        self.btn_reload.pack(side="right")
        self.btn_panic = tk.Button(head, text="紧急断开", command=self.act_panic,
                                   fg="#fff", bg=C_BAD, activebackground="#b03a4e",
                                   activeforeground="#fff", relief="flat",
                                   padx=14, pady=2, cursor="hand2")
        self.btn_panic.pack(side="right", padx=(0, 8))

        # ---- 服务器列表
        sbox = ttk.LabelFrame(outer, text=" 服务器 ", padding=10)
        sbox.pack(fill="x", pady=(0, 10))
        cols = ("id", "name", "alias", "tunnel", "keep", "status", "mounts")
        self.tree_srv = ttk.Treeview(sbox, columns=cols, show="headings",
                                     height=4, selectmode="browse")
        for c, txt, wd in [("id", "ID", int(fs * 7)), ("name", "名称", int(fs * 12)),
                           ("alias", "SSH 别名", int(fs * 10)),
                           ("tunnel", "隧道", int(fs * 11)),
                           ("keep", "保活", int(fs * 4)),
                           ("status", "服务器", int(fs * 8)),
                           ("mounts", "挂载", int(fs * 5))]:
            self.tree_srv.heading(c, text=txt)
            self.tree_srv.column(c, width=wd, anchor="w")
        self.tree_srv.tag_configure("on", foreground=C_OK)
        self.tree_srv.tag_configure("off", foreground=C_NA)
        self.tree_srv.pack(fill="x")
        self.tree_srv.bind("<<TreeviewSelect>>", lambda _e: self._refresh_mounts())

        sbar = ttk.Frame(sbox)
        sbar.pack(fill="x", pady=(8, 0))
        self.btns = {}
        for key, text, cmd in [("s_add", "添加服务器…", self.act_add_server),
                               ("s_edit", "编辑", self.act_edit_server),
                               ("s_del", "删除", self.act_del_server)]:
            b = ttk.Button(sbar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.btns[key] = b
        ttk.Separator(sbar, orient="vertical").pack(side="left", fill="y", padx=8)
        for key, text, cmd in [("t_start", "启动隧道", self.act_tunnel_start),
                               ("t_stop", "停止", self.act_tunnel_stop),
                               ("t_re", "重连", self.act_tunnel_restart)]:
            b = ttk.Button(sbar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.btns[key] = b

        # ---- 挂载目录
        mbox = ttk.LabelFrame(outer, text=" 挂载目录（选中服务器的） ", padding=10)
        mbox.pack(fill="both", expand=False, pady=(0, 10))
        self.tree = ttk.Treeview(mbox, columns=("local", "mount", "state"),
                                 show="headings", height=5, selectmode="browse")
        cw = int(fs * 23)
        for c, txt, wd, anc in [("local", "本机文件夹", cw, "w"),
                                ("mount", "服务器挂载点", cw, "w"),
                                ("state", "状态", int(cw * 0.34), "center")]:
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=wd, anchor=anc)
        self.tree.tag_configure("on", foreground=C_OK)
        self.tree.tag_configure("off", foreground=C_NA)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _e: self.act_toggle())

        mbar = ttk.Frame(mbox)
        mbar.pack(fill="x", pady=(8, 0))
        for key, text, cmd in [("add", "添加文件夹…", self.act_add_mount),
                               ("tog", "挂载 / 卸载", self.act_toggle),
                               ("del", "从列表移除", self.act_del_mount),
                               ("ref", "刷新", self.act_refresh)]:
            b = ttk.Button(mbar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.btns[key] = b
        ttk.Label(mbar, text="双击一行可切换挂载状态",
                  style="Sub.TLabel").pack(side="left", padx=(8, 0))

        # ---- 日志
        lbox = ttk.LabelFrame(outer, text=" 运行日志 ", padding=6)
        lbox.pack(fill="both", expand=True)
        self.txt = tk.Text(lbox, height=8, wrap="none", font=self.f_mono,
                           bg="#fbfbfc", relief="flat", state="disabled")
        ls = ttk.Scrollbar(lbox, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=ls.set)
        ls.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("ts", foreground="#9aa1a9")
        self.txt.tag_configure("warn", foreground=C_WARN)
        self.txt.tag_configure("error", foreground=C_BAD)

        self.lbl_status = ttk.Label(outer, text="", style="Sub.TLabel")
        self.lbl_status.pack(fill="x", pady=(6, 0))

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
        except queue.Empty:
            pass
        self.root.after(200, self._drain)

    def _set_busy(self, busy):
        self.busy = busy
        for b in self.btns.values():
            b.configure(state="disabled" if busy else "normal")
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
                                srv.tunnel_spawn()
                            except Exception as exc:  # noqa: BLE001
                                self.log(f"[{srv.label}] 重连失败: {exc}", "error")
                                srv._retry_at = now + 60      # 配置类错误，别高频重试
                            srv._retry_at = now + delay
                            streaks[srv.sid] = 0
                    else:
                        streaks[srv.sid] = 0
                        if up and srv._retry_n:
                            self.log(f"[{srv.label}] 隧道已恢复")
                            srv._retry_n = 0
                            srv._retry_at = 0.0
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
                srv.on_server(f"bridge-statusd start -c {machine_id()}", 30)
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
            exe = sys.executable
            if exe.lower().endswith("python.exe"):
                cand = exe[:-len("python.exe")] + "pythonw.exe"
                if os.path.exists(cand):
                    exe = cand
            args = [exe, os.path.abspath(__file__)] + [
                a for a in sys.argv[1:] if a != "--auto-tunnel"]
            subprocess.Popen(args, cwd=BASE_DIR, creationflags=NO_WINDOW,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            self.root.after(400, self.root.destroy)
        except Exception as exc:  # noqa: BLE001
            self.log(f"重启失败: {exc}", "error")

    # ------------------------------------------------------------ 服务器动作

    def act_add_server(self):
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
                 f"/root/.winbridge/status/{machine_id()}")
        self._refresh_servers()

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
        if srv.mounts:
            messagebox.showwarning(APP_NAME, "该服务器仍有挂载，请先卸载。")
            return
        if not messagebox.askyesno(APP_NAME, f"从列表移除 {srv.label}？\n（不会改动服务器本身）"):
            return
        srv.tunnel_kill()
        self.servers.remove(srv)
        self._persist()
        self.log(f"已移除 {srv.label}")
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
                self.log(f"  自查 1：终端能否连上 →  ssh {srv.alias} echo ok", "warn")
                self.log(f"  自查 2：服务器上是否装了工具 →  ssh {srv.alias} 'which bridge-register'", "warn")
                self.log("  自查 3：没装的话在服务器上跑 bridge-install.sh", "warn")
                return
            self.log(f"  已登记，分配端口 {port}")
            self._persist()

            self.log(f"[{srv.label}] 启动隧道 -> {srv.alias}")
            srv.tunnel_spawn()
            srv.want_up = True
            time.sleep(2)
            # 让服务器挂上状态目录并起守护，之后状态就走零 SSH 通道
            mid = machine_id()
            srv.on_server(f"bridge-mount -c {mid} {shlex_quote(srv.status_dir)} "
                          f"/root/.winbridge/status/{mid} >/dev/null 2>&1; "
                          f"bridge-statusd start -c {mid}", 60)
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
            srv.on_server(f"bridge-statusd start -c {machine_id()}", 30)
            srv.poll()
        self._work(job)

    # ------------------------------------------------------------ 挂载动作

    def _sel_mount(self):
        sel = self.tree.selection()
        return self.tree.item(sel[0], "values")[0] if sel else None

    def act_add_mount(self):
        srv = self.current()
        if not srv:
            messagebox.showinfo(APP_NAME, "请先选中一台服务器。")
            return
        path = filedialog.askdirectory(title=f"选择要挂载到 {srv.label} 的文件夹",
                                       mustexist=True)
        if not path:
            return
        path = os.path.normpath(path)
        if Server.is_internal(path):
            messagebox.showinfo(APP_NAME, "这是控制台的内部状态目录，不需要手动挂载。")
            return
        if path not in srv.conf.setdefault("mounts", []):
            srv.conf["mounts"].append(path)
            self._persist()
            self.log(f"[{srv.label}] 已添加 {path}")
        self._do_mount(srv, path)

    def act_del_mount(self):
        srv, path = self.current(), self._sel_mount()
        if not srv or not path:
            return
        if path in srv.mounts:
            messagebox.showwarning(APP_NAME, "该目录正在挂载中，请先卸载。")
            return
        if path in srv.conf.get("mounts", []):
            srv.conf["mounts"].remove(path)
            self._persist()
            self.log(f"[{srv.label}] 已从列表移除 {path}")
            self._refresh_mounts()

    def act_toggle(self):
        srv, path = self.current(), self._sel_mount()
        if not srv or not path:
            return
        if path in srv.mounts:
            self._do_umount(srv, path, srv.mounts[path])
        else:
            self._do_mount(srv, path)

    def act_refresh(self):
        srv = self.current()
        if srv:
            self._work(srv.poll)

    def _do_mount(self, srv, path):
        def job():
            if not srv.port_ok:
                self.log(f"[{srv.label}] 隧道未连接，无法挂载", "error")
                return
            self.log(f"[{srv.label}] 挂载 {path} …")
            rc, out = srv.on_server(
                f"bridge-mount -c {machine_id()} {shlex_quote(path)}", 60)
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
        keep = None
        sel = self.tree_srv.selection()
        if sel:
            keep = self.tree_srv.item(sel[0], "values")[0]
        self.tree_srv.delete(*self.tree_srv.get_children())
        for srv in self.servers:
            t, st = srv.state["tunnel"], srv.state["server"]
            self.tree_srv.insert("", "end", values=(
                srv.sid, srv.label, srv.alias, t.get("text", "—"),
                "开" if srv.conf.get("auto_tunnel") else "关",
                st.get("text", "—"), len(srv.user_mounts)),
                tags=("on" if t.get("ok") else "off",))
        kids = self.tree_srv.get_children()
        if keep:
            for iid in kids:
                if self.tree_srv.item(iid, "values")[0] == keep:
                    self.tree_srv.selection_set(iid)
                    return
        if kids:
            self.tree_srv.selection_set(kids[0])

    def _refresh_mounts(self):
        srv = self.current()
        keep = self._sel_mount()
        self.tree.delete(*self.tree.get_children())
        if not srv:
            return
        rows = [p for p in dict.fromkeys(list(srv.conf.get("mounts", []))
                                         + list(srv.mounts.keys()))
                if not Server.is_internal(p)]
        for path in rows:
            mp = srv.mounts.get(path)
            self.tree.insert("", "end", values=(path, mp or "—", "已挂载" if mp else "未挂载"),
                             tags=("on" if mp else "off",))
        if keep:
            for iid in self.tree.get_children():
                if self.tree.item(iid, "values")[0] == keep:
                    self.tree.selection_set(iid)
                    break

    def _refresh_ui(self):
        col = C_OK if self.sshd["ok"] else (C_NA if self.sshd["ok"] is None else C_BAD)
        svc = {"windows": "Windows SSH 服务", "macos": "远程登录(sshd)"}.get(PLATFORM, "sshd")
        self.lbl_sshd.configure(text=f"● {svc}: {self.sshd['text']}", foreground=col)
        if self.update_ready:
            self.btn_reload.configure(text="重载 ●", fg="#fff", bg=C_WARN,
                                      activebackground=C_WARN, activeforeground="#fff")
        self._refresh_servers()
        self._refresh_mounts()

        srv = self.current()
        if srv:
            src, fresh = srv.state.get("source"), srv.state.get("fresh_s")
            mode = (f"状态实时同步 · {fresh:.0f}s前" if src == "local" and fresh is not None
                    else "SSH 直连探测" if src == "ssh"
                    else "等待状态…" if src == "stale" else "—")
            pipe = "状态管道已挂载" if srv.status_mounted else "状态管道未挂载"
            up = f"服务器已运行 {srv.state['uptime']}" if srv.state.get("uptime") else ""
            self.lbl_status.configure(
                text=f"[{srv.label}] {mode}    ·    {pipe}" + (f"    ·    {up}" if up else ""))
        else:
            self.lbl_status.configure(text="尚未配置服务器 —— 点「添加服务器…」开始")
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
    except Exception:  # noqa: BLE001
        pass
    BridgeApp(root, auto_tunnel=auto, start_minimized=mini)
    root.mainloop()


if __name__ == "__main__":
    main()
