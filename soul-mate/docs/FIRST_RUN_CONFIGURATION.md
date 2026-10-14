# 首次微信跑通与全部配置指南

更新时间：2026-08-09

这份文档只回答三件事：第一次文字测试需要什么、每个可选能力填在哪里、测试成功后再开哪些功能。任何 API Key、Plus 激活码和 Cookie 都不要粘贴到聊天、日志或本文档。

## 一、纪雨文字首测已经通过

### 已经满足的条件

| 项目 | 当前状态 |
|---|---|
| Docker 核心 | `http://127.0.0.1:8080/healthz` 返回 `ok` |
| Windows 微信 | `4.1.8.107`，在 wxautox4 Plus 的支持范围内 |
| 纪雨绑定 | `Locin / Theonlywan1 / VIOLET / ji-yu` |
| 杜佳颖绑定 | `Sakino / Sakino411 / VIOLET / du-jiaying` |
| 纪雨文字模型 | DeepSeek 已保存并完成过真实连接测试 |
| 主动消息与朋友圈 | 保持关闭，不会在首次测试中自行发送 |
| 首条真实文字闭环 | 入站、生成、出站租约、发送、ACK 均成功，未重复发送 |

纯文字首测不需要阿里云百炼 Key，也不需要 X、RSS、iPhone、Tailscale 或 VPS。

### 每次重启后仍要完成的检查

Plus 授权与 Windows 用户和权限上下文有关。必须在之后运行桥接的同一个“以管理员身份运行”的 PowerShell 中检查和激活。

```powershell
cd C:\Users\32020\Desktop\chatbot\soul-mate
.\.venv-host\Scripts\wxautox4.exe auth check --json
```

只有看到 `"active": true` 才继续。如果是 `false`，在同一个窗口执行下面的本地激活；不要把授权码发给任何人：

```powershell
$plusActivationCode = Read-Host '请粘贴 Plus 授权码'
& .\.venv-host\Scripts\wxautox4.exe auth activate $plusActivationCode
Remove-Variable plusActivationCode
.\.venv-host\Scripts\wxautox4.exe auth check --json
```

展开顶部昵称为 `Locin` 的微信主窗口，不要只留在托盘，不要锁屏。然后做只读预检：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_wxautox_bridge.ps1 -CheckOnly -AccountId ji-yu
```

预检只定位窗口，不监听、不读消息、不发送。输出必须同时满足：

- `account_id` 是 `ji-yu`；
- `window_nickname` 是 `Locin`；
- `wechat_id` 是 `Theonlywan1`；
- `window_ready` 是 `true`。

不要把命令输出的 JSON 再粘贴回 PowerShell。JSON 是结果，不是下一条命令。

### 启动后续真实测试

预检通过后，在同一个管理员 PowerShell 中运行并保持窗口打开：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_wxautox_bridge.ps1 -AccountId ji-yu -ValidationMode
```

`-ValidationMode` 会正常识图、转写、调用人物模型并把回复发回微信，但测试输入和回复不写入正式聊天历史、记忆、关系进度或人格演变证据。测试通过并准备开始日常聊天时，按 `Ctrl+C` 停止，再去掉该参数重新启动。

同一次验收中的上一轮会在内存里短暂保留，最多 20 条、30 分钟，用于正确理解“你是说今天吗”等承接句；它不会落入正式历史，停止或重启 Docker 后消失。图片＋语音＋文字会在发送前额外检查是否完整承接、追问是否说明对象和时间范围。小字截图自动放大并优先 OCR，用户亲口说明的图片含义高于不确定的视觉猜测。

从备注为 `VIOLET` 的 Owner 微信向 `Locin` 发送：

```text
在吗
```

第一条文字已经验证“微信入站 → 纪雨文字模型 → 同一个微信窗口出站”。现在可以依次验证连续三到五条短消息、文字＋图片＋短语音的同轮整合和桥接重启。普通连续消息按最后一条接收后的 8 秒静默窗口聚合；含图片或语音的轮次至少等待 12 秒，并会在图片识别结束后再吸收一次刚到达的语音或图片。停止桥接时在运行窗口按 `Ctrl+C`，不要执行 `docker compose down -v`。

