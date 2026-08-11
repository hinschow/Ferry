#!/bin/bash
# ============================================================================
#  Windows <-> Linux 桥接：服务器端一键安装
#
#  用法：把本文件放到新服务器上，然后
#        bash bridge-install.sh <你的Windows用户名> [隧道端口，默认2222]
#    例：bash bridge-install.sh yourname
#
#  安装完会打印一段公钥和 Windows 侧要做的两步操作。
# ============================================================================
set -e

WIN_USER="${1:-}"
WIN_PORT="${2:-2222}"
[ -z "$WIN_USER" ] && { echo "用法: bash bridge-install.sh <Windows用户名> [隧道端口]"; exit 1; }

echo "==> 1/5 检查依赖"
if ! command -v sshfs >/dev/null 2>&1; then
  echo "    安装 sshfs ..."
  (apt-get update -qq && apt-get install -y -qq sshfs) >/dev/null 2>&1 \
    || { echo "    ❌ sshfs 安装失败，请手动安装后重试"; exit 1; }
fi
[ -e /dev/fuse ] || { echo "    ❌ 无 /dev/fuse，本机（容器？）不支持 FUSE 挂载"; exit 1; }
command -v iconv >/dev/null 2>&1 || echo "    ⚠️ 无 iconv，winrun 的中文支持会受影响"
echo "    sshfs $(sshfs --version 2>&1 | grep -o 'SSHFS version.*' || echo ok) / FUSE 可用"

echo "==> 2/5 创建目录与配置"
mkdir -p /root/.winbridge /root/mnt /root/local-project
mkdir -p /root/.winbridge/clients /root/.winbridge/status
CLIENT_NAME="${3:-client1}"
CLIENT_OS="${4:-windows}"
cat > /root/.winbridge/clients/${CLIENT_NAME}.conf <<CFGEOF
NAME=$CLIENT_NAME
OS=$CLIENT_OS
USER="$WIN_USER"
PORT=$WIN_PORT
LABEL='$CLIENT_NAME'
TOOL_DIR='D:\\bridge-console'
CFGEOF
echo "$CLIENT_NAME" > /root/.winbridge/current
cat > /root/.winbridge/config <<CFGEOF
WIN_USER="$WIN_USER"
WIN_PORT=$WIN_PORT
WIN_PROJECT=""                 # 默认项目路径（可留空）
MOUNT_POINT=/root/local-project
STATUS_DIR='/root/local-project/bridge-console'   # 状态文件只写这里，不碰用户项目
TOOL_DIR_WIN='C:\\bridge-console'   # 客户端工具目录（客户端连上后会自动上报覆盖）
CFGEOF
echo "    /root/.winbridge/config"

echo "==> 3/5 生成专用密钥"
if [ -f /root/.ssh/id_win ]; then
  echo "    已存在，跳过"
else
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  ssh-keygen -t ed25519 -f /root/.ssh/id_win -N '' -C "remote-to-windows-sshfs-$(hostname)" -q
  echo "    已生成 /root/.ssh/id_win"
fi

echo "==> 4/5 安装命令行工具"

cat > /root/.winbridge/lib.sh <<'LIB_EOF'
# 客户端解析：-c <名字> > $BRIDGE_CLIENT > current 文件
bridge_resolve_client() {
  BRIDGE_C=""
  if [ "$1" = "-c" ]; then BRIDGE_C="$2"; BRIDGE_SHIFT=2; else BRIDGE_SHIFT=0; fi
  [ -z "$BRIDGE_C" ] && BRIDGE_C="${BRIDGE_CLIENT:-}"
  [ -z "$BRIDGE_C" ] && BRIDGE_C=$(cat /root/.winbridge/current 2>/dev/null)
  [ -z "$BRIDGE_C" ] && { echo "ERR|未指定客户端，用 -c <名字> 或设置 /root/.winbridge/current"; return 1; }
  CONF="/root/.winbridge/clients/${BRIDGE_C}.conf"
  [ -f "$CONF" ] || { echo "ERR|找不到客户端档案: $CONF"; return 1; }
  # shellcheck disable=SC1090
  . "$CONF"
  CLIENT="$NAME"
  MNT_ROOT="/root/mnt/$CLIENT"              # 每个客户端独立挂载根，避免撞名
  STATUS_DIR="/root/.winbridge/status/$CLIENT"
  return 0
}

