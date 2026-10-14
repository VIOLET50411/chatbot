# Tailscale 私网 HTTPS

Tailscale Serve 把本机 `127.0.0.1:8080` 通过 tailnet 内的 HTTPS 地址提供给 iPhone，不开放到公众互联网。它适合本地模式下安装 PWA 和在外网访问管理台；它不能让关机后的电脑继续提供服务。

## 启用

1. Windows 和 iPhone 安装 Tailscale，并登录同一 tailnet。
   Windows 可在管理员 PowerShell 执行 `winget install --id Tailscale.Tailscale -e`；iPhone 从 App Store 安装。
2. 启动 Docker 核心。
3. 在管理员 PowerShell 运行：

   ```powershell
   .\scripts\configure_tailscale_serve.ps1
   ```

4. 脚本检查本机健康和 Tailscale 登录状态，再执行持久后台 Serve。使用输出的 `https://...ts.net/chat`。

后台模式会在 Tailscale 或电脑重启后恢复配置。查看状态：

```powershell
tailscale serve status
```

关闭并清空 Soul Mate 的 Serve 配置：

```powershell
.\scripts\configure_tailscale_serve.ps1 -Disable
```

脚本只使用 Serve，不使用 Funnel；Funnel 会公开到互联网，不属于本方案。tailnet 访问策略仍应只允许你的设备。官方命令参考：[Tailscale Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve)。

手机状态快捷指令的逐步配置见 [iPhone 快捷指令](IPHONE_SHORTCUT.md)。
