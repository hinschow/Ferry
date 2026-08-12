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

# ---------------------------------------------------------------- 挂载登记表
#
# 挂载点允许放在 MNT_ROOT 之外（用户自选位置），但 bridge-mounts / bridge-statusd
# 原来只按 MNT_ROOT 前缀匹配 /proc/mounts —— 自选位置会被漏掉，客户端就一直显示
# 「未挂载」。所以每次挂载都在这里登记一行，两边取并集。
#   每行： <挂载点>\t<本地路径>\t<own|keep>     own = 目录是我们建的，卸载后删掉
BRIDGE_REG_DIR=/root/.winbridge/mounts

bridge_reg_file() { printf '%s/%s.tsv' "$BRIDGE_REG_DIR" "${1:-$CLIENT}"; }

bridge_reg_add() {   # <挂载点> <本地路径> <own|keep>
  local f; f=$(bridge_reg_file)
  mkdir -p "$BRIDGE_REG_DIR"
  bridge_reg_del "$1"
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
