#!/bin/bash
# ============================================================================
#  Windows / macOS / Linux <-> Linux 服务器 桥接：服务器端一键安装
#
#  用法：把本文件放到新服务器上，直接执行（无需任何参数）
#        bash bridge-install.sh
#
#  安装完会打印一段公钥和本地电脑要做的操作。
#  客户端首次连接时会自动上报自己的用户名/系统/工具路径，并领取一个
#  不冲突的隧道端口 —— 服务器这边不需要预先配置任何机器信息。
# ============================================================================
set -e

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
mkdir -p /root/.winbridge/clients /root/.winbridge/status /root/.winbridge/index /root/mnt
# 不预建客户端档案 —— 客户端首次连接时由 bridge-register 用真实信息自动创建
cat > /root/.winbridge/config <<'CFGEOF'
# 全局默认值。每台本地机的信息在 clients/<机器名>.conf，由客户端自动上报生成。
MOUNT_POINT=/root/local-project
CFGEOF
echo "    /root/.winbridge/config（客户端档案将由 bridge-register 自动创建）"

echo "==> 3/5 生成专用密钥"
if [ -f /root/.ssh/id_bridge ]; then
  echo "    已存在，跳过"
elif [ -f /root/.ssh/id_win ]; then
  # 兼容早期版本：沿用已有密钥，建软链统一入口
  ln -sf /root/.ssh/id_win /root/.ssh/id_bridge
  ln -sf /root/.ssh/id_win.pub /root/.ssh/id_bridge.pub
  echo "    沿用已有密钥 /root/.ssh/id_win"
else
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  ssh-keygen -t ed25519 -f /root/.ssh/id_bridge -N '' -C "bridge@$(hostname)" -q
  echo "    已生成 /root/.ssh/id_bridge"
fi

cat > /root/.winbridge/index-exclude.txt <<'EXCEOF'
# bridge-index 的排除规则（每行一个目录名，匹配任意层级）
# 注意：git 仓库优先用 git ls-files，天然遵守 .gitignore，通常用不到这里
.git
node_modules
__pycache__
.venv
venv
.tox
.mypy_cache
.pytest_cache
.ruff_cache
.uv-cache
.cache
.turbo
.gradle
dist
build
out
target
vendor
site-packages
EXCEOF

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

SSH_OPTS=(-i /root/.ssh/id_bridge -p "$PORT"
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
  -o IdentityFile=/root/.ssh/id_bridge,reconnect,ServerAliveInterval=15,ServerAliveCountMax=3 \
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

cat > /usr/local/bin/bridge-register <<'TOOL_bridge_register_EOF'
#!/bin/bash
# bridge-register --name <名> --os <windows|macos|linux> --user <用户名>
#                 [--tool-dir <路径>] [--status-local <路径>] [--label <说明>] [--port <端口>]
#
# 由客户端在建立隧道前自动调用：客户端把自己的身份上报，服务器分配端口并建档。
# 输出形如  PORT=2224  供客户端解析。已存在则返回原端口（幂等）。
NAME=""; OS=""; USER_=""; TOOL=""; SLOCAL=""; LABEL=""; PORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --os) OS="$2"; shift 2 ;;
    --user) USER_="$2"; shift 2 ;;
    --tool-dir) TOOL="$2"; shift 2 ;;
    --status-local) SLOCAL="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    *) echo "ERR|未知参数: $1"; exit 1 ;;
  esac
done
[ -z "$NAME" ] || [ -z "$OS" ] || [ -z "$USER_" ] && {
  echo "ERR|--name --os --user 必填"; exit 1; }
# 名字消毒，避免路径穿越
NAME=$(printf '%s' "$NAME" | tr -cd '[:alnum:]._-' | cut -c1-40)
[ -z "$NAME" ] && { echo "ERR|名字非法"; exit 1; }

CONF="/root/.winbridge/clients/${NAME}.conf"
mkdir -p /root/.winbridge/clients

