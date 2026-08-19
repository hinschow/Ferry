#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ferry agent —— 本地 HTTP 服务，把 ferry_core 的能力暴露给网页界面。

    python ferry_agent.py            起服务并打印地址
    python ferry_agent.py --open     顺便用系统浏览器打开

Electron 壳只做一件事：开窗口加载这个地址。不装 Electron 也能用 ——
浏览器直接打开就是完整界面。

安全：只绑 127.0.0.1，且所有 /api 都要带启动时生成的随机 token。
不加 token 的话，你在浏览器里打开的任意网页都能往 127.0.0.1 发请求，
把你的隧道停掉或挂载别的目录。Host 头也要校验，挡 DNS rebinding。
"""
import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ferry_core as core                                    # noqa: E402
from ferry_core import (CFG, save_cfg, Server, sshd_status,   # noqa: E402
                        sshd_start, sshd_stop, machine_id, shlex_quote,
                        parse_invite, apply_invite, mount_local, mount_target,
                        mount_entry, default_mount_dir, BASE_DIR,
                        INSTALL_DIR, CFG_PATH)

UI_DIR = os.path.join(BASE_DIR, "ui")
TOKEN = secrets.token_urlsafe(24)


# ================================================================ 状态

class Agent:
    """把 BridgeApp 里那套轮询搬过来，只是不再驱动 tkinter 而是喂给 HTTP。"""

    def __init__(self):
        self.servers = []
        self.sshd = {"ok": None, "text": "检测中"}
        self.busy = False
        self.log_buf = []          # (seq, ts, msg, level)
        self.seq = 0
        self.lock = threading.Lock()
        self.load_servers()
        for fn in (self._poll_local, self._poll_remote):
            threading.Thread(target=fn, daemon=True).start()

    # ---- 日志
    def log(self, msg, level="info"):
        with self.lock:
            self.seq += 1
            self.log_buf.append((self.seq, time.strftime("%H:%M:%S"), str(msg), level))
            del self.log_buf[:-400]          # 只留最近 400 条

    def logs_since(self, since):
        with self.lock:
            return [{"seq": s, "ts": t, "msg": m, "level": lv}
                    for s, t, m, lv in self.log_buf if s > since]

    # ---- 服务器集合
    def load_servers(self):
        self.servers = []
        for conf in CFG.get("servers", []):
            srv = Server(conf, self.log)
            srv.ensure_status_dir()
            self.servers.append(srv)

    def by_id(self, sid):
        return next((s for s in self.servers if s.sid == sid), None)

    def persist(self):
        CFG["servers"] = [s.conf for s in self.servers]
        save_cfg(CFG)

    # ---- 轮询（与 tkinter 客户端同一套判断）
    def _poll_local(self):
        streaks, sshd_at = {}, 0.0
        while True:
            try:
                if time.time() - sshd_at >= 10:
                    ok, text = sshd_status()
                    self.sshd = {"ok": ok, "text": text}
                    sshd_at = time.time()
                now = time.time()
                for srv in list(self.servers):
                    srv.state["tunnel"] = srv.tunnel_state()
                    up = srv.state["tunnel"]["ok"]
                    want = srv.want_up or srv.conf.get("auto_tunnel", False)
                    if want and not up:
                        streaks[srv.sid] = streaks.get(srv.sid, 0) + 1
                        if streaks[srv.sid] >= 2 and now >= srv._retry_at:
                            srv._retry_n += 1
                            delay = min(60, 3 * (2 ** min(srv._retry_n - 1, 4)))
                            self.log(f"[{srv.label}] 隧道断开，自动重连（第 {srv._retry_n} 次）…", "warn")
                            try:
                                srv.tunnel_kill()
                                srv.want_up = True
                                if srv._port_conflict >= 2:
                                    oldp = srv.port
                                    newp, err = srv.register(realloc=True)
                                    if err:
                                        self.log(f"  换端口失败: {err}", "error")
                                    elif newp and newp != oldp:
                                        self.log(f"  服务器改分配端口 {oldp} → {newp}")
                                        self.persist()
                                    srv._port_conflict = 0
                                srv.tunnel_spawn()
                            except Exception as exc:      # noqa: BLE001
                                self.log(f"[{srv.label}] 重连失败: {exc}", "error")
                                srv._retry_at = now + 60
                            srv._retry_at = now + delay
                            streaks[srv.sid] = 0
                    else:
                        streaks[srv.sid] = 0
                        if up and srv._retry_n:
                            self.log(f"[{srv.label}] 隧道已恢复")
                            srv._retry_n = 0
                            srv._retry_at = 0.0
                            threading.Thread(target=self.ensure_server_side,
                                             args=(srv,), daemon=True).start()
                        elif up and srv._probes >= 3 and not srv._fixed_pipe:
                            srv._fixed_pipe = True
                            self.log(f"[{srv.label}] 状态管道未就绪，正在补挂…", "warn")
                            threading.Thread(target=self.ensure_server_side,
                                             args=(srv,), daemon=True).start()
            except Exception as exc:                       # noqa: BLE001
                self.log(f"本地轮询异常: {exc}", "error")
            time.sleep(CFG.get("poll_local", 2))

    def _poll_remote(self):
        while True:
            for srv in list(self.servers):
                try:
                    srv.poll()
                except Exception as exc:                   # noqa: BLE001
                    self.log(f"[{srv.label}] 轮询异常: {exc}", "error")
            time.sleep(CFG.get("poll_remote", 3))

    def ensure_server_side(self, srv):
        try:
            mid = machine_id()
            rc, out = srv.on_server(
                f"bridge-mount -c {mid} {shlex_quote(srv.status_dir)} --status 2>&1; "
                f"bridge-statusd start -c {mid} 2>&1", 60)
            bad = [l for l in out.splitlines() if l.startswith("ERR|")]
            if bad:
                self.log(f"[{srv.label}] 状态管道挂载失败: {bad[0][4:]}", "error")
            return not bad
        except Exception as exc:                           # noqa: BLE001
            self.log(f"[{srv.label}] 服务器端初始化异常: {exc}", "error")
            return False

    # ---- 给界面的快照
    def snapshot(self):
        out = []
        for srv in self.servers:
            planned = {}
            for e in srv.conf.get("mounts", []):
                planned.setdefault(mount_local(e), mount_target(e))
            rows = []
            for p in dict.fromkeys(list(planned) + list(srv.mounts.keys())):
                if not p or Server.is_internal(p):
                    continue
                mp = srv.mounts.get(p)
                rows.append({
                    "local": p,
                    "server": mp or planned.get(p)
                              or f"{srv.srv_mnt_root}/{default_mount_dir(p)}",
                    "mounted": bool(mp),
                })
            out.append({
                "id": srv.sid, "name": srv.label, "alias": srv.alias,
                "host": srv.conf.get("host", ""), "port": srv.port,
                "tunnel": srv.state["tunnel"], "server": srv.state["server"],
                "source": srv.state.get("source"), "fresh_s": srv.state.get("fresh_s"),
                "uptime": srv.state.get("uptime"),
                "auto_tunnel": bool(srv.conf.get("auto_tunnel")),
                "pipe_ok": srv.status_mounted, "mounts": rows,
                "mnt_root": srv.srv_mnt_root,
            })
        return {"sshd": self.sshd, "servers": out,
                "active": CFG.get("active"), "busy": self.busy,
                "platform": core.PLATFORM, "machine": machine_id()}


AGENT = None


# ================================================================ HTTP

def work(fn):
    """动作都在后台跑，HTTP 立刻返回 —— 挂载一次要好几秒，不能阻塞界面"""
    def wrapper():
        AGENT.busy = True
        try:
            fn()
        except Exception as exc:                           # noqa: BLE001
            AGENT.log(f"操作失败: {exc}", "error")
        finally:
            AGENT.busy = False
    threading.Thread(target=wrapper, daemon=True).start()


def do_mount(srv, local, target=""):
    def job():
        if not srv.port_ok:
            AGENT.log(f"[{srv.label}] 隧道未连接，无法挂载", "error")
            return
        AGENT.log(f"[{srv.label}] 挂载 {local} …")
        cmd = f"bridge-mount -c {machine_id()} {shlex_quote(local)}"
        if target:
            cmd += f" {shlex_quote(target)}"
        rc, out = srv.on_server(cmd, 60)
        line = out.strip().splitlines()[-1] if out.strip() else ""
        if line.startswith(("OK|", "ALREADY|")):
            AGENT.log(f"  → {line.split('|', 1)[1]}")
        else:
            AGENT.log(f"  失败: {line.split('|', 1)[-1] or out.strip()}", "error")
        srv.poll()
    work(job)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # 别把每条请求都打到控制台
        pass

    # ---- 鉴权：token 对不上一律拒绝
    def _authed(self, q):
        tok = self.headers.get("X-Ferry-Token") or (q.get("t", [""])[0])
        if not secrets.compare_digest(tok or "", TOKEN):
            return False
        # DNS rebinding：攻击者把域名解到 127.0.0.1，Host 头就不是 127.0.0.1 了
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path):
        full = os.path.normpath(os.path.join(UI_DIR, path.lstrip("/")))
        if not full.startswith(UI_DIR) or not os.path.isfile(full):
            self.send_error(404)
            return
        ext = os.path.splitext(full)[1]
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".png": "image/png",
                 ".svg": "image/svg+xml", ".ico": "image/x-icon"}.get(ext, "application/octet-stream")
        data = open(full, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/"):
            if not self._authed(q):
                return self._json({"error": "unauthorized"}, 403)
            return self._get_api(u.path, q)
        # 界面本身不校验 token（里面没有敏感数据），但它拿不到 token 就调不了 api
        self._file("index.html" if u.path in ("/", "") else u.path)

    def _get_api(self, path, q):
        if path == "/api/state":
            return self._json(AGENT.snapshot())
        if path == "/api/log":
            since = int(q.get("since", ["0"])[0])
            return self._json({"lines": AGENT.logs_since(since), "seq": AGENT.seq})
        if path == "/api/browse":
            srv = AGENT.by_id(q.get("id", [""])[0])
            if not srv:
                return self._json({"error": "no such server"}, 404)
            p = q.get("path", [""])[0]
            cmd = f"bridge-ls -c {machine_id()}"
            if p:
                cmd += f" {shlex_quote(p)}"
            rc, out = srv.on_server(cmd, 30)
            cwd, rows = None, []
            for line in out.splitlines():
                if line.startswith("CWD|"):
                    cwd = line[4:].strip()
                elif line.startswith("D|"):
                    parts = line.split("|")
                    rows.append({"name": parts[1],
                                 "flag": parts[2] if len(parts) > 2 else "free"})
            if cwd is None:
                return self._json({"error": out.strip().split("|")[-1] or "读取失败"}, 400)
            return self._json({"cwd": cwd, "dirs": rows})
        if path == "/api/pick-folder":
            # 网页开不了原生选择框，让 agent 用 tkinter 弹一个
            return self._json({"path": pick_folder()})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            return self._json({"error": "unauthorized"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        try:
            return self._post_api(u.path, body)
        except Exception as exc:                           # noqa: BLE001
            AGENT.log(f"接口异常 {u.path}: {exc}", "error")
            return self._json({"error": str(exc)}, 500)

    def _post_api(self, path, b):
        srv = AGENT.by_id(b.get("id", "")) if "id" in b else None

        if path == "/api/select":
            CFG["active"] = b.get("id")
            save_cfg(CFG)
            return self._json({"ok": True})

        if path == "/api/tunnel/start" and srv:
            if srv.tunnel_proc and srv.tunnel_proc.poll() is None:
                AGENT.log(f"[{srv.label}] 隧道已在运行", "warn")
                return self._json({"ok": True})

            def job():
                AGENT.log(f"[{srv.label}] 向服务器登记本机…")
                port, err = srv.register()
                if err:
                    AGENT.log(f"  登记失败: {err}", "error")
                    srv.diagnose(err)
                    return
                AGENT.log(f"  已登记，分配端口 {port}")
                AGENT.persist()
                AGENT.log(f"[{srv.label}] 启动隧道 -> {srv.alias}")
                srv.tunnel_spawn()
                srv.want_up = True
                time.sleep(2)
                AGENT.ensure_server_side(srv)
                srv.poll()
            work(job)
            return self._json({"ok": True})

        if path == "/api/tunnel/stop" and srv:
            def job():
                srv.want_up = False
                srv.tunnel_kill()
                AGENT.log(f"[{srv.label}] 隧道已停止")
                time.sleep(1)
                srv.poll()
            work(job)
            return self._json({"ok": True})

        if path == "/api/mount" and srv:
            local, target = b.get("local", ""), b.get("server", "")
            entries = srv.conf.setdefault("mounts", [])
            idx = next((i for i, e in enumerate(entries)
                        if mount_local(e) == local), -1)
            if target == f"{srv.srv_mnt_root}/{default_mount_dir(local)}":
                target = ""
            item = mount_entry(local, target)
            if idx >= 0:
                entries[idx] = item
            else:
                entries.append(item)
            AGENT.persist()
            do_mount(srv, local, target)
            return self._json({"ok": True})

        if path == "/api/umount" and srv:
            local = b.get("local", "")
            mp = srv.mounts.get(local)
            if not mp:
                return self._json({"error": "未挂载"}, 400)

            def job():
                AGENT.log(f"[{srv.label}] 卸载 {local} …")
                rc, out = srv.on_server(
                    f"bridge-umount -c {machine_id()} {shlex_quote(mp)}", 40)
                line = out.strip().splitlines()[-1] if out.strip() else ""
                ok = line.startswith("OK|")
                AGENT.log("  已卸载" if ok else f"  失败: {line.split('|', 1)[-1]}",
                          "info" if ok else "error")
                srv.poll()
            work(job)
            return self._json({"ok": True})

        if path == "/api/mount/remove" and srv:
            local = b.get("local", "")
            if local in srv.mounts:
                return self._json({"error": "正在挂载中，请先卸载"}, 400)
            srv.conf["mounts"] = [e for e in srv.conf.get("mounts", [])
                                  if mount_local(e) != local]
            AGENT.persist()
            AGENT.log(f"[{srv.label}] 已从列表移除 {local}")
            return self._json({"ok": True})

        if path == "/api/server/add-invite":
            d = parse_invite(b.get("token", ""))
            if not d:
                return self._json({"error": "接入码格式不对"}, 400)

            def job():
                AGENT.log(f"接入 {d['host']} …")
                alias, tips = apply_invite(d, AGENT.log)
                for t in tips:
                    AGENT.log("  " + t)
                sid = d.get("name") or alias
                if AGENT.by_id(sid):
                    AGENT.log(f"  {sid} 已存在，跳过", "warn")
                    return
                conf = {"id": sid, "name": sid, "ssh_alias": alias,
                        "host": d["host"], "win_user": core.local_user(),
                        "auto_tunnel": True, "enabled": True, "mounts": []}
                s = Server(conf, AGENT.log)
                s.ensure_status_dir()
                AGENT.servers.append(s)
                CFG["active"] = sid
                AGENT.persist()
                AGENT.log(f"  ✅ 已添加 {sid}")
            work(job)
            return self._json({"ok": True})

        if path == "/api/server/remove" and srv:
            if srv.user_mounts:
                return self._json({"error": "还有挂载，请先卸载",
                                   "mounts": list(srv.user_mounts)}, 400)

            def job():
                if srv.port_ok and srv.status_mounted:
                    try:
                        srv.on_server(f"bridge-umount -c {machine_id()} "
                                      f"{srv.srv_status_mp}", 25)
                    except Exception:                      # noqa: BLE001
                        pass
                srv.tunnel_kill()
                if srv in AGENT.servers:
                    AGENT.servers.remove(srv)
                AGENT.persist()
                AGENT.log(f"已移除 {srv.label}")
            work(job)
            return self._json({"ok": True})

        if path == "/api/sshd/start":
            def job():
                AGENT.log("正在启动本机 SSH 服务…")
                ok, msg = sshd_start()
                AGENT.log("  " + msg, "info" if ok else "error")
                if ok:
                    AGENT.sshd = {"ok": True, "text": "运行中"}
            work(job)
            return self._json({"ok": True})

        if path == "/api/panic":
            def job():
                for s in AGENT.servers:
                    s.want_up = False
                    s.tunnel_kill()
                ok, msg = sshd_stop()
                AGENT.log("已停止全部隧道。" + msg, "warn")
            work(job)
            return self._json({"ok": True})

        return self._json({"error": "not found"}, 404)


def pick_folder():
    """原生目录选择框。网页做不到，只能由 agent 这边弹。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        r = tk.Tk()
        r.withdraw()
        r.attributes("-topmost", True)
        p = filedialog.askdirectory(title="选择要挂载的本机文件夹", mustexist=True)
        r.destroy()
        return os.path.normpath(p) if p else ""
    except Exception:                                      # noqa: BLE001
        return ""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    global AGENT
    AGENT = Agent()
    # 把实际读的配置路径打出来 —— 从仓库副本启动会读到空配置，
    # 界面上只表现为"没有服务器"，不写清楚根本查不出来
    AGENT.log(f"Ferry agent 启动 · 配置 {CFG_PATH}")
    if not os.path.exists(CFG_PATH):
        AGENT.log("配置文件不存在，当作全新安装（左上角「＋ 添加」粘接入码即可）", "warn")
    elif not AGENT.servers:
        AGENT.log("配置里没有服务器 —— 确认这是你在用的那份配置", "warn")
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/?t={TOKEN}"
    # Electron 壳读这个文件拿地址；顺带当"已在运行"的标记
    with open(os.path.join(BASE_DIR, ".agent-url"), "w", encoding="utf-8") as fh:
        fh.write(url)
    print(url, flush=True)
    if "--open" in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.unlink(os.path.join(BASE_DIR, ".agent-url"))
        except OSError:
            pass


if __name__ == "__main__":
    main()