## 二、管理台入口

- 地址：<http://127.0.0.1:8080>
- 本机验收账号：`admin`
- 本机验收密码：`123456`

这个弱密码只允许本机或 Tailscale 私网使用，部署公网 VPS 前必须更换。

进入“日志与设置”后，在“人物的模型与外界信息”卡片顶部直接点“杜佳颖”或“纪雨”。当前人物会显示为选中状态，标题和保存按钮也会显示人物名。标注“按人物保存”的配置需要分别保存一次；同一服务允许填写同一把 Key，但数据库仍按人物隔离。外界信息状态接口也按当前人物请求，不再固定显示杜佳颖。

## 三、API Key 和模型填在哪里

位置：`日志与设置` → `人物的模型与外界信息`。先用卡片顶部的人物按钮选择编辑对象。

### 1. 文字回复：DeepSeek

位置：`文字对话模型`。

| 字段 | 建议值 |
|---|---|
| DeepSeek API Key | 每个人物可独立填写；当前两人物已经配置 |
| API 地址 | `https://api.deepseek.com/v1` |
| 默认模型 | `deepseek-v4-pro` |
| 隐藏决策模型 | 留空则沿用默认模型 |
| 自然表达模型 | 留空则沿用默认模型 |

保存后点“测试文字模型连接”。这是微信文字聊天唯一必需的模型服务。

### 2. 看懂图片：阿里云百炼

位置：`图片与语音理解`。

| 字段 | 建议值 |
|---|---|
| 阿里云百炼 Key | 在百炼控制台创建的 API Key |
| 识图 API 地址 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 识图模型 | `qwen3.7-flash` |

`qwen3.7-flash` 是当前首选：支持视觉输入，价格明显低于 Plus/Max，适合日常照片、截图、表情和 OCR。若当前地域或账号暂时不接受这个模型名，先换成 `qwen3.6-flash` 并点连接测试。`Qwen-Image`、`Wan-Image` 用于生成或编辑图片，不是“看懂收到的微信图片”，首次接入不要购买它们。

管理台不会回显 Key 原文。要让两个人物都识图，可先在纪雨保存并测试，再切换杜佳颖保存同一把 Key。

### 3. 微信语音与 PWA 音频

微信语音优先使用 Plus 的即时转写，不需要额外 API Key。先把微信文字跑通，再发一条短语音验收 `VoiceMessage.to_text()`。

PWA 上传的音频文件或 Plus 无法转写时，使用：

| 字段 | 建议值 |
|---|---|
| 语音转文字模型 | `qwen3-asr-flash` |
| 语音 API 地址 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 单独的语音 Key | 可留空，沿用当前人物的识图 Key |

`qwen-audio-3.0-tts-flash` 和 `cosyvoice-v3.5-plus` 是把文字合成为语音，不是识别收到的语音。当前项目尚未把机器人语音回复纳入首次链路，不要为首测配置 TTS。`Fun-ASR` 单价更低，但需要另一套适配；当前 `qwen3-asr-flash` 已有兼容适配，先用它更稳。

### 4. 总 Prompt 与人物补充 Prompt

位置：`日志与设置` 的 Prompt 区域。

- 总 Prompt：所有人物运行时共同使用，不写入人物快照；
- 人物补充 Prompt：只影响当前选择人物，也不写入快照；
- 两者均保留版本和回滚。

不要在 Prompt 中保存人格事实、API Key、Plus 授权码或 Cookie。

### 5. RSS、X 与小红书

位置：`新闻、X 与小红书`。

| 能力 | 需要填写 | 首测是否必需 |
|---|---|---|
| RSS | 每行 `名称 | https://订阅地址` | 否 |
| X 官方 API | Bearer Token + 公开账号列表 | 否 |
| X/小红书只读浏览器 | 宿主代理地址 + 至少 24 位 Token + 对应开关 | 否 |

