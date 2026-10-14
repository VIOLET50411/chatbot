# iPhone 17 PWA 安装与验收

## 数据关系

这是一个 PWA，聊天页内可切换杜佳颖和纪雨。每个人物显示自己的头像、关系、历史和未读状态；每个人物又绑定自己的 VIOLET 联系人。微信与 PWA 共用该联系人的历史、记忆和关系，PWA 回复不会写入微信 outbox，因此不会在微信重复发送。

## 推荐访问方式

本地模式推荐 Tailscale 私网 HTTPS：

```powershell
.\scripts\configure_tailscale_serve.ps1
```

让 iPhone 登录同一 tailnet，在 Safari 打开脚本输出的 `https://设备名.ts.net/chat`，输入 `PWA_CHAT_TOKEN`。也可以通过 VPS 的 Caddy HTTPS 域名访问。局域网 HTTP 只适合临时调试，不作为最终“添加到主屏幕”验收路径。

管理台也支持 iPhone 小屏访问，但它是管理台根路径，不是聊天 `/chat` 外壳。管理台使用自己的五区底部导航、`admin-route` theme-color 和 `100vh` 高度链；如果从主屏幕打开管理台，检查底部标签栏、页面滚动和 safe-area 背景连续即可。不要把管理台和 `/chat` 混装进同一个 iframe 或旧的主屏幕图标。

## 安装

1. Safari 打开 `/chat`，确认能切换两个人物并读取各自历史。
2. 点分享按钮，选择“添加到主屏幕”。
3. 从主屏幕图标重新打开，确认不是 Safari 标签页模式。
4. Token 保存在此 PWA 的本地存储；不要在共享手机使用。

## UI 实现约束

`/chat` 不进入管理台 Shell，不使用 iframe。`html、body、#root、PWA 页面和聊天容器` 的外层高度链统一为 `100vh`，不混用 `100dvh / 100% / fixed body`。safe-area 只给顶部按钮、消息区和输入栏增加内边距；键盘由 VisualViewport 单独计算偏移，不改变外层高度。

Liquid Glass 只用于顶部人物切换、输入工具和关键按钮。消息正文保持高对比、低装饰；动画仅使用 transform/opacity，支持减少动态、深浅色、VoiceOver、横竖屏和至少 44pt 触控区。

当前界面采用暖沙色与琥珀色强调：聊天气泡是半透明磨砂玻璃，不使用微信绿色；连续来自同一方的消息会隐藏重复头像，保留微信式的消息分组和完整语义。管理台使用同一套暖色令牌，但正文卡片保持更高不透明度，避免玻璃叠加造成阅读疲劳。

历史消息使用游标分页。首次请求最近一页，向上滚动到顶部会带着 `before_created_at + before_id` 请求更早内容，并补偿滚动高度，避免加载后跳回底部。服务端完整账本不删除，浏览器 IndexedDB 只缓存最近 300 条作为离线启动内容。

## 真机验收

- Home Indicator 区域背景连续，底部没有白边。
- 弹出中文键盘后输入栏始终可见，历史区域可滚动且不会跳到底部外。
- 横竖屏切换后高度正常。
- 发送时显示“发送中”，成功变“已发送”；断网显示失败并可用同一幂等 ID 重试。
- 连续点击发送不会创建重复消息。
- 切换人物后历史、关系和输入状态不串。
- 开启“减少动态效果”后弹性动画被关闭。
- VoiceOver 能读出人物、消息发送者、状态和输入按钮。
- Manifest、Apple Touch Icon 和 Service Worker 均从 `/static/...` 返回正确资源，不返回 SPA HTML。

电脑关机、休眠、Docker 或 Tailscale 停止时，本地 PWA 不可用，已落库数据不丢。VPS 模式下 PWA 可继续工作，但 Windows 关机期间微信收发暂停。
