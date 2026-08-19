#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ferry 一键接入 —— 在【你的电脑】上运行，自动完成两端配置。

    python3 ferry-setup.py root@1.2.3.4

它会：
  1. 检查本机环境（Python/Tk、sshd）
  2. 连上服务器，装好服务端工具（已装则跳过）
  3. 自动取回服务器公钥并授权到本机 —— 不用你复制粘贴
  4. 写好本机 SSH 别名
  5. 生成客户端配置并启动图形界面

需要手工介入的只有两处（都会明确提示）：
  · Windows：安装/启动 sshd 要管理员权限
  · macOS：打开「系统设置 → 通用 → 共享 → 远程登录」
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
HERE = os.path.dirname(os.path.abspath(__file__))
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0

C_OK, C_ERR, C_WARN, C_DIM, C_END = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"
if IS_WIN and not os.environ.get("WT_SESSION"):
    try:                                    # 让老 conhost 也认 ANSI
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:                       # noqa: BLE001
        C_OK = C_ERR = C_WARN = C_DIM = C_END = ""

STEP = 0


def step(title):
    global STEP
    STEP += 1
    print(f"\n{C_DIM}[{STEP}/6]{C_END} {title}")


def ok(m):    print(f"      {C_OK}✓{C_END} {m}")
def warn(m):  print(f"      {C_WARN}!{C_END} {m}")
def die(m, hint=""):
    print(f"      {C_ERR}✗ {m}{C_END}")
    if hint:
        print(f"        {hint}")
    sys.exit(1)


def run(args, timeout=120, stdin_text=None):
    """跑命令。必须用文件而非管道：Windows 的 ssh.exe 接管道会挂死。"""
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        try:
            rc = subprocess.call(
                args, stdout=fo, stderr=fe, creationflags=NO_WINDOW, timeout=timeout,
                stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE)
        except subprocess.TimeoutExpired:
            return -1, "超时"
        except FileNotFoundError:
            return -127, f"找不到命令: {args[0]}"
        fo.seek(0); fe.seek(0)
        return rc, (fo.read() + fe.read()).decode("utf-8", "replace")


def ssh(target, cmd, timeout=180, ident=None):
    a = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
         "-o", "StrictHostKeyChecking=accept-new"]
    if ident:
        a += ["-i", os.path.expanduser(ident)]
    return run(a + [target, cmd], timeout)


# ============================================================ 各步骤

def check_local():
    step("检查本机环境")
    print(f"      Python {sys.version.split()[0]}  ({sys.executable})")
    try:
        import tkinter
        tkv = float(tkinter.TkVersion)
        if tkv < 8.6:
            die(f"Tcl/Tk {tkinter.TkVersion} 在 macOS 上会崩溃",
                "执行 brew install python-tk，然后用 /opt/homebrew/bin/python3 重跑本脚本")
        ok(f"Tk {tkinter.TkVersion}")
    except ImportError:
        die("缺少 tkinter",
            "macOS: brew install python-tk    Ubuntu: sudo apt install python3-tk")


def check_sshd():
    step("检查本机 SSH 服务（服务器要靠它回连）")
    if IS_WIN:
        rc, out = run(["powershell", "-NoLogo", "-NonInteractive", "-Command",
                       "(Get-Service sshd -ErrorAction SilentlyContinue).Status"], 30)
        st = out.strip()
        if st == "Running":
            ok("sshd 运行中")
            return True
        warn(f"sshd 状态：{st or '未安装'}")
        print(f"""
      请用{C_WARN}管理员 PowerShell{C_END}执行（一整行），完成后回来继续：

        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0; Start-Service sshd; Set-Service sshd -StartupType Automatic
""")
        input("      配置好后按回车继续…")
        rc, out = run(["powershell", "-NoLogo", "-NonInteractive", "-Command",
                       "(Get-Service sshd -ErrorAction SilentlyContinue).Status"], 30)
        if out.strip() == "Running":
            ok("sshd 已就绪")
            return True
        die("sshd 仍未运行")
    else:
        import socket
        try:
            with socket.create_connection(("127.0.0.1", 22), timeout=3):
                ok("sshd 运行中")
                return True
        except OSError:
            pass
        if IS_MAC:
            print(f"""
      请打开{C_WARN}系统设置 → 通用 → 共享 → 远程登录{C_END}，完成后回来继续。
      （或终端执行： sudo systemsetup -setremotelogin on）
""")
        else:
            print("\n      请启动 sshd： sudo systemctl enable --now ssh\n")
        input("      配置好后按回车继续…")
        try:
            with socket.create_connection(("127.0.0.1", 22), timeout=3):
                ok("sshd 已就绪")
                return True
        except OSError:
            die("本机 22 端口仍未监听")