# 本地路径 -> sftp 路径（Windows 要转盘符，POSIX 原样）
bridge_sftp_path() {
  case "$OS" in
    windows) printf '/%s' "$(printf '%s' "$1" | tr '\\' '/')" ;;
    *)       printf '%s' "$1" ;;
  esac
}

# 路径 -> 挂载点目录名
bridge_mount_name() {
  printf '%s' "$1" | tr '\\/: ' '____' | sed 's/__*/_/g; s/^_//; s/_$//'
}
LIB_EOF

cat > /usr/local/bin/bridge-run <<'TOOL_bridge_run_EOF'
#!/bin/bash
# bridge-run [-c 客户端] [-d 工作目录] "命令"   — 在本地机器上原生执行
. /root/.winbridge/lib.sh
bridge_resolve_client "$@" || exit 1
[ $BRIDGE_SHIFT -gt 0 ] && shift $BRIDGE_SHIFT
WD=""; if [ "$1" = "-d" ]; then WD="$2"; shift 2; fi
[ -z "$1" ] && { echo "用法: bridge-run [-c 客户端] [-d 目录] \"命令\""; exit 1; }

SSH_OPTS=(-i /root/.ssh/id_win -p "$PORT"
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR -o ConnectTimeout=15
  -o ControlMaster=auto -o ControlPath="/root/.winbridge/cm-${CLIENT}-%C" -o ControlPersist=60m)

