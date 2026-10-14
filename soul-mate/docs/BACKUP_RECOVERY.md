# 备份、恢复与升级

## 需要保护的内容

- `data/registry`：人物和微信绑定；
- 每人物 SQLite：联系人、聊天、关系、八层记忆、附件和队列；
- `data/platform`：加密运行设置、Owner 资料和设备数据；
- 人格素材、快照和已审核媒体；
- `.env` 与运行设置主密钥应单独保管，不能放入普通聊天或迁移包。

## 一致性备份

不要直接复制正在写入的 SQLite 文件。项目备份/迁移脚本使用 SQLite Backup API：

```powershell
python .\scripts\backup_db.py
```

脚本会递归备份根人物库、纪雨独立人物库以及 `data/platform` 下的全部 SQLite，逐库执行完整性检查，并复制不含密钥的 `data/registry/*.json`、审核语料目录和每人物附件目录。每次结果位于 `backups/soul-mate-时间/`，其中 `manifest.json` 分别列出数据库、元数据和素材附件。Cookie、日志、模型缓存、`.env` 与明文密钥不会进入该恢复点。

备份前后可以执行只读检查：

```powershell
python .\scripts\check_db.py
```

输出的 `DATABASE_COUNT` 必须与备份清单中的数据库数量一致；任何一项 `DB_OK=False` 都不要继续升级或切换。

人格大改、微信换绑、升级和 VPS 切换前都先生成恢复点。检查备份中不含 Plus 激活码、Cookie、明文 Key 和日志。

## 本地升级

1. 停止宿主微信桥接，避免升级期间继续捕获。
2. 生成一致性备份。
3. 运行后端测试、前端类型检查和生产构建。
4. `docker compose up -d --build`。
5. 检查 `/healthz`、数据卷和管理台人物/绑定。
6. 先运行微信 `-CheckOnly`，再恢复桥接。

Docker 重建不会删除绑定挂载的 `data/`。不要使用 `docker compose down -v`；VPS 的 Caddy 卷也包含证书状态。

## 恢复原则

- 恢复前停止当前核心和桥接，保证只有一处写入。
- 恢复 `backups/soul-mate-时间/data/` 中的整套同一时间点数据，不把两个不同时间点的 SQLite 目录直接合并。
- 保留当前故障数据副本，再替换为备份。
- 恢复后先启动核心并检查数据库，再进行只读窗口预检。
- outbox 中 `delivery_unknown` 必须与真实微信人工核对，不能因恢复而重发。

## VPS 切换

`prepare_vps_bundle.ps1` 生成不含 `.env` 的加密数据迁移包。`migrate_to_vps.ps1` 要求显式确认单写源，检查桥接已停止，停止本地核心，上传并部署，等待 VPS 健康后才修改 Windows 桥接地址；失败时恢复本地 `.env` 和核心。完整步骤见 `deploy/vps/README.md`。
