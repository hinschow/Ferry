#!/bin/bash
cd "$(dirname "$0")" || exit 1

# Apple 自带 / Xcode 的 python3 链接的是有崩溃缺陷的 Tk 8.5.9，优先找带 Tk 8.6+ 的解释器
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
  echo "没有找到带 Tk 8.6+ 的 Python。请执行： brew install python-tk"
  echo "（系统/Xcode 自带的 Tk 8.5.9 在 macOS 上会崩溃）"
  read -r -p "按回车退出…" _
  exit 1
fi
exec "$PY" bridge_gui.py "$@"