if [ "$OS" = "windows" ]; then
  PS=$(printf '$ProgressPreference="SilentlyContinue"; [Console]::OutputEncoding=[Text.Encoding]::UTF8; %s%s' \
        "$( [ -n "$WD" ] && printf 'Set-Location "%s"; ' "$WD" )" "$*")
  ENC=$(printf '%s' "$PS" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
  exec ssh "${SSH_OPTS[@]}" "$USER@127.0.0.1" \
      "powershell -NoLogo -NonInteractive -EncodedCommand $ENC" \
      2> >(grep -v -e '^#< CLIXML' -e '^<Objs ' >&2)
else
  # macOS / Linux：直接 bash，无需编码转换
  CMD="$( [ -n "$WD" ] && printf 'cd %q && ' "$WD" )$*"
  exec ssh "${SSH_OPTS[@]}" "$USER@127.0.0.1" "bash -lc $(printf '%q' "$CMD")"
fi
TOOL_bridge_run_EOF

cat > /usr/local/bin/bridge-mount <<'TOOL_bridge_mount_EOF'
#!/bin/bash
# bridge-mount [-c 客户端] <本地路径> [挂载点]
. /root/.winbridge/lib.sh
bridge_resolve_client "$@" || exit 1
[ $BRIDGE_SHIFT -gt 0 ] && shift $BRIDGE_SHIFT
LPATH="$1"
[ -z "$LPATH" ] && { echo "ERR|用法: bridge-mount [-c 客户端] <本地路径> [挂载点]"; exit 1; }

SFTP_PATH=$(bridge_sftp_path "$LPATH")
MP="${2:-$MNT_ROOT/$(bridge_mount_name "$LPATH")}"

mountpoint -q "$MP" && { echo "ALREADY|$MP"; exit 0; }
mkdir -p "$MP" || { echo "ERR|无法创建挂载点 $MP"; exit 1; }

ERR=$(sshfs -p "$PORT" "$USER@127.0.0.1:$SFTP_PATH" "$MP" \
  -o IdentityFile=/root/.ssh/id_win,reconnect,ServerAliveInterval=15,ServerAliveCountMax=3 \
  -o compression=yes,max_conns=4 \
  -o cache=yes,kernel_cache,cache_timeout=60 \
  -o attr_timeout=15,entry_timeout=15,negative_timeout=5 \
  -o uid=0,gid=0,StrictHostKeyChecking=no,UserKnownHostsFile=/dev/null 2>&1)

if mountpoint -q "$MP"; then echo "OK|$MP"; else
  rmdir "$MP" 2>/dev/null; echo "ERR|${ERR:-挂载失败，检查路径是否存在}"; exit 1
fi
TOOL_bridge_mount_EOF

cat > /usr/local/bin/bridge-umount <<'TOOL_bridge_umount_EOF'
#!/bin/bash
. /root/.winbridge/lib.sh
bridge_resolve_client "$@" || exit 1
[ $BRIDGE_SHIFT -gt 0 ] && shift $BRIDGE_SHIFT
MP="$1"
[ -z "$MP" ] && { echo "ERR|用法: bridge-umount [-c 客户端] <挂载点>"; exit 1; }
fusermount3 -u "$MP" 2>/dev/null || fusermount -u "$MP" 2>/dev/null
mountpoint -q "$MP" && { echo "ERR|卸载失败，可能有进程正在使用"; exit 1; }
case "$MP" in /root/mnt/*) rmdir "$MP" 2>/dev/null ;; esac
echo "OK|$MP"
TOOL_bridge_umount_EOF

cat > /usr/local/bin/bridge-mounts <<'TOOL_bridge_mounts_EOF'
#!/bin/bash
# bridge-mounts [-c 客户端]   不带 -c 则列出全部
. /root/.winbridge/lib.sh
if [ "$1" = "-c" ]; then
  bridge_resolve_client "$@" || exit 1
  awk -v p="$MNT_ROOT" -v s="/root/.winbridge/status/$CLIENT" \
    '$3=="fuse.sshfs" && (index($2,p)==1 || $2==s) {gsub(/\\040/," ",$2); print $2"\t"$1}' /proc/mounts
else
  awk '$3=="fuse.sshfs" {gsub(/\\040/," ",$2); print $2"\t"$1}' /proc/mounts
fi
TOOL_bridge_mounts_EOF

cat > /usr/local/bin/bridge-check <<'TOOL_bridge_check_EOF'
#!/bin/bash
# bridge-check [-c 客户端]   不带 -c 则检查全部
. /root/.winbridge/lib.sh
check_one() {
  local conf="$1"; ( . "$conf"
    CLIENT="$NAME"; MNT_ROOT="/root/mnt/$CLIENT"; STATUS_DIR="/root/.winbridge/status/$CLIENT"
    printf '\n══ %s (%s / %s) ══\n' "$CLIENT" "$OS" "$LABEL"
    if ss -tln 2>/dev/null | grep -q ":$PORT "; then echo "  隧道端口 $PORT   ✅ 已监听"
    else echo "  隧道端口 $PORT   ❌ 未监听 —— 该机器未建立隧道"; return 1; fi
    local probe
    if [ "$OS" = "windows" ]; then probe='Write-Output OK'; else probe='echo OK'; fi
    if bridge-run -c "$CLIENT" "$probe" 2>/dev/null | grep -q OK; then echo "  免密登录     ✅ 成功"
    else echo "  免密登录     ❌ 失败"; return 1; fi
    local n; n=$(bridge-mounts -c "$CLIENT" | wc -l)
    echo "  挂载数量     $n"
    local f="$STATUS_DIR/.bridge-status.json"
    if [ -f "$f" ]; then
      local ts; ts=$(grep -o '"ts":[0-9]*' "$f" 2>/dev/null | cut -d: -f2)
      [ -n "$ts" ] && echo "  状态管道     ✅ $(( $(date +%s) - ts ))秒前" || echo "  状态管道     ⚠️ 文件异常"
    else echo "  状态管道     ⚠️ 未挂载（客户端会回退 SSH 探测）"; fi
  )
}
if [ "$1" = "-c" ]; then check_one "/root/.winbridge/clients/$2.conf"
else for f in /root/.winbridge/clients/*.conf; do check_one "$f"; done; fi
TOOL_bridge_check_EOF

cat > /usr/local/bin/bridge-grep <<'TOOL_bridge_grep_EOF'
#!/bin/bash
# bridge-grep [-c 客户端] [-d 目录] <pattern> [路径...]  — 用本地机器的 ripgrep 搜索
. /root/.winbridge/lib.sh
bridge_resolve_client "$@" || exit 1
[ $BRIDGE_SHIFT -gt 0 ] && shift $BRIDGE_SHIFT
D=(); if [ "$1" = "-d" ]; then D=(-d "$2"); shift 2; fi
PAT="$1"; shift
[ -z "$PAT" ] && { echo "用法: bridge-grep [-c 客户端] [-d 目录] <pattern> [路径]"; exit 1; }
P="$*"; [ -z "$P" ] && P="."
# 显式路径是必须的：rg 在非交互 stdin 下若无路径会永远等 stdin
bridge-run -c "$CLIENT" "${D[@]}" "rg -nS --no-heading --color never -M 300 '$PAT' $P"
TOOL_bridge_grep_EOF

cat > /usr/local/bin/bridge-git <<'TOOL_bridge_git_EOF'
#!/bin/bash
# bridge-git [-c 客户端] [-d 目录] <git参数>  — git 在本地机器上跑
. /root/.winbridge/lib.sh
bridge_resolve_client "$@" || exit 1
[ $BRIDGE_SHIFT -gt 0 ] && shift $BRIDGE_SHIFT
D=(); if [ "$1" = "-d" ]; then D=(-d "$2"); shift 2; fi
bridge-run -c "$CLIENT" "${D[@]}" "git --no-pager $*"
TOOL_bridge_git_EOF

cat > /usr/local/bin/bridge-reset <<'TOOL_bridge_reset_EOF'
#!/bin/bash
rm -f /root/.winbridge/cm-* 2>/dev/null
echo "已清理 SSH 复用连接"
TOOL_bridge_reset_EOF

cat > /usr/local/bin/bridge-statusd <<'TOOL_bridge_statusd_EOF'
#!/bin/bash
# bridge-statusd start|stop|status [-c 客户端]   不带 -c 则对所有客户端操作
ACT="$1"; shift
. /root/.winbridge/lib.sh

one() {
  local conf="$1" act="$2"
  ( . "$conf"
    CLIENT="$NAME"; MNT_ROOT="/root/mnt/$CLIENT"; SDIR="/root/.winbridge/status/$CLIENT"
    PIDF="/root/.winbridge/statusd-$CLIENT.pid"
    case "$act" in
      start)
        if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
          echo "  $CLIENT: 已在运行 (pid $(cat "$PIDF"))"; return 0; fi
        loop() {
          local NAME_F=.bridge-status.json INTERVAL=3
          while true; do
            local TS PORT_OK UP MJSON mp src JSON
            TS=$(date +%s)
            if ss -tln 2>/dev/null | grep -q ":${PORT} "; then PORT_OK=true; else PORT_OK=false; fi
            UP=$(uptime -p 2>/dev/null | tr '"' '@')
            MJSON=""
            while IFS=$'\t' read -r mp src; do
              [ -z "$mp" ] && continue
              MJSON="${MJSON:+$MJSON,}{\"mount\":\"$mp\",\"src\":\"$src\"}"
            done < <(awk -v p="$MNT_ROOT" -v s="$SDIR" \
                 '$3=="fuse.sshfs" && (index($2,p)==1 || $2==s){gsub(/\\040/," ",$2); print $2"\t"$1}' /proc/mounts)
            JSON="{\"ts\":$TS,\"client\":\"$CLIENT\",\"os\":\"$OS\",\"port_ok\":$PORT_OK,\"uptime\":\"$UP\",\"mounts\":[$MJSON]}"
            if [ -d "$SDIR" ]; then
              ( printf '%s' "$JSON" > "$SDIR/$NAME_F" ) & local p=$!
              ( sleep 5; kill $p 2>/dev/null ) & local w=$!
              wait $p 2>/dev/null; kill $w 2>/dev/null
            fi
            sleep $INTERVAL
          done
        }
        setsid bash -c "PORT=$PORT CLIENT=$CLIENT OS=$OS MNT_ROOT=$MNT_ROOT SDIR=$SDIR
                        $(declare -f loop); loop" >/dev/null 2>&1 < /dev/null &
        echo $! > "$PIDF"; echo "  $CLIENT: 已启动 (pid $!)" ;;
      stop)
        [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null && rm -f "$PIDF" \
          && echo "  $CLIENT: 已停止" || echo "  $CLIENT: 未在运行" ;;
      status)
        if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
          echo "  $CLIENT: 运行中 (pid $(cat "$PIDF"))"; else echo "  $CLIENT: 未运行"; fi ;;
    esac )
}

case "$ACT" in
  start|stop|status)
    if [ "$1" = "-c" ]; then one "/root/.winbridge/clients/$2.conf" "$ACT"
    else for f in /root/.winbridge/clients/*.conf; do one "$f" "$ACT"; done; fi ;;
  *) echo "用法: bridge-statusd start|stop|status [-c 客户端]"; exit 1 ;;
esac
TOOL_bridge_statusd_EOF

cat > /usr/local/bin/bridge-daemon <<'TOOL_bridge_daemon_EOF'
#!/bin/bash
# bridge-daemon start <名字> [-c 客户端] [-d 工作目录] '<命令>'
# bridge-daemon log <名字> [-c 客户端] [行数] | stop <名字> [-c 客户端] | list [-c 客户端]
#
# 在本地机器上启动脱离 SSH 会话的长驻进程。
#   Windows: 必须用计划任务（OpenSSH 会在会话结束时杀掉整个子进程树）
#   macOS: 无 setsid 命令，用子 shell + nohup（Linux 优先 setsid）
ACT="$1"; DNAME="$2"; shift 2 2>/dev/null
. /root/.winbridge/lib.sh
bridge_resolve_client "$@" || exit 1
[ $BRIDGE_SHIFT -gt 0 ] && shift $BRIDGE_SHIFT
NAME="$DNAME"          # 客户端档案里的 NAME= 会覆盖它，所以在 source 之后恢复

# daemon 脚本与日志放进【已挂载的状态目录】，不再为此单独挂载工具目录
DAEMON_SRV="/root/.winbridge/status/$CLIENT/daemons"
if [ "$OS" = "windows" ]; then
  DAEMON_LOCAL="${STATUS_LOCAL:-${TOOL_DIR}\\status\\${STATUS_SUB:-$CLIENT}}\\daemons"
else
  DAEMON_LOCAL="${STATUS_LOCAL:-${TOOL_DIR}/status/${STATUS_SUB:-$CLIENT}}/daemons"
fi
mkdir -p "$DAEMON_SRV" 2>/dev/null

case "$ACT" in
  start)
    WD=""; if [ "$1" = "-d" ]; then WD="$2"; shift 2; fi
    CMD="$*"
    { [ -z "$NAME" ] || [ -z "$CMD" ]; } && { echo "用法: bridge-daemon start <名字> [-c 客户端] [-d 目录] '<命令>'"; exit 1; }

    if [ "$OS" = "windows" ]; then
      {
        echo "\$ErrorActionPreference = 'Continue'"
        echo "\$OutputEncoding = [Text.UTF8Encoding]::new(\$false)"
        echo "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(\$false)"
        echo "chcp 65001 > \$null"
        echo "\$log = '${DAEMON_LOCAL}\\${NAME}.log'"
        [ -n "$WD" ] && echo "Set-Location '$WD'"
        echo "Add-Content \$log -Encoding UTF8 (\"=== started \" + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + \" ===\")"
        echo "& {"; echo "$CMD"; echo "} *>&1 | ForEach-Object { Add-Content \$log (\"\$_\") -Encoding UTF8 }"
        echo "Add-Content \$log -Encoding UTF8 (\"=== exited \" + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + \" ===\")"
      } > "$DAEMON_SRV/${NAME}.ps1"
      rm -f "$DAEMON_SRV/${NAME}.log"
      bridge-run -c "$CLIENT" "schtasks /delete /tn 'bridge_${CLIENT}_${NAME}' /f 2>&1 | Out-Null
schtasks /create /tn 'bridge_${CLIENT}_${NAME}' /tr 'powershell -NoLogo -WindowStyle Hidden -ExecutionPolicy Bypass -File \"${DAEMON_LOCAL}\\${NAME}.ps1\"' /sc once /st 00:00 /f 2>&1 | Out-Null
schtasks /run /tn 'bridge_${CLIENT}_${NAME}' 2>&1 | Out-Null
Write-Output 'started: ${NAME}'"
    else
      {
        echo "#!/bin/bash"
        echo "log='${DAEMON_LOCAL}/${NAME}.log'"
        [ -n "$WD" ] && echo "cd $(printf '%q' "$WD") || exit 1"
        echo 'echo "=== started $(date "+%Y-%m-%d %H:%M:%S") ===" >> "$log"'
        echo "{"; echo "$CMD"; echo '} >> "$log" 2>&1'
        echo 'echo "=== exited $(date "+%Y-%m-%d %H:%M:%S") ===" >> "$log"'
      } > "$DAEMON_SRV/${NAME}.sh"
      chmod +x "$DAEMON_SRV/${NAME}.sh" 2>/dev/null
      rm -f "$DAEMON_SRV/${NAME}.log"
      bridge-run -c "$CLIENT" "mkdir -p '${DAEMON_LOCAL}'; chmod +x '${DAEMON_LOCAL}/${NAME}.sh'; \
echo \$\$ > '${DAEMON_LOCAL}/${NAME}.pid'; \
(setsid '${DAEMON_LOCAL}/${NAME}.sh' >/dev/null 2>&1 </dev/null &) 2>/dev/null \
  || (nohup '${DAEMON_LOCAL}/${NAME}.sh' >/dev/null 2>&1 </dev/null &); \
echo 'started: ${NAME}'"
    fi ;;

  log)  tail -n "${1:-40}" "$DAEMON_SRV/${NAME}.log" 2>/dev/null || echo "(无日志)" ;;

  stop)
    if [ "$OS" = "windows" ]; then
      bridge-run -c "$CLIENT" "schtasks /end /tn 'bridge_${CLIENT}_${NAME}' 2>&1 | Out-Null
schtasks /delete /tn 'bridge_${CLIENT}_${NAME}' /f 2>&1 | Out-Null
Write-Output 'stopped: ${NAME}'"
    else
      bridge-run -c "$CLIENT" "pkill -f '${DAEMON_LOCAL}/${NAME}.sh' && echo 'stopped: ${NAME}' || echo 'not running'"
    fi ;;

  list)
    if [ "$OS" = "windows" ]; then
      bridge-run -c "$CLIENT" "schtasks /query /fo table 2>\$null | Select-String 'bridge_${CLIENT}_'"
    else
      bridge-run -c "$CLIENT" "pgrep -fl '${DAEMON_LOCAL}/' || echo '(无)'"
    fi ;;
  *) echo "用法: bridge-daemon start|log|stop|list <名字> [-c 客户端] ..."; exit 1 ;;
esac
TOOL_bridge_daemon_EOF

chmod +x /usr/local/bin/bridge-run /usr/local/bin/bridge-mount /usr/local/bin/bridge-umount /usr/local/bin/bridge-mounts /usr/local/bin/bridge-check /usr/local/bin/bridge-grep /usr/local/bin/bridge-git /usr/local/bin/bridge-reset /usr/local/bin/bridge-statusd /usr/local/bin/bridge-daemon 
for p in win-run:bridge-run win-check:bridge-check win-mounts:bridge-mounts win-grep:bridge-grep win-git:bridge-git win-reset:bridge-reset win-daemon:bridge-daemon; do
  ln -sfn "/usr/local/bin/${p##*:}" "/usr/local/bin/${p%%:*}"; done
echo "    bridge-run bridge-mount bridge-umount bridge-mounts bridge-check bridge-grep bridge-git bridge-statusd bridge-daemon（含 win-* 兼容软链）"
echo "==> 5/5 完成"
SRV_IP=$(curl -s -m 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
HOSTN=$(hostname)

cat <<TIPEOF

════════════════════════════════════════════════════════════════════
 服务器端装好了。接下来在你的 Windows 上做两步。
════════════════════════════════════════════════════════════════════

【第 1 步】授权本服务器的公钥（管理员 PowerShell，纯追加，不影响已有密钥）

\$key = '$(cat /root/.ssh/id_win.pub)'
\$g = (Get-LocalGroup -SID 'S-1-5-32-544').Name
\$admin = [bool](Get-LocalGroupMember -Group \$g -EA SilentlyContinue | Where-Object { \$_.Name -like "*\\\$env:USERNAME" })
\$f = if (\$admin) { 'C:\ProgramData\ssh\administrators_authorized_keys' } else { "\$env:USERPROFILE\.ssh\authorized_keys" }
if (-not (Test-Path \$f) -or -not (Select-String -Path \$f -SimpleMatch '$(hostname)' -Quiet)) { Add-Content \$f \$key }
if (\$admin) { icacls \$f /inheritance:r /grant "\${g}:F" /grant "SYSTEM:F" | Out-Null }
Restart-Service sshd
Get-Content \$f


【第 2 步】加 SSH 别名（普通 PowerShell 即可，追加到 ~/.ssh/config）

\$c = "\$env:USERPROFILE\.ssh\config"
Add-Content \$c ""
Add-Content \$c "Host $HOSTN"
Add-Content \$c "    HostName $SRV_IP"
Add-Content \$c "    User root"
Add-Content \$c "    IdentityFile ~/.ssh/你连这台服务器用的私钥"
Add-Content \$c "    RemoteForward $WIN_PORT 127.0.0.1:22"
Add-Content \$c "    ServerAliveInterval 30"
Add-Content \$c "    StrictHostKeyChecking accept-new"

  ⚠️ IdentityFile 那行要改成你实际连这台服务器用的私钥路径。
  ⚠️ 回环地址必须写 127.0.0.1，不能写 localhost（Windows 会解析成 ::1）。


【第 3 步】建隧道（保持窗口开着，或用桌面客户端管理）

ssh -N $HOSTN


【验证】回到服务器上执行

win-check                              # 隧道 + 免密 + 路径
win-mount 'D:\你的项目路径'            # 挂载
win-statusd start                      # 状态守护（桌面客户端用）
winrun "echo hello"                    # 在 Windows 上执行命令

════════════════════════════════════════════════════════════════════
TIPEOF