def install_server(target, ident):
    step(f"在服务器 {target} 上安装桥接工具")
    rc, out = ssh(target, "command -v bridge-register >/dev/null 2>&1 && echo INSTALLED",
                  60, ident)
    if rc != 0:
        die(f"连不上服务器：{out.strip()[:160]}",
            f"先确认能手动登录： ssh {target}")
    if "INSTALLED" in out:
        ok("服务端工具已安装，跳过")
    else:
        # 安装脚本从同目录的 server/ 取工具，所以要把两者一起送过去。
        # （早先安装脚本内嵌了一份工具副本，可以单文件 scp —— 但那份副本
        #   和 server/ 老是漂移，已经取消。）
        if not os.path.exists(os.path.join(HERE, "bridge-install.sh")):
            die("同目录下找不到 bridge-install.sh")
        if not os.path.isdir(os.path.join(HERE, "server")):
            die("同目录下找不到 server/ —— 请克隆整个仓库，别只拷单个脚本")
        print("      打包并上传安装脚本…")
        import tarfile, tempfile
        fd, tgz = tempfile.mkstemp(suffix=".tgz")
        os.close(fd)
        try:
            with tarfile.open(tgz, "w:gz") as tf:
                tf.add(os.path.join(HERE, "bridge-install.sh"), "bridge-install.sh")
                tf.add(os.path.join(HERE, "server"), "server")
            a = ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
            if ident:
                a += ["-i", os.path.expanduser(ident)]
            rc, out = run(a + [tgz, f"{target}:/tmp/ferry-install.tgz"], 180)
        finally:
            try:
                os.unlink(tgz)
            except OSError:
                pass
        if rc != 0:
            die(f"上传失败：{out.strip()[:160]}")
        rc, out = ssh(target,
                      "rm -rf /tmp/ferry-install && mkdir -p /tmp/ferry-install && "
                      "tar xzf /tmp/ferry-install.tgz -C /tmp/ferry-install && "
                      "bash /tmp/ferry-install/bridge-install.sh 2>&1 | tail -40; "
                      "rm -rf /tmp/ferry-install /tmp/ferry-install.tgz", 300, ident)
        if rc != 0 or "4/4" not in out:
            die(f"安装失败：\n{out.strip()[-400:]}")
        ok("服务端工具安装完成")

    rc, out = ssh(target, "cat /root/.ssh/id_bridge.pub 2>/dev/null || "
                          "cat /root/.ssh/id_win.pub 2>/dev/null", 30, ident)
    key = next((l.strip() for l in out.splitlines() if l.startswith("ssh-")), "")
    if not key:
        die("取不到服务器公钥")
    ok(f"已取回服务器公钥 …{key.split()[1][-12:]}")
    return key


def authorize(key):
    step("把服务器公钥授权到本机（追加，不影响已有密钥）")
    tag = key.split()[-1]
    if IS_WIN:
        ps = f'''
$g = (Get-LocalGroup -SID 'S-1-5-32-544').Name
$admin = [bool](Get-LocalGroupMember -Group $g -EA SilentlyContinue | Where-Object {{ $_.Name -like "*\\$env:USERNAME" }})
if ($admin) {{ $f = 'C:\\ProgramData\\ssh\\administrators_authorized_keys' }}
else {{ New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\\.ssh" | Out-Null
        $f = "$env:USERPROFILE\\.ssh\\authorized_keys" }}
if ((Test-Path $f) -and (Select-String -Path $f -SimpleMatch '{tag}' -Quiet)) {{ 'SKIP' }}
else {{ Add-Content $f '{key}' -Encoding ASCII; 'ADDED' }}
if ($admin) {{ icacls $f /inheritance:r /grant "${{g}}:F" /grant "SYSTEM:F" | Out-Null }}
Restart-Service sshd -EA SilentlyContinue
'''
        rc, out = run(["powershell", "-NoLogo", "-NonInteractive", "-Command", ps], 60)
        if "ADDED" in out:
            ok("公钥已授权")
        elif "SKIP" in out:
            ok("公钥已存在，跳过")
        else:
            die(f"授权失败：{out.strip()[:200]}",
                "若提示权限不足，请用管理员 PowerShell 重跑本脚本")
    else:
        d = os.path.expanduser("~/.ssh")
        os.makedirs(d, mode=0o700, exist_ok=True)
        ak = os.path.join(d, "authorized_keys")
        cur = open(ak, encoding="utf-8").read() if os.path.exists(ak) else ""
        if tag in cur:
            ok("公钥已存在，跳过")
        else:
            with open(ak, "a", encoding="utf-8") as fh:
                fh.write(("" if cur.endswith("\n") or not cur else "\n") + key + "\n")
            os.chmod(ak, 0o600)
            ok("公钥已授权")


