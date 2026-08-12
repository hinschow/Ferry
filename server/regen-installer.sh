#!/bin/bash
OUT="${1:-/root/.winbridge/bridge-install.sh}"
TOOLS="bridge-run bridge-mount bridge-umount bridge-mounts bridge-check bridge-grep bridge-git bridge-reset bridge-statusd bridge-daemon bridge-register bridge-add-client bridge-index bridge-find bridge-guard bridge-sync-md bridge-invite bridge-ls"
{
sed -n '1,/^echo "==> 4\/5 安装命令行工具"$/p' /root/.winbridge/bridge-install.sh.tpl
echo ""
echo "cat > /root/.winbridge/lib.sh <<'LIB_EOF'"
cat /root/.winbridge/lib.sh
echo "LIB_EOF"
for f in $TOOLS; do
  echo ""
  echo "cat > /usr/local/bin/$f <<'TOOL_${f//-/_}_EOF'"
  cat "/usr/local/bin/$f"
  echo "TOOL_${f//-/_}_EOF"
done
echo ""
echo "chmod +x $(for f in $TOOLS; do printf '/usr/local/bin/%s ' "$f"; done)"
echo 'for p in win-run:bridge-run win-check:bridge-check win-mounts:bridge-mounts win-grep:bridge-grep win-git:bridge-git win-reset:bridge-reset win-daemon:bridge-daemon win-mount:bridge-mount win-umount:bridge-umount; do'
echo '  ln -sfn "/usr/local/bin/${p##*:}" "/usr/local/bin/${p%%:*}"; done'
echo 'echo "    bridge-run bridge-mount bridge-umount bridge-mounts bridge-check bridge-grep bridge-git bridge-statusd bridge-daemon（含 win-* 兼容软链）"'
sed -n '/^echo "==> 5\/5 完成"/,$p' /root/.winbridge/bridge-install.sh.tpl
} > "$OUT"
chmod +x "$OUT"
bash -n "$OUT" && echo "✅ $OUT ($(stat -c%s "$OUT") 字节)"
