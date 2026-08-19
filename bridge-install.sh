#!/bin/bash
# ============================================================================
#  Ferry 服务器端安装
#
#      git clone https://github.com/hinschow/Ferry.git ~/Ferry
#      cd ~/Ferry && bash bridge-install.sh
#
#  工具源码在同目录的 server/ 下，本脚本只负责装。
#  早先这里内嵌了一份全部工具的副本（好处是单文件可 scp），结果同一份代码
#  在仓库里存两遍、改完要记得重新生成 —— 漂移过好几次（index-exclude.txt
#  就装错过旧版）。现在 server/ 是唯一来源。
#
#  客户端首次连接时会自动上报用户名/系统/工具路径并领取一个不冲突的隧道端口，
#  服务器这边不需要预先配置任何机器信息。
# ============================================================================
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/server"

if [ ! -d "$SRC" ]; then
  cat >&2 <<EOF
❌ 找不到 $SRC

   本脚本要和 server/ 目录放在一起。请克隆整个仓库再执行：
     git clone https://github.com/hinschow/Ferry.git ~/Ferry
     cd ~/Ferry && bash bridge-install.sh
EOF
  exit 1
fi

echo "==> 1/4 检查依赖"
if ! command -v sshfs >/dev/null 2>&1; then
  echo "    安装 sshfs ..."
  (apt-get update -qq && apt-get install -y -qq sshfs) >/dev/null 2>&1 \
    || { echo "    ❌ sshfs 安装失败，请手动安装后重试"; exit 1; }
fi
[ -e /dev/fuse ] || { echo "    ❌ 无 /dev/fuse，本机（容器？）不支持 FUSE 挂载"; exit 1; }
command -v iconv >/dev/null 2>&1 || echo "    ⚠️ 无 iconv，bridge-run 的中文支持会受影响"
echo "    sshfs / FUSE 可用"

echo "==> 2/4 创建目录与配置"
# 不预建客户端档案 —— 客户端首次连接时由 bridge-register 用真实信息自动创建。
# 挂载根按客户端分开：/root/mnt/<机器名>/
mkdir -p /root/.winbridge/clients /root/.winbridge/status /root/.winbridge/index
mkdir -p /root/.winbridge/mounts /root/.winbridge/invites /root/mnt
chmod 700 /root/.winbridge/invites
if [ ! -f /root/.winbridge/config ]; then
  cat > /root/.winbridge/config <<'CFGEOF'
# 全局可选项。每台本地机的信息在 clients/<机器名>.conf，由客户端自动上报生成，
# 不要在这里写机器相关的东西。
# ACTIVE_PROJECT='/root/mnt/<机器名>/<目录>'   # bridge-sync-md 会把它标为「当前主项目」
CFGEOF
fi
# 端口是整机全局资源。各角色跑在自己家目录里，看不到别人的登记 ——
# 这个共享目录让它们分配端口时能互相避让。setgid 保证新文件归 ferry 组。
getent group ferry >/dev/null || groupadd ferry
install -d -g ferry -m 2775 /var/lib/ferry/ports
echo "    /root/.winbridge/{clients,status,index,mounts,invites} 与 /root/mnt"
echo "    /var/lib/ferry/ports（角色间共享的端口登记）"
# 把已有客户端的端口补登进共享表 —— 它们是在共享表存在之前登记的，
# 不补的话新建的角色会挑到同一个端口，一连上就 remote port forwarding failed。
for cf in /root/.winbridge/clients/*.conf; do
  [ -f "$cf" ] || continue
  cn=$(sed -n 's/^NAME=//p' "$cf" | head -1)
  cp_=$(sed -n 's/^PORT=//p' "$cf" | sed 's/#.*//; s/[[:space:]]//g' | head -1)
  [ -n "$cn" ] && [ -n "$cp_" ] && printf 'root\t%s\t%s\n' "$cn" "$cp_" > "/var/lib/ferry/ports/root.$cn"
done

# ---- 从老版本升上来时，把确定已死的东西清掉 ----
# 重装不清理的话，这些会一直留着：孤儿守护进程照样每 3 秒空转，
# 改名前的旧脚本还 source 着早已删除的配置键，跑起来必错。
LEGACY=0
# ① 单客户端时代的状态守护（现在是每客户端一个 statusd-<名>.pid）
if [ -f /root/.winbridge/statusd.pid ]; then
  OLDPID=$(cat /root/.winbridge/statusd.pid 2>/dev/null)
  # 只杀确认是旧式的：旧守护用 STATUS_DIR=，新的每客户端守护用 SDIR=。
  # pid 可能已被系统回收给别的进程，所以必须核对命令行再动手。
  CL=$(tr "\0" " " < "/proc/$OLDPID/cmdline" 2>/dev/null || true)
  if [ -n "$OLDPID" ] && kill -0 "$OLDPID" 2>/dev/null &&
     case "$CL" in *STATUS_DIR=/root/.winbridge/status*) case "$CL" in *SDIR=*) false ;; *) true ;; esac ;; *) false ;; esac; then
    kill "$OLDPID" 2>/dev/null && LEGACY=$((LEGACY+1))
  fi
  rm -f /root/.winbridge/statusd.pid