def write_alias(target, ident):
    step("写入本机 SSH 别名")
    user, _, host = target.rpartition("@")
    user = user or "root"
    alias = "ferry-" + re.sub(r"[^A-Za-z0-9]", "-", host).strip("-")
    cfg = os.path.expanduser("~/.ssh/config")
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    cur = open(cfg, encoding="utf-8").read() if os.path.exists(cfg) else ""
    if re.search(rf"^\s*Host\s+{re.escape(alias)}\s*$", cur, re.M):
        ok(f"别名 {alias} 已存在")
        return alias
    if os.path.exists(cfg):
        shutil.copy2(cfg, cfg + ".bak")
    with open(cfg, "a", encoding="utf-8") as fh:
        fh.write(f"\nHost {alias}\n    HostName {host}\n    User {user}\n")
        if ident:
            fh.write(f"    IdentityFile {ident}\n")
        fh.write("    ServerAliveInterval 30\n    ServerAliveCountMax 3\n"
                 "    StrictHostKeyChecking accept-new\n")
    os.chmod(cfg, 0o600)
    ok(f"别名 {alias} → {user}@{host}")
    return alias


def write_client_cfg(alias, host):
    step("生成客户端配置")
    p = os.path.join(HERE, "bridge-config.json")
    try:
        cfg = json.load(open(p, encoding="utf-8"))
    except Exception:                        # noqa: BLE001
        cfg = {}
    cfg.setdefault("servers", [])
    if any(s.get("ssh_alias") == alias for s in cfg["servers"]):
        ok("该服务器已在客户端列表中")
    else:
        cfg["servers"].append({
            "id": alias, "name": host, "ssh_alias": alias, "host": host,
            "identity": None, "mounts": [], "auto_tunnel": True, "enabled": True,
        })
        cfg.setdefault("active", alias)
        json.dump(cfg, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        ok(f"已加入客户端：{host}")


def launch():
    """把 agent 拉起来并用浏览器打开。

    客户端已经是 Electron + Python agent 了 —— 这里只起 agent，
    界面走浏览器；想要原生窗口就 cd electron && npm start。
    """
    step("启动控制台")
    agent = os.path.join(HERE, "ferry_agent.py")
    if not os.path.exists(agent):
        warn("同目录下没有 ferry_agent.py，跳过")
        return
    exe = sys.executable
    if IS_WIN and exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            exe = cand
    subprocess.Popen([exe, agent, "--open"], cwd=HERE, creationflags=NO_WINDOW)
    ok("已启动，浏览器会自动打开控制台")
    print("      想要原生窗口： cd electron && npm install && npm start")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    ident = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--identity=")), None)

    print(f"""
{C_DIM}══════════════════════════════════════════════════════════{C_END}
  Ferry 一键接入
{C_DIM}══════════════════════════════════════════════════════════{C_END}""")

    target = args[0] if args else ""
    if not target:
        target = input("\n  服务器地址（如 root@1.2.3.4）: ").strip()
    if not target:
        die("未提供服务器地址")
    if "@" not in target:
        target = "root@" + target
    host = target.rpartition("@")[2]

    check_local()
    check_sshd()
    key = install_server(target, ident)
    authorize(key)
    alias = write_alias(target, ident)
    write_client_cfg(alias, host)
    launch()

    print(f"""
{C_DIM}══════════════════════════════════════════════════════════{C_END}
  {C_OK}完成{C_END}   服务器 {host}   别名 {alias}

  之后在服务器上可用：
    bridge-check            看接入的机器
    bridge-find <关键词>    秒查文件（先在界面挂目录）
    bridge-run "命令"       在你本机原生执行
{C_DIM}══════════════════════════════════════════════════════════{C_END}
""")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  已取消")
        sys.exit(130)
