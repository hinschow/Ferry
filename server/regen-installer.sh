#!/bin/bash
# 重新打包服务器端一键安装脚本，并同步进仓库。
#
#   bash regen-installer.sh
#
# 以 /usr/local/bin/bridge-* 的现状为准（那才是跑着的版本），
# 打包成 bridge-install.sh，再把工具源码和安装脚本一并同步进 repo/。
# 早先 dist/ 与 repo/ 各存一份，改完老忘了同步 —— 现在只有 repo/ 一处。
set -e
HERE=/root/.winbridge
OUT="${1:-$HERE/bridge-install.sh}"
REPO="$HERE/repo"
TOOLS="bridge-run bridge-mount bridge-umount bridge-mounts bridge-check bridge-grep bridge-git bridge-reset bridge-statusd bridge-daemon bridge-register bridge-add-client bridge-index bridge-find bridge-guard bridge-sync-md bridge-invite bridge-ls"
{
sed -n '1,/^echo "==> 4\/5 安装命令行工具"$/p' "$HERE/bridge-install.sh.tpl"
echo ""
echo "cat > /root/.winbridge/lib.sh <<'LIB_EOF'"
cat "$HERE/lib.sh"
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
echo 'echo "    bridge-* 全套已安装（含 win-* 兼容软链）"'
sed -n '/^echo "==> 5\/5 完成"/,$p' "$HERE/bridge-install.sh.tpl"
} > "$OUT"
chmod +x "$OUT"
bash -n "$OUT"

# ---- 同步进仓库，省掉手工拷贝这一步
if [ -d "$REPO/server" ]; then
  cp "$OUT" "$REPO/bridge-install.sh"
  cp "$HERE/lib.sh" "$HERE/index-exclude.txt" "$REPO/server/"
  cp "$HERE/regen-installer.sh" "$REPO/server/"
  for f in $TOOLS; do cp "/usr/local/bin/$f" "$REPO/server/"; done
  echo "✅ $OUT ($(stat -c%s "$OUT") 字节)，已同步进 repo/"
else
  echo "✅ $OUT ($(stat -c%s "$OUT") 字节)"
fi