fi
# ② win-* 改名为 bridge-* 时留下的旧脚本（软链是有效的兼容入口，保留）
for f in /usr/local/bin/win-*.legacy /usr/local/bin/win-statusd /usr/local/bin/winrun /usr/local/bin/winrun2; do
  [ -e "$f" ] && [ ! -L "$f" ] && { rm -f "$f"; LEGACY=$((LEGACY+1)); }
  [ -L "$f" ] && [ ! -e "$(readlink -f "$f")" ] && { rm -f "$f"; LEGACY=$((LEGACY+1)); }
done
# ③ config 里单客户端时代的键（每台机器的信息现在在 clients/<名>.conf）
if [ -f /root/.winbridge/config ] && grep -qE '^(WIN_USER|WIN_PORT|WIN_PROJECT|TOOL_DIR_WIN|MOUNT_POINT|STATUS_DIR)=' /root/.winbridge/config; then
  KEEP=$(grep -E '^ACTIVE_PROJECT=' /root/.winbridge/config || true)
  cp /root/.winbridge/config /root/.winbridge/config.bak
  { echo "# 全局可选项。每台本地机的信息在 clients/<机器名>.conf，由客户端自动上报生成，"
    echo "# 不要在这里写机器相关的东西。"
    [ -n "$KEEP" ] && echo "$KEEP"
  } > /root/.winbridge/config
  LEGACY=$((LEGACY+1))
  echo "    已精简 config（旧版备份在 config.bak）"
fi
[ "$LEGACY" -gt 0 ] && echo "    清理了 $LEGACY 处老版本残留"

# ---- 让 sshd 自己回收僵死会话 ----
# 客户端被强杀/断网时，服务器这边的会话收不到 FIN，会一直挂着不放隧道端口。
# sshd 默认 ClientAliveInterval=0（永不主动探测），这种尸体可能挂几小时。
# 下次控制台起来要建隧道，就撞上自己的尸体：remote port forwarding failed。
# 60×3 = 最多 3 分钟回收。ListenAddress 那条坑不适用（这两项在 Match 块外合法）。
if ! grep -qE '^\s*ClientAliveInterval\s' /etc/ssh/sshd_config 2>/dev/null; then
  cp /etc/ssh/sshd_config /etc/ssh/sshd_config.ferry.bak
  printf '\n# Ferry: 回收僵死会话，避免隧道端口被尸体占住\nClientAliveInterval 60\nClientAliveCountMax 3\n' \
    >> /etc/ssh/sshd_config
  if sshd -t 2>/dev/null; then
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
    echo "    已开启 sshd 会话回收（ClientAliveInterval 60 × 3）"
  else
    cp /etc/ssh/sshd_config.ferry.bak /etc/ssh/sshd_config
    echo "    ⚠️ sshd_config 校验失败，已回滚，未改动"
  fi
fi

echo "==> 3/4 生成专用密钥"
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

echo "==> 4/4 安装命令行工具"
install -d -m 0755 /usr/local/lib/ferry
install -m 0644 "$SRC/lib.sh" /usr/local/lib/ferry/lib.sh
install -m 0644 "$SRC/claude-md-block.md" /usr/local/lib/ferry/claude-md-block.md
install -m 0644 "$SRC/index-exclude.txt" /root/.winbridge/index-exclude.txt
rm -f /root/.winbridge/lib.sh      # 旧位置，已移到 /usr/local/lib/ferry/
N=0
for f in "$SRC"/bridge-*; do
  [ -f "$f" ] || continue
  install -m 0755 "$f" "/usr/local/bin/$(basename "$f")"
  N=$((N + 1))
done
# 旧名兼容：早期版本叫 win-*，保留软链免得手指记忆失效
for p in win-run:bridge-run win-check:bridge-check win-mounts:bridge-mounts \
         win-grep:bridge-grep win-git:bridge-git win-reset:bridge-reset \
         win-daemon:bridge-daemon win-mount:bridge-mount win-umount:bridge-umount; do
  ln -sfn "/usr/local/bin/${p##*:}" "/usr/local/bin/${p%%:*}"