if [ -f "$CONF" ] && [ -z "$PORT" ]; then
  PORT=$(sed -n 's/^PORT=//p' "$CONF" | sed 's/#.*//; s/[[:space:]]//g' | head -1)
fi
if [ -z "$PORT" ]; then
  USED=$(grep -h '^PORT=' /root/.winbridge/clients/*.conf 2>/dev/null \
         | cut -d= -f2 | sed 's/#.*//; s/[[:space:]]//g' | grep -E '^[0-9]+$')
  PORT=2222
  while echo "$USED" | grep -qx "$PORT" || ss -tln 2>/dev/null | grep -q ":$PORT "; do
    PORT=$((PORT + 1))
  done
fi

[ -z "$TOOL" ] && case "$OS" in
  windows) TOOL='C:\bridge-console' ;;
  *)       TOOL="/home/$USER_/bridge-console" ;;
esac
[ -z "$SLOCAL" ] && case "$OS" in
  windows) SLOCAL="${TOOL}\\status\\${NAME}" ;;
  *)       SLOCAL="${TOOL}/status/${NAME}" ;;
esac
[ -z "$LABEL" ] && LABEL="$NAME"

cat > "$CONF" <<CFGEOF
NAME=$NAME
OS=$OS
USER=$USER_
PORT=$PORT
LABEL='$LABEL'
TOOL_DIR='$TOOL'
STATUS_LOCAL='$SLOCAL'
CFGEOF
mkdir -p "/root/.winbridge/status/$NAME" "/root/mnt/$NAME"
[ -f /root/.winbridge/current ] || echo "$NAME" > /root/.winbridge/current

echo "PORT=$PORT"
echo "NAME=$NAME"
echo "OK|已登记 $NAME ($OS/$USER_) 端口 $PORT"
TOOL_bridge_register_EOF

cat > /usr/local/bin/bridge-add-client <<'TOOL_bridge_add_client_EOF'
#!/bin/bash
# bridge-add-client <名字> <windows|macos|linux> <该机器上的用户名> [端口]
# 不给端口则自动挑一个没被占用的
NAME="$1"; OS="$2"; USER="$3"; PORT="$4"
[ -z "$NAME" ] || [ -z "$OS" ] || [ -z "$USER" ] && {
  echo "用法: bridge-add-client <名字> <windows|macos|linux> <用户名> [端口]"; exit 1; }
CONF="/root/.winbridge/clients/${NAME}.conf"
[ -f "$CONF" ] && { echo "ERR|客户端 $NAME 已存在: $CONF"; exit 1; }

if [ -z "$PORT" ]; then
  # 逐行剥注释与空白（不能用 tr -d '[:space:]'，那会把换行也删掉，端口会粘连）
  USED=$(grep -h '^PORT=' /root/.winbridge/clients/*.conf 2>/dev/null \
         | cut -d= -f2 | sed 's/#.*//; s/[[:space:]]//g' | grep -E '^[0-9]+$')
  PORT=2222
  while echo "$USED" | grep -qx "$PORT" || ss -tln 2>/dev/null | grep -q ":$PORT "; do
    PORT=$((PORT + 1))
  done
fi

case "$OS" in
  windows) TOOL='D:\bridge-console'; SLOCAL="D:\\bridge-console\\status\\${NAME}" ;;
  *)       TOOL="/Users/$USER/bridge-console"; SLOCAL="/Users/$USER/bridge-console/status/${NAME}" ;;
esac

cat > "$CONF" <<CFGEOF
NAME=$NAME
OS=$OS
USER=$USER
PORT=$PORT
LABEL='$NAME'
TOOL_DIR='$TOOL'
STATUS_LOCAL='$SLOCAL'
CFGEOF
mkdir -p "/root/.winbridge/status/$NAME" "/root/mnt/$NAME"

