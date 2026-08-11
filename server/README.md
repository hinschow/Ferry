# 服务器端工具源码

这些是安装到 `/usr/local/bin/` 的脚本。`../bridge-install.sh` 是把它们打包后的
一键安装器，由 `regen-installer.sh` 生成 —— **改完工具记得重新生成**：

```bash
bash regen-installer.sh          # 会读取 /usr/local/bin/bridge-* 重新打包
```

| 脚本 | 作用 |
|---|---|
| `lib.sh` | 公共库：客户端解析、按 OS 分派路径转换 |
| `bridge-register` | 客户端自注册（上报身份、领取隧道端口） |
| `bridge-run` | 在客户端机器上原生执行命令 |
| `bridge-mount` / `bridge-umount` / `bridge-mounts` | 挂载管理 |
| `bridge-check` | 连通性检查 |
| `bridge-grep` / `bridge-git` | 搜索与 git（跑在客户端本机） |
| `bridge-daemon` | 长驻进程（Windows→schtasks，Mac/Linux→nohup） |
| `bridge-statusd` | 状态守护，每客户端一个 |
| `bridge-add-client` | 手工添加客户端（一般用不到，客户端会自注册） |
| `bridge-reset` | 清理卡死的 SSH 复用连接 |
