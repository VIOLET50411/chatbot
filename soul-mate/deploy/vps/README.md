# Soul Mate 可选 VPS 模式

这套方案把管理台、PWA、人物核心、调度器、数据库和本地向量模型放在 Linux VPS 的 Docker 中。`wxautox4 Plus` 仍必须运行在已登录且未锁屏的 Windows 微信电脑上；它通过 HTTPS 主动连接 VPS，因此 VPS 不需要访问你的 Windows 入站端口。

## 一台 Plus 能否运行两个角色

可以。Plus 是这台 Windows 机器上的自动化能力，不是每个人物一份。两个微信账号都在同一 Windows 会话中登录后，宿主代理通过窗口顶部昵称分别连接 `du-jiaying` 与 `ji-yu`，两套数据库和关系进度仍完全隔离。微信界面操作共用一把锁、顺序执行，所以高并发时会排队；无需购买第二份 Plus。只有当两个账号必须同时在两台不同电脑上运行时，才需要第二台已授权机器。

## 首次切换到 VPS

1. 准备一台至少 4 核、8 GB 内存、40 GB 磁盘的 Linux VPS，并把域名 A/AAAA 记录指向它；开放 TCP 80、443 和 UDP 443。
2. 首次在 VPS 手工准备好目录、域名和 `deploy/vps/.env.vps`。之后推荐使用 `scripts/migrate_to_vps.ps1` 完成停本地核心、离线导出、上传、远端替换、健康检查和桥接地址切换；脚本失败会恢复本地核心。只生成迁移包时可运行 `scripts/prepare_vps_bundle.ps1`。输出不含 `.env`、Plus 激活码、模型缓存和日志，但包含经 SQLite Backup API 导出的私有数据。
3. 把压缩包传到 VPS 的空目录并解压。复制 `deploy/vps/.env.vps.example` 为 `deploy/vps/.env.vps`，只在 VPS 本地填写密钥。
4. 公网模式禁止继续使用 `admin / 123456`。在 VPS 项目根目录执行下面命令，输入一个新密码，并把输出写入 `OWNER_PASSWORD_HASH`；`ADMIN_PASSWORD` 保持为空：

   ```bash
   python -c "import getpass; from web.platform_auth import hash_owner_password; print(hash_owner_password(getpass.getpass('新管理密码: ')))"
   ```

   同时为 `RUNTIME_SETTINGS_MASTER_KEY` 生成独立的高强度随机值。它用于加密管理台保存的 DeepSeek、百炼、X 和社交浏览器密钥；不要与登录密码、PWA Token 或桥接 Token 复用。

5. 设置强随机的 `PWA_CHAT_TOKEN` 和 `WECHAT_BRIDGE_TOKEN`，两者不能相同。然后启动：

   ```bash
   mkdir -p deploy/vps/logs
   docker compose -f deploy/vps/docker-compose.vps.yml up -d --build
   docker compose -f deploy/vps/docker-compose.vps.yml ps
   ```

6. 初次手工部署可在健康后设置桥接地址；后续一条命令迁移为：

   ```powershell
   .\scripts\migrate_to_vps.ps1 -SshTarget 'root@服务器' -VpsUrl 'https://你的域名' -IUnderstandOnlyOneCoreMayWrite
   ```

   迁移前微信桥接必须停止，远端必须已有可用的 `.env.vps`。管理台可保存并测试备用 VPS 地址，但不会在浏览器内搬运私有数据库。

7. iPhone 快捷指令的上传地址改为 `https://你的域名/api/device/v1/push`。`device_token` 放在 JSON 请求体，不放 URL。

## 切回本地

先停止 VPS 核心，导回最新数据后再启动本地核心，禁止两套核心同时写同一世界线。Windows 端执行：

```powershell
.\scripts\set_core_mode.ps1 -Mode Local
docker compose up -d --build
.\scripts\start_wxautox_bridge.ps1
```

数据回迁必须以停机后的最新 VPS 数据包为准；不要把本机旧 `data` 与 VPS 新 `data` 直接合并。

## 电脑关闭或重启的影响

- 本地模式：电脑关闭后管理台、PWA、定时任务、手机数据接收和微信回复全部停止；Docker 与宿主桥接重启后，已落库的数据和待发送队列会恢复。
- VPS 模式：电脑关闭后管理台、PWA、手机数据接收、记忆维护和资讯刷新继续运行；微信收发、微信图片/表情和朋友圈停止，待发送动作保留到 Windows 与微信恢复。微信在关机期间未被 Plus 回调捕获的消息，是否能补取取决于微信会话列表保留情况，不能承诺完全补齐。
- 两种模式下，Windows 锁屏、微信退出登录、窗口版本/布局变化都可能让 Plus 暂停；文字功能与表情面板有独立降级，不会因为表情选择器失效而盲发。

## 社交平台在 VPS 模式下

推荐把 `ai-social-browser` 的 headed Chrome、relay 和只读动作服务放在 VPS 私有 Docker 网络，或通过 Tailscale/WireGuard 让核心访问 Windows 的鉴权代理。绝不能把原项目 8272/8273 或 Chrome 9333 端口直接暴露公网。首次登录、验证码和风控挑战必须人工完成；本项目永远不自动绕过验证，也不允许点赞、回复或发布。