PUB=$(cat /root/.ssh/id_bridge.pub 2>/dev/null)
SRV=$(curl -s -m 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
cat <<TIPEOF

✅ 已创建客户端档案: $CONF
   名字=$NAME  系统=$OS  用户=$USER  隧道端口=$PORT（自动避开已用端口）

在【那台 $OS 机器】上执行：
TIPEOF
if [ "$OS" = "windows" ]; then
cat <<TIPEOF
  1) 复制 bridge-console 目录过去（含 bridge_gui.py / setup-windows.ps1）
  2) 管理员 PowerShell:
     powershell -ExecutionPolicy Bypass -File setup-windows.ps1 \\
       -PubKey "$PUB" \\
       -ServerHost $SRV -Alias $NAME -Port $PORT -LoopbackOnly -AutoStart
  3) 双击 桌面客户端.bat → 「添加服务器…」→ SSH 别名填 $NAME
TIPEOF
else
cat <<TIPEOF
  1) 复制 bridge-console 目录过去（含 bridge_gui.py / setup-mac.sh）
  2) 终端:
     bash setup-mac.sh --pubkey "$PUB" \\
       --host $SRV --alias $NAME --port $PORT --autostart
  3) python3 bridge_gui.py → 「添加服务器…」→ SSH 别名填 $NAME
TIPEOF
fi
cat <<TIPEOF

回到服务器验证：  bridge-check -c $NAME
TIPEOF
TOOL_bridge_add_client_EOF

cat > /usr/local/bin/bridge-index <<'TOOL_bridge_index_EOF'
#!/bin/bash
# bridge-index [-c 客户端] [<挂载点>]  — 在客户端本机生成文件索引，存到服务器本地磁盘
#
# 策略：git 仓库优先用 `git ls-files`（秒级，天然遵守 .gitignore）；
#       非 git 目录才走目录遍历 + /root/.winbridge/index-exclude.txt 的排除规则。
. /root/.winbridge/lib.sh
bridge_resolve_client "$@" || exit 1
[ $BRIDGE_SHIFT -gt 0 ] && shift $BRIDGE_SHIFT
IDX_DIR=/root/.winbridge/index
mkdir -p "$IDX_DIR"

TARGETS="$1"
[ -z "$TARGETS" ] && TARGETS=$(awk -v p="$MNT_ROOT" '$3=="fuse.sshfs" && index($2,p)==1 {gsub(/\\040/," ",$2); print $2}' /proc/mounts)
[ -z "$TARGETS" ] && { echo "ERR|该客户端没有挂载"; exit 1; }

EXC_FILE=/root/.winbridge/index-exclude.txt
EXCLUDES=$(grep -v '^[[:space:]]*#' "$EXC_FILE" 2>/dev/null | grep -v '^[[:space:]]*$')

