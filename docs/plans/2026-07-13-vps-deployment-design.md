# Grok 注册机 VPS 部署设计

## 目标

将 Web 控制台部署到 Linux VPS，同时保证 Cloudflare 邮件 Worker 能公开调用邮件 Webhook，管理面、账号信息、邮箱凭证和 CPA 文件不能匿名访问。

## 架构

公网流量先进入 Caddy，由 Caddy自动申请和续期 HTTPS 证书，再转发给单进程、多线程的 Waitress 服务。Flask 管理页面和除邮件 Webhook、健康检查以外的 API 使用 HTTP Basic Auth。`/api/webhook/email` 不使用管理面密码，而是继续通过 `X-Webhook-Secret` 和 `EMAIL_WEBHOOK_SECRET` 做独立校验。

Waitress 必须保持单进程运行，因为 OpenAI-CPA 验证码池位于进程内存。多进程部署会造成邮件写入一个进程、注册线程却从另一个进程读取。多线程可以同时处理 Webhook、页面请求和注册任务。

## 容器与数据

应用容器安装 Chromium、Xvfb 和中文字体。浏览器仍按“有头”方式运行，但画面由 Xvfb 提供，适合无桌面的 VPS。Compose 为 Chromium 配置较大的 `/dev/shm`。

以下内容通过宿主机绑定挂载持久化：

- `config.json`
- `accounts_cli.txt`
- `emails_used.txt` / `emails_error.txt`
- `cpa_auths/`
- `cookies/`
- `screenshots/`

部署脚本负责首次创建这些文件和目录，不在镜像中保存真实密码或 API Key。

## 安全边界

- 管理面密码仅从 VPS `.env.vps` 环境变量读取，不写入项目配置接口。
- 公网请求在未配置管理密码时拒绝访问管理面；本机回环访问保持兼容。
- Webhook 限制 JSON 请求体大小，并使用恒定时间方式比较通信密钥。
- 管理 API 响应禁止缓存，并增加防点击劫持、MIME 嗅探和来源策略响应头。
- Caddy 只负责 HTTPS，应用自身负责认证，因此即使绕过 Caddy 也不能匿名访问管理 API。

## 验证

- 单元测试覆盖：管理面无认证、正确认证、错误认证、Webhook 免管理认证、跨站写请求拒绝。
- 现有 OpenAI-CPA 内存池测试继续通过。
- 校验 Compose 展开结果、JSON 配置、Python 语法和本地浏览器页面。
