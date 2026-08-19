# Ferry 桥接：本机挂载了远程电脑的目录 —— 使用纪律

这台服务器通过 Ferry（sshfs 反向隧道）挂载了本地电脑的目录（`bridge-mounts` 查看）。
挂载路径上每次文件操作都要过一次网络往返（实测 350–700ms），规则：

- ✅ 读写**已知路径**的单个文件：Read/Edit 正常用
- ✅ 找文件：`bridge-find <关键词>`（查预建索引，毫秒级；文件增删后跑 `bridge-index` 刷新）
- ✅ 搜内容 / git / 构建测试：`bridge-grep` / `bridge-git` / `bridge-run`（在对方机器原生执行，~1 秒）
- ❌ **绝不**在挂载路径上跑 `find`、`grep -r`、`rg`、`ls -R`、`git status`，
  也不要用 Glob/Grep/Explore 工具扫挂载 —— 全树遍历要 2 分钟以上，
  且多个会话共享一条隧道，谁遍历谁把所有人堵死

## 会话打不开（标签页自动关闭、加载 60 秒被杀）

引用了大量挂载文件的会话，重开时每个文件要冷 stat 一次（~440ms，**已删除的也要付一次往返**），
几百个文件就超过 60 秒加载限制。预热缓存无效（attr_timeout=15s，热完就过期）。解法：

    bridge-pause -c <机器名> 120    # 暂时卸掉数据挂载：路径变本地 ENOENT，恢复瞬间完成
                                    # 窗口内重开会话，到点自动挂回

`bridge-check` 会列出有此风险的会话。预防：大批量读挂载文件走 bridge-grep / bridge-run，
别在一个会话里用 Read 逐个啃几百个挂载文件 —— 对话记录越大越难恢复。

## 常用命令

`bridge-check`（体检 + 风险会话）· `bridge-mounts`（挂载表）· `bridge-mount / bridge-umount` ·
`bridge-invite`（发接入码）· `bridge-as <名>`（管理员查看某角色的挂载）· `bridge-port-clean`（清僵死隧道）

工具源码与完整文档：https://github.com/hinschow/Ferry
