# ================================================================ 路径解析
#
# 所有路径都从 $HOME 推导，而不是写死 /root —— 这一条决定了「角色隔离」能不能成立。
#
#   以 root 跑        $HOME=/root        →  /root/.winbridge、/root/mnt   （和以前完全一样）
#   以 ferry-mac 跑   $HOME=/home/ferry-mac →  各自的家目录，互相看不到
#
# FUSE 在没开 allow_other 时只允许挂载者本人访问，连 root 直接读都会被拒
# （root 可以 su 过去，所以管理员仍然看得到 —— 这是设计意图）。
# 于是「每台电脑一个 Unix 账户」就等于「每台电脑一个隔离的角色」，不需要额外机制。
FERRY_USER="$(id -un)"
# HOME 偶尔会是空的（env -i、某些 cron/sudo 场景），从 passwd 兜底
[ -n "$HOME" ] || HOME="$(getent passwd "$FERRY_USER" | cut -d: -f6)"
FERRY_HOME="${FERRY_HOME:-$HOME/.winbridge}"
FERRY_MNT="${FERRY_MNT:-$HOME/mnt}"
FERRY_KEY="${FERRY_KEY:-$HOME/.ssh/id_bridge}"

# 端口在整台机器上是全局资源，不能各家管各家 —— 两个角色都挑 2222 就撞了。
# 这个目录归 ferry 组、setgid 2775，所有角色都能读写彼此的登记。
FERRY_PORTS="${FERRY_PORTS:-/var/lib/ferry/ports}"

# 客户端解析：-c <名字> > $BRIDGE_CLIENT > current 文件
bridge_resolve_client() {
  BRIDGE_C=""
  if [ "$1" = "-c" ]; then BRIDGE_C="$2"; BRIDGE_SHIFT=2; else BRIDGE_SHIFT=0; fi
  [ -z "$BRIDGE_C" ] && BRIDGE_C="${BRIDGE_CLIENT:-}"
  [ -z "$BRIDGE_C" ] && BRIDGE_C=$(cat "$FERRY_HOME/current" 2>/dev/null)
  [ -z "$BRIDGE_C" ] && { echo "ERR|未指定客户端，用 -c <名字> 或设置 $FERRY_HOME/current"; return 1; }
  CONF="$FERRY_HOME/clients/${BRIDGE_C}.conf"
  [ -f "$CONF" ] || { echo "ERR|找不到客户端档案: $CONF"; return 1; }
  # shellcheck disable=SC1090
  . "$CONF"
  CLIENT="$NAME"
  MNT_ROOT="$FERRY_MNT/$CLIENT"             # 每个客户端独立挂载根，避免撞名
  STATUS_DIR="$FERRY_HOME/status/$CLIENT"
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

# ---------------------------------------------------------------- 挂载登记表
#
# 挂载点允许放在 MNT_ROOT 之外（用户自选位置），但 bridge-mounts / bridge-statusd
# 原来只按 MNT_ROOT 前缀匹配 /proc/mounts —— 自选位置会被漏掉，客户端就一直显示
# 「未挂载」。所以每次挂载都在这里登记一行，两边取并集。
#   每行： <挂载点>\t<本地路径>\t<own|keep>     own = 目录是我们建的，卸载后删掉
BRIDGE_REG_DIR="$FERRY_HOME/mounts"

bridge_reg_file() { printf '%s/%s.tsv' "$BRIDGE_REG_DIR" "${1:-$CLIENT}"; }

bridge_reg_add() {   # <挂载点> <本地路径> <own|keep>
  local f; f=$(bridge_reg_file)
  mkdir -p "$BRIDGE_REG_DIR"
  bridge_reg_del "$1" >/dev/null
  printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$f"
}

bridge_reg_del() {   # <挂载点>  → 回显该行的第三列（own/keep），没登记则空
  local f; f=$(bridge_reg_file)
  [ -f "$f" ] || return 0
  awk -F'\t' -v mp="$1" '$1==mp{print $3}' "$f"
  local tmp="$f.$$"
  awk -F'\t' -v mp="$1" '$1!=mp' "$f" > "$tmp" && mv "$tmp" "$f"
}

bridge_reg_points() {  # 列出登记过的挂载点（每行一个）
  local f; f=$(bridge_reg_file "${1:-$CLIENT}")
  [ -f "$f" ] && cut -f1 "$f" || true
}

# ---------------------------------------------------------------- 端口登记
# 只记「哪个角色的哪台客户端占了哪个端口」，供分配时避让。
# 目录不存在（还没升级）时全部降级为空，行为回落到只看 ss -tln。

bridge_port_claim() {   # <客户端名> <端口>
  [ -d "$FERRY_PORTS" ] || return 0
  printf '%s\t%s\t%s\n' "$FERRY_USER" "$1" "$2" > "$FERRY_PORTS/$FERRY_USER.$1" 2>/dev/null || true
}

bridge_port_release() { # <客户端名>
  [ -d "$FERRY_PORTS" ] && rm -f "$FERRY_PORTS/$FERRY_USER.$1" 2>/dev/null
  return 0
}

bridge_ports_taken() {  # 列出别人占着的端口（排除自己这台客户端）
  [ -d "$FERRY_PORTS" ] || return 0
  local me="$FERRY_PORTS/$FERRY_USER.${1:-}"
  for f in "$FERRY_PORTS"/*; do
    [ -f "$f" ] || continue
    [ "$f" = "$me" ] && continue
    cut -f3 "$f" 2>/dev/null
  done
}

# ---------------------------------------------------------------- Claude 知识块
#
# 把 Ferry 的使用纪律（挂载别遍历、会话恢复超时怎么救）写进指定用户的
# ~/.claude/CLAUDE.md —— Claude 会话在任何工作目录都会加载这份用户级记忆。
# 不装的话，知识只存在于某台服务器的某个文件里，换台机器就归零。
# 标记块幂等：重装只更新块内内容，用户自己写的部分原样保留。
bridge_install_claude_block() {   # <家目录> [属主]
  local home="$1" owner="$2"
  local src=/usr/local/lib/ferry/claude-md-block.md
  [ -f "$src" ] || return 0
  local f="$home/.claude/CLAUDE.md"
  mkdir -p "$home/.claude" || return 0
  touch "$f" 2>/dev/null || return 0
  awk '/<!-- FERRY:BEGIN/{s=1} /<!-- FERRY:END/{s=0;next} !s' "$f" > "$f.tmp"
  {
    cat "$f.tmp"
    [ -s "$f.tmp" ] && echo ""
    echo "<!-- FERRY:BEGIN 由 bridge-install.sh 维护，勿手改（重装会刷新此块） -->"
    cat "$src"
    echo "<!-- FERRY:END -->"
  } > "$f"
  rm -f "$f.tmp"
  [ -n "$owner" ] && chown -R "$owner" "$home/.claude" 2>/dev/null
  return 0
}