for MP in $TARGETS; do
  SRC=$(awk -v m="$MP" '$3=="fuse.sshfs" && $2==m {print $1}' /proc/mounts | head -1)
  [ -z "$SRC" ] && { echo "跳过（非挂载点）: $MP"; continue; }
  LOCAL=$(printf '%s' "$SRC" | sed 's/^[^:]*://')
  case "$OS" in windows) LOCAL=$(printf '%s' "$LOCAL" | sed 's|^/||' | tr '/' '\\') ;; esac

  OUT="$IDX_DIR/${CLIENT}__$(basename "$MP").txt"
  S=$(date +%s%N)
  MODE=""

  # --- 优先 git ---
  if [ "$OS" = "windows" ]; then
    GIT=$(bridge-run -c "$CLIENT" -d "$LOCAL" 'if (Test-Path ".git") { git ls-files 2>$null }' 2>/dev/null)
  else
    GIT=$(bridge-run -c "$CLIENT" -d "$LOCAL" '[ -d .git ] && git ls-files 2>/dev/null' 2>/dev/null)
  fi
  if [ -n "$GIT" ]; then
    LIST="$GIT"; MODE="git ls-files"
  else
    # --- 回退：目录遍历 + 排除 ---
    if [ "$OS" = "windows" ]; then
      PAT=$(printf '%s' "$EXCLUDES" | sed 's|/|\\|g' | tr '\n' '|' | sed 's/|$//')
      LIST=$(bridge-run -c "$CLIENT" -d "$LOCAL" "\$e='$PAT'.Split('|'); \$r=(Get-Location).Path
Get-ChildItem -Recurse -File -Force -EA SilentlyContinue | ForEach-Object {
  \$p=\$_.FullName.Substring(\$r.Length); \$skip=\$false
  foreach (\$x in \$e) { if (\$x -and \$p.Contains('\\'+\$x+'\\')) { \$skip=\$true; break } }
  if (-not \$skip) { \$p.TrimStart('\\') } }" 2>/dev/null)
    else
      A=""; while IFS= read -r e; do [ -n "$e" ] && A="$A -not -path \"*/$e/*\""; done <<< "$EXCLUDES"
      LIST=$(bridge-run -c "$CLIENT" -d "$LOCAL" "find . -type f $A -printf '%P\n' 2>/dev/null" 2>/dev/null)
    fi
    MODE="目录遍历"
  fi

  [ -z "$LIST" ] && { echo "  ❌ $MP 索引失败"; continue; }
  {
    echo "# mount=$MP"
    echo "# local=$LOCAL"
    echo "# client=$CLIENT"
    echo "# mode=$MODE"
    echo "# built=$(date '+%Y-%m-%d %H:%M:%S')"
    printf '%s\n' "$LIST" | sed 's/\r$//' | grep -v '^[[:space:]]*$'
  } > "$OUT"
  N=$(grep -vc '^#' "$OUT")
  echo "  ✅ $(basename "$MP"): $N 个文件（$MODE），$(( ($(date +%s%N)-S)/1000000 ))ms"
  [ "$N" -gt 80000 ] && echo "     ⚠️ 数量偏大，考虑往 $EXC_FILE 加排除规则"
done
TOOL_bridge_index_EOF

cat > /usr/local/bin/bridge-find <<'TOOL_bridge_find_EOF'
#!/bin/bash
# bridge-find <关键词>  — 在预建索引里秒查文件路径（不碰挂载）
PAT="$*"
[ -z "$PAT" ] && { echo "用法: bridge-find <文件名关键词>"; exit 1; }
IDX_DIR=/root/.winbridge/index
[ -d "$IDX_DIR" ] || { echo "尚无索引，先跑: bridge-index"; exit 1; }
FOUND=0
for f in "$IDX_DIR"/*.txt; do
  [ -e "$f" ] || continue
  MP=$(sed -n 's/^# mount=//p' "$f" | head -1)
  BUILT=$(sed -n 's/^# built=//p' "$f" | head -1)
  HITS=$(grep -iv '^#' "$f" | grep -i -- "$PAT")
  [ -z "$HITS" ] && continue
  FOUND=1
  echo "── $MP  (索引于 $BUILT)"
  printf '%s\n' "$HITS" | head -40 | while IFS='|' read -r p sz; do
    if [ -n "$sz" ]; then printf '   %-70s %10s B\n' "$p" "$sz"
    else printf '   %s\n' "$p"; fi
  done
  T=$(printf '%s\n' "$HITS" | wc -l)
  [ "$T" -gt 40 ] && echo "   … 共 $T 条，已截断"
done
[ "$FOUND" = 0 ] && echo "无匹配。索引可能过期，跑 bridge-index 刷新。"
TOOL_bridge_find_EOF

cat > /usr/local/bin/bridge-guard <<'TOOL_bridge_guard_EOF'
#!/bin/bash
# bridge-guard start|stop|status|once
# 守护挂载：自动终止在 /root/mnt/** 上做全树遍历的进程（Cursor 索引、误用的 find/rg 等）
PIDF=/root/.winbridge/guard.pid
LOG=/root/.winbridge/guard.log

scan_once() {
  local killed=0
  for p in $(pgrep -f 'ripgrep|/rg |[^a-z]rg$|find |ls -R' 2>/dev/null); do
    [ "$p" = "$$" ] && continue
    local cwd cmd
    cwd=$(readlink /proc/$p/cwd 2>/dev/null) || continue
    cmd=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null)
    # 只管作用在挂载上的；bridge-* 自己的命令放行
    case "$cmd" in *bridge-*) continue ;; esac
    case "$cwd" in
      /root/mnt/*|/root/local-project*) ;;
      *) case "$cmd" in *"/root/mnt/"*|*"/root/local-project"*) ;; *) continue ;; esac ;;
    esac
    # 跑满 20 秒才动手，避免误杀正常的短命令
    local et; et=$(ps -o etimes= -p "$p" 2>/dev/null | tr -d ' ')
    [ -z "$et" ] && continue
    [ "$et" -lt 20 ] && continue
    echo "$(date '+%F %T') KILL pid=$p et=${et}s cwd=$cwd cmd=$(echo "$cmd" | cut -c1-90)" >> "$LOG"
    kill -9 "$p" 2>/dev/null && killed=$((killed+1))
  done
  echo "$killed"
}

case "$1" in
  once)   n=$(scan_once); echo "本次终止 $n 个" ;;
  start)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      echo "已在运行 (pid $(cat "$PIDF"))"; exit 0; fi
    setsid bash -c "$(declare -f scan_once); while true; do scan_once >/dev/null; sleep 10; done" \
      >/dev/null 2>&1 < /dev/null &
    echo $! > "$PIDF"; echo "守护已启动 (pid $!)，日志 $LOG" ;;
  stop)   [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null && rm -f "$PIDF" && echo "已停止" || echo "未运行" ;;
  status)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then echo "运行中 (pid $(cat "$PIDF"))"
    else echo "未运行"; fi
    [ -f "$LOG" ] && { echo "--- 最近拦截 ---"; tail -5 "$LOG"; } ;;
  *) echo "用法: bridge-guard start|stop|status|once"; exit 1 ;;
esac
TOOL_bridge_guard_EOF

cat > /usr/local/bin/bridge-sync-md <<'TOOL_bridge_sync_md_EOF'
#!/bin/bash
# 把当前挂载状态同步进 /root/CLAUDE.md 的标记区块，让新会话总能看到最新映射
MD="${1:-/root/CLAUDE.md}"
BEGIN='<!-- BRIDGE-MOUNTS:BEGIN 由 bridge-sync-md 自动生成，勿手改 -->'
END='<!-- BRIDGE-MOUNTS:END -->'

BODY=$(
  echo "$BEGIN"
  echo ""
  echo "> 本节由 \`bridge-sync-md\` 生成，更新于 $(date '+%Y-%m-%d %H:%M')。"
  echo "> **实时状态请跑 \`bridge-mounts\`；本表可能已过期。**"
  echo ""
  ACTIVE=$(sed -n 's/^ACTIVE_PROJECT=//p' /root/.winbridge/config 2>/dev/null | sed 's/#.*//' | tr -d "'\" ")
  if [ -n "$ACTIVE" ]; then
    echo "**当前主项目**：\`$ACTIVE\`"
    echo ""
  fi
  echo "| 服务器路径 | 本机路径 | 机器 |"
  echo "|---|---|---|"
  awk '$3=="fuse.sshfs"{gsub(/\\040/," ",$2); print $2"\t"$1}' /proc/mounts | while IFS=$'\t' read -r mp src; do
    case "$mp" in
      /root/.winbridge/status/*) continue ;;   # 内部管道不展示
    esac
    client=$(printf '%s' "$mp" | sed -n 's|^/root/mnt/\([^/]*\)/.*|\1|p')
    [ -z "$client" ] && client="-"
    local_path=$(printf '%s' "$src" | sed 's/^[^:]*://; s|^/\([A-Za-z]\):|\1:|')
    case "$local_path" in
      [A-Za-z]:*) local_path=$(printf '%s' "$local_path" | tr '/' '\\') ;;
    esac
    echo "| \`$mp\` | \`$local_path\` | $client |"
  done
  echo ""
  echo "$END"
)

if grep -q "BRIDGE-MOUNTS:BEGIN" "$MD" 2>/dev/null; then
  python3 - "$MD" "$BODY" <<'PY'
import re, sys
md, body = sys.argv[1], sys.argv[2]
s = open(md, encoding="utf-8").read()
pat = re.compile(r"<!-- BRIDGE-MOUNTS:BEGIN.*?<!-- BRIDGE-MOUNTS:END -->", re.S)
# 用函数做替换：字符串形式会把 body 里的 \c \b 当成正则转义（Windows 路径必踩）
s = pat.sub(lambda _m: body, s)
open(md, "w", encoding="utf-8").write(s)
PY
  echo "已更新 $MD 的挂载区块"
else
  printf '\n%s\n' "$BODY" >> "$MD"
  echo "已在 $MD 末尾新增挂载区块"
fi
TOOL_bridge_sync_md_EOF

chmod +x /usr/local/bin/bridge-run /usr/local/bin/bridge-mount /usr/local/bin/bridge-umount /usr/local/bin/bridge-mounts /usr/local/bin/bridge-check /usr/local/bin/bridge-grep /usr/local/bin/bridge-git /usr/local/bin/bridge-reset /usr/local/bin/bridge-statusd /usr/local/bin/bridge-daemon /usr/local/bin/bridge-register /usr/local/bin/bridge-add-client /usr/local/bin/bridge-index /usr/local/bin/bridge-find /usr/local/bin/bridge-guard /usr/local/bin/bridge-sync-md 
for p in win-run:bridge-run win-check:bridge-check win-mounts:bridge-mounts win-grep:bridge-grep win-git:bridge-git win-reset:bridge-reset win-daemon:bridge-daemon win-mount:bridge-mount win-umount:bridge-umount; do
  ln -sfn "/usr/local/bin/${p##*:}" "/usr/local/bin/${p%%:*}"; done
echo "    bridge-run bridge-mount bridge-umount bridge-mounts bridge-check bridge-grep bridge-git bridge-statusd bridge-daemon（含 win-* 兼容软链）"
echo "==> 5/5 完成"
SRV=$(curl -s -m 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
HOSTN=$(hostname)

cat <<TIPEOF

════════════════════════════════════════════════════════════════════
 服务器端装好了。接下来只需在【你的本地电脑】上操作。
════════════════════════════════════════════════════════════════════

本服务器的公钥（下一步要用）：

$(cat /root/.ssh/id_bridge.pub)


【第 1 步】在【你的本地电脑】上取得客户端文件（不是在这台服务器上）：

  git clone <本项目仓库地址> ~/bridge-console
  cd ~/bridge-console

  # 或者把 setup-windows.ps1 / setup-mac.sh / bridge_gui.py 三个文件拷过去也行

【第 2 步】配置本机（下面命令都是一整行）

  Windows —— 管理员 PowerShell：

  powershell -ExecutionPolicy Bypass -File setup-windows.ps1 -PubKey "上面那行公钥" -ServerHost $SRV -Alias $HOSTN -Identity ~/.ssh/你连本服务器用的私钥 -LoopbackOnly -AutoStart

  macOS —— 先开「系统设置 → 通用 → 共享 → 远程登录」，然后终端执行：

  bash setup-mac.sh --pubkey "上面那行公钥" --host $SRV --alias $HOSTN --identity ~/.ssh/你连本服务器用的私钥 --autostart

  注意 --identity / -Identity 要填【私钥】路径（不带 .pub）；
  若你本来就能直接 ssh 上这台服务器，这个参数可以整个省略。

【第 3 步】打开桌面客户端 → 「添加服务器…」→ SSH 别名填 $HOSTN → 「启动隧道」

  客户端会自动把本机的用户名、系统、工具路径上报给服务器，
  并领取一个不冲突的隧道端口。服务器这边无需任何手工配置。

【验证】回到服务器执行：

  bridge-check            # 列出所有已接入的机器
  bridge-mounts           # 看挂载
  bridge-find <关键词>    # 查文件（先在客户端界面挂目录，再 bridge-index）

════════════════════════════════════════════════════════════════════
TIPEOF
