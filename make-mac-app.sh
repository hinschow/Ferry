#!/bin/bash
# 把控制台打包成一个真正的 macOS 应用 Ferry.app
#
#   bash make-mac-app.sh [输出目录]      默认就放在本目录
#
# 不需要 Xcode，不需要 PyInstaller —— .app 本质上就是一个按规矩摆好的文件夹。
# 好处是它仍然直接跑 bridge_gui.py，所以控制台的「重载」自更新照常能用。
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$SRC}"
APP="$OUT/Ferry.app"

[ -f "$SRC/bridge_gui.py" ] || { echo "✗ 同目录下找不到 bridge_gui.py"; exit 1; }

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ---- Info.plist：应用名、图标、版本、Retina
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>Ferry</string>
  <key>CFBundleDisplayName</key>       <string>桥接控制台</string>
  <key>CFBundleIdentifier</key>        <string>online.ferry.console</string>
  <key>CFBundleVersion</key>           <string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleExecutable</key>        <string>Ferry</string>
  <key>CFBundleIconFile</key>          <string>ferry</string>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>LSMinimumSystemVersion</key>    <string>10.13</string>
</dict>
</plist>
PLIST

# ---- 图标
if [ -f "$SRC/assets/ferry.icns" ]; then
  cp "$SRC/assets/ferry.icns" "$APP/Contents/Resources/ferry.icns"
else
  echo "  （没找到 assets/ferry.icns，先跑 python3 tools/make-icons.py 才有图标）"
fi

# ---- 启动器：.app 放在源码目录里，往上三层就是 bridge_gui.py 所在处
cat > "$APP/Contents/MacOS/Ferry" <<'LAUNCH'
#!/bin/bash
HERE="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$HERE" || exit 1

# Apple 自带 / Xcode 的 python3 链接的是有崩溃缺陷的 Tk 8.5.9，先找 Tk 8.6+
pick_python() {
  for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    command -v "$p" >/dev/null 2>&1 || continue
    v=$("$p" -c "import tkinter;print(tkinter.TkVersion)" 2>/dev/null) || continue
    case "$v" in 8.6*|8.7*|9.*) echo "$p"; return 0 ;; esac
  done
  return 1
}

PY=$(pick_python)
if [ -z "$PY" ]; then
  # 双击启动看不到终端输出，只能弹窗告诉用户
  osascript -e 'display alert "缺少可用的 Python" message "系统自带的 Tk 8.5.9 在 macOS 上会崩溃。\n\n请在终端执行：\n    brew install python-tk\n\n然后重新打开 Ferry。" as critical' >/dev/null 2>&1
  exit 1
fi
exec "$PY" "$HERE/bridge_gui.py" "$@"
LAUNCH
chmod +x "$APP/Contents/MacOS/Ferry"

# 从网上下载的包会带隔离标记，Finder 会拦；就地生成的一般没有，保险起见清一次
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
touch "$APP"                       # 让 Finder 立刻刷新图标缓存

echo "✅ 已生成 $APP"
echo "   双击即可启动；也可以拖进「程序」文件夹或 Dock。"
echo "   注意：它靠相对位置找 bridge_gui.py —— 移动时请把整个文件夹一起搬。"
