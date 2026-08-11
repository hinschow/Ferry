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