社交浏览器只开放读取，不点赞、不回复、不关注、不发布。登录态只保存在专用宿主浏览器中，不交给模型。

### 6. iPhone 状态与健康数据

位置：`日志与设置` → `iPhone 状态与健康数据`。

先在管理台创建设备并选择允许的字段，再把生成的 URL 和请求体配置复制到 iPhone 快捷指令。可上传电量、充电、当前 App、城市级位置、网络、专注模式、步数、睡眠和心率摘要；不上传精确经纬度，不用于医疗诊断。首次微信测试不需要配置。逐步操作见 [iPhone 快捷指令指南](IPHONE_SHORTCUT.md)。

### 7. VPS 与 Tailscale

- 备用 VPS 地址：`日志与设置` → `本地与 VPS`；
- PWA Token：`日志与设置` → `PWA 与 iPhone 私网访问`，点“生成安全 Token”，复制一次后保存；
- VPS 完整部署与回滚：`deploy/vps/README.md`；
- iPhone 私网访问：Windows 与 iPhone 都安装 Tailscale、登录同一账号，然后运行 `scripts/configure_tailscale_serve.ps1`；
- PWA 真机说明：`docs/PWA_IPHONE.md`。

只填写 VPS 地址不会自动迁移数据。实际切换必须停旧核心、迁移最新备份、验证新核心，再切换 Windows 桥接地址，禁止本地和 VPS 同时写同一份世界线。

## 四、启动秘密与灾难恢复配置

文件：`C:\Users\32020\Desktop\chatbot\soul-mate\.env`

这些值当前已经存在，正常首测不要修改：

- `WECHAT_BRIDGE_TOKEN`：Windows 桥接访问 Docker 的凭据；
- `RUNTIME_SETTINGS_MASTER_KEY`：加密管理台密钥和 Owner 敏感资料；
- `PWA_CHAT_TOKEN`：仅作为 PWA 登录口令的首次启动/灾难恢复默认值；日常可在管理台替换；
- `OWNER_PASSWORD_HASH` 或管理员启动认证；
- `WECHAT_ACCOUNTS_JSON`：仅用于账号目录损坏时的灾难恢复，不是日常编辑来源。

日常人物绑定、联系人、模型、API Key、Prompt、RSS、社交平台、手机与 VPS 地址都应在管理台修改，不要直接改 `.env`。

## 五、第一次测试通过后的顺序

1. 纪雨发一条文字；
2. 连续发三到五条短消息，验证只聚合回复一次；
3. 连续发一句提示、一张普通图片和一段五秒左右语音，图片与语音交换顺序再测一次；每轮只能回复一次，并且要同时结合全部信息；
4. 未配置视觉 Key 时必须承认看不清，不能编造；Plus 转写为空时安全跳过；
5. 停止并重启桥接，确认没有重复回复；
6. 再按相同步骤测试杜佳颖；
7. 两个单账号都通过后才启动双账号；
8. 主动消息、收藏表情和朋友圈继续保持关闭，待单独验收。

## 六、常见错误

| 现象 | 处理 |
|---|---|
| `active: false` | 在运行桥接的同一个管理员 PowerShell 里重新激活并再次检查 |
| PowerShell 报 `UnexpectedToken` | 不要把 JSON 输出粘贴回命令行，只复制本文中的命令 |
| `window_ready: false` | 展开准确昵称窗口；确认 4.1.8.107；不要锁屏；确认授权在同一权限上下文有效 |
| 直接运行 `.ps1` 被策略拦截 | 使用本文的 `powershell -NoProfile -ExecutionPolicy Bypass -File ...` 形式 |
| 文字模型失败 | 在管理台检查当前人物的 Key、余额、Base URL 与模型名，点连接测试 |
| 图片模型失败 | 检查百炼 Key、地域、`qwen3.7-flash` 模型权限和图片大小 |
| 语音没有内容 | 先看 Plus 是否提供转写；不要把 TTS 模型当 ASR 使用 |
| Docker 服务无法访问 | 管理员 PowerShell 运行 `docker compose up -d --build`，再检查 `/healthz` |
