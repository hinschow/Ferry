#!/bin/bash
# ============================================================================
#  Bridge Console - macOS side setup
#
#  Usage:
#     bash setup-mac.sh --pubkey "ssh-ed25519 AAAA... comment" \
#                       --host 1.2.3.4 --alias myserver \
#                       --identity ~/.ssh/id_rsa --port 2223 [--autostart]
#
#  Everything is append-only and backed up.
# ============================================================================
set -e
PUBKEY=""; SRVHOST=""; ALIAS=""; IDENTITY=""; PORT=2223; AUTOSTART=0
while [ $# -gt 0 ]; do
  case "$1" in
    --pubkey)   PUBKEY="$2"; shift 2 ;;
    --host)     SRVHOST="$2"; shift 2 ;;
    --alias)    ALIAS="$2"; shift 2 ;;
    --identity) IDENTITY="$2"; shift 2 ;;
    --port)     PORT="$2"; shift 2 ;;
    --autostart) AUTOSTART=1; shift ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 常见笔误：把公钥当私钥传进来
case "$IDENTITY" in
  *.pub)
    echo "[!] --identity 应该填【私钥】路径，你给的是公钥：$IDENTITY"
    echo "    去掉结尾的 .pub 再试，例如 ${IDENTITY%.pub}"
    exit 1 ;;
esac

echo "[1/8] Remote Login (sshd)"
if systemsetup -getremotelogin 2>/dev/null | grep -qi "On"; then
  echo "      already on"
else
  echo "      NOT enabled."
  echo "      Enable it: System Settings > General > Sharing > Remote Login"
  echo "      or run:    sudo systemsetup -setremotelogin on"
fi

echo "[2/8] Authorizing server public key"
if [ -n "$PUBKEY" ]; then
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
  TAG=$(printf '%s' "$PUBKEY" | awk '{print $NF}')
  if grep -qF "$TAG" ~/.ssh/authorized_keys 2>/dev/null; then
    echo "      key already present, skipped"
  else
    cp ~/.ssh/authorized_keys ~/.ssh/authorized_keys.bak 2>/dev/null || true
    printf '%s\n' "$PUBKEY" >> ~/.ssh/authorized_keys
    echo "      appended to ~/.ssh/authorized_keys"
  fi
else
  echo "      no --pubkey given, skipped"
fi

echo "[3/8] SSH host alias"
if [ -n "$SRVHOST" ]; then
  [ -z "$ALIAS" ] && ALIAS=$(printf '%s' "$SRVHOST" | tr -cd '[:alnum:]')
  CFG=~/.ssh/config
  mkdir -p ~/.ssh; touch "$CFG"; chmod 600 "$CFG"
  cp "$CFG" "$CFG.bak" 2>/dev/null || true
  if grep -qE "^[[:space:]]*Host[[:space:]]+$ALIAS[[:space:]]*$" "$CFG"; then
    echo "      alias '$ALIAS' already exists, skipped"
  else
    {
      echo ""
      echo "Host $ALIAS"
      echo "    HostName $SRVHOST"
      echo "    User root"
      [ -n "$IDENTITY" ] && echo "    IdentityFile $IDENTITY"
      # 127.0.0.1 not localhost - keep it explicit and IPv4
      echo "    RemoteForward $PORT 127.0.0.1:22"
      echo "    ServerAliveInterval 30"
      echo "    ServerAliveCountMax 3"
      echo "    StrictHostKeyChecking accept-new"
    } >> "$CFG"
    echo "      added alias '$ALIAS' -> $SRVHOST (RemoteForward $PORT)"
  fi
else
  echo "      no --host given, skipped"
fi

echo "[4/8] Python / Tk check"
# Apple's system Tk 8.5.9 is deprecated and crashes on modern macOS (esp. Apple Silicon).
# We need an interpreter whose tkinter links against Tk 8.6+.
PY=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  command -v "$cand" >/dev/null 2>&1 || continue
  tkv=$("$cand" -c "import tkinter;print(tkinter.TkVersion)" 2>/dev/null) || continue
  case "$tkv" in
    8.6*|8.7*|9.*) PY="$cand"; echo "      OK: $cand  (Tk $tkv)"; break ;;
    *) echo "      skip: $cand  (Tk $tkv - known to crash)" ;;
  esac
done
if [ -z "$PY" ]; then
  echo "      ! No usable Python found (need tkinter with Tk 8.6+)."
  echo "        Apple/Xcode python3 links against the broken system Tk 8.5.9."
  echo "        Fix:  brew install python-tk"
  echo "        Then: /opt/homebrew/bin/python3 bridge_gui.py"
fi

echo "[5/8] Launcher permissions & quarantine"
for f in start-mac.command setup-mac.sh; do
  if [ -f "$HERE/$f" ]; then
    chmod +x "$HERE/$f" 2>/dev/null && echo "      chmod +x $f"
  fi
done
# Remove Gatekeeper quarantine so double-click works without the "cannot be opened" prompt
if xattr -dr com.apple.quarantine "$HERE" 2>/dev/null; then
  echo "      cleared com.apple.quarantine"
fi

echo "[6/8] Full Disk Access check"
# macOS TCC blocks sshd sessions from reading ~/Desktop, ~/Documents, ~/Downloads
# unless sshd-keygen-wrapper is granted Full Disk Access.
PROBE=~/Documents
if [ -d "$PROBE" ]; then
  if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
       "$USER@127.0.0.1" "ls '$PROBE' >/dev/null 2>&1" 2>/dev/null; then
    echo "      protected folders readable over SSH - OK"
  else
    echo "      ! Could not verify. If mounting ~/Documents / ~/Desktop / ~/Downloads"
    echo "        fails with 'Operation not permitted', grant Full Disk Access to:"
    echo "        /usr/libexec/sshd-keygen-wrapper"
    echo "        System Settings > Privacy & Security > Full Disk Access > + (Cmd+Shift+G)"
  fi
else
  echo "      skipped"
fi

echo "[7/8] Auto-start LaunchAgent"
if [ "$AUTOSTART" = "1" ]; then
  PLIST=~/Library/LaunchAgents/com.bridge.console.plist
  mkdir -p ~/Library/LaunchAgents
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.bridge.console</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY:-/opt/homebrew/bin/python3}</string>
    <string>$HERE/bridge_gui.py</string>
    <string>--auto-tunnel</string>
    <string>--minimized</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict>
</plist>
PLISTEOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST" 2>/dev/null || true
  echo "      created $PLIST"
else
  echo "      skipped (pass --autostart to enable)"
fi

# ---- build the Ferry.app bundle so the console launches like a real app
if [ -f "$HERE/make-mac-app.sh" ]; then
  echo "[8/8] building Ferry.app"
  bash "$HERE/make-mac-app.sh" "$HERE" >/dev/null 2>&1 \
    && echo "      $HERE/Ferry.app  (double-click, or drag to the Dock)" \
    || echo "      skipped (make-mac-app.sh failed)"
fi

echo ""
echo "Done."
[ -n "$ALIAS" ] && echo "  test with: ssh -N $ALIAS"
echo "  then on the server: bridge-check -c <client>"