done
echo "    已安装 $N 个 bridge-* 命令（含 win-* 兼容软链）"

# 跑着的状态守护还是老代码 —— 不重启的话装完等于没装，
# 用户会看到「升级了但行为一点没变」。
RESTARTED=""
for pidf in /root/.winbridge/statusd-*.pid; do
  [ -f "$pidf" ] || continue
  c=$(basename "$pidf" .pid); c=${c#statusd-}
  if kill -0 "$(cat "$pidf" 2>/dev/null)" 2>/dev/null; then
    bridge-statusd stop -c "$c" >/dev/null 2>&1
    bridge-statusd start -c "$c" >/dev/null 2>&1
    RESTARTED="$RESTARTED $c"
  fi
done
[ -n "$RESTARTED" ] && echo "    已用新代码重启状态守护：$RESTARTED"

# Ferry 的使用纪律写进 Claude 的用户级记忆（~/.claude/CLAUDE.md 的标记块）。
# 会话恢复超时怎么救、挂载为什么不能遍历 —— 这些知识必须跟着安装走，
# 而不是留在某台服务器的某个文件里，换台机器就没人知道了。
. /usr/local/lib/ferry/lib.sh
bridge_install_claude_block "$HOME"
CN=1
if getent group ferry >/dev/null 2>&1; then
  FGID=$(getent group ferry | cut -d: -f3)
  for u in $(getent passwd | awk -F: -v g="$FGID" '$4==g{print $1}'); do
    h=$(getent passwd "$u" | cut -d: -f6)
    [ -d "$h" ] && bridge_install_claude_block "$h" "$u" && CN=$((CN+1))
  done
fi
echo "    Claude 使用纪律已写入 $CN 个账户的 ~/.claude/CLAUDE.md"

SRV=$(curl -s -m 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
HOSTN=$(hostname)

cat <<TIPEOF

════════════════════════════════════════════════════════════════════
 服务器端装好了。接下来：这台服务器上发接入码 → 本地电脑粘贴。
════════════════════════════════════════════════════════════════════

本服务器的公钥（手工配置时才需要）：

$(cat /root/.ssh/id_bridge.pub)


【第 1 步】就在这台服务器上，给每台要接入的电脑各发一张接入码：

  bridge-invite --name 我的Mac

  会打印一段 FERRY1: 开头的接入码。每台电脑发一张，名字别重复
  （重名会被拦下并告诉你怎么改）。
  ⚠️ 接入码里含本服务器的登录凭据，只发给你信任的机器。

【第 2 步】在【你的本地电脑】上取得客户端（不是在这台服务器上）：

  git clone https://github.com/hinschow/Ferry.git ~/Ferry && cd ~/Ferry

  最省事：cd client && python3 ferry_agent.py --open   浏览器打开就是完整界面，零构建

  想要原生窗口和托盘图标：
    cd client/electron && npm install && npm run build
    然后双击 client/_electron/Ferry-win32-x64/Ferry.exe（macOS 是 Ferry.app）
    预编译的包也可以从 Releases 下载

  本机的 SSH 服务要开着，服务器靠它回连：
  Windows 管理员 PowerShell：
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0; Start-Service sshd
  macOS：系统设置 → 通用 → 共享 → 远程登录

【第 3 步】客户端 →「＋ 添加」→ 选「是（粘贴接入码）」→ 粘贴整段 → 完成

  授权服务器公钥、保存登录密钥、写 SSH 别名、建立隧道，客户端全自动做完。
  隧道端口也由服务器自动分配，这边不用配任何机器信息。

【验证】回到这台服务器执行：

  bridge-check            # 列出所有已接入的机器
  bridge-mounts           # 看挂载
  bridge-invite --list    # 看发过哪些接入码、是否还有效

【接入码的其它用法】

  bridge-invite --list           已发放的
  bridge-invite --show <名字>    弄丢了重新显示
  bridge-invite --revoke <名字>  吊销某一台，其它不受影响

【不想用接入码？】本服务器公钥就是上面那行，可以手工配：

  Windows：powershell -ExecutionPolicy Bypass -File client/setup-windows.ps1 -PubKey "上面那行公钥" -ServerHost $SRV -Alias $HOSTN -LoopbackOnly -AutoStart
  macOS  ：bash client/setup-mac.sh --pubkey "上面那行公钥" --host $SRV --alias $HOSTN --autostart

  若还要指定连本服务器用的私钥，加 -Identity / --identity（填【私钥】路径，不带 .pub）。

════════════════════════════════════════════════════════════════════
TIPEOF
