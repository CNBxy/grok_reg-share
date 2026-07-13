# Linux VPS 部署

推荐系统：Ubuntu 22.04/24.04 或 Debian 12，至少 2 核 4GB 内存。并发注册或 CPA Mint 建议 4 核 8GB 以上。

## 1. 准备 VPS

安装 Docker Engine 和 Compose 插件，并将域名的 A/AAAA 记录指向 VPS。安全组/防火墙只需开放：

- TCP 22（SSH）
- TCP 80（Caddy 申请证书和 HTTP 跳转）
- TCP/UDP 443（HTTPS/HTTP3）

不要开放应用容器的 5000 端口。

## 2. 上传并启动

如果使用 `sosoveooo-bit/grok_reg-share` 公开 Fork，可在全新的 Ubuntu / Debian VPS 上直接执行：

```bash
curl -fsSL https://raw.githubusercontent.com/sosoveooo-bit/grok_reg-share/main/install-vps.sh | sudo sh
```

脚本会安装 Docker、询问域名、生成管理密码和 Webhook 密钥，并自动完成下面的 Compose 部署。运行前必须先把域名 A/AAAA 记录指向 VPS。

手动上传项目时，在项目根目录执行：

```bash
bash deploy/vps/deploy.sh
```

第一次执行会生成 `.env.vps` 并停止。编辑以下必填项：

```dotenv
DOMAIN=reg.example.com
WEB_ADMIN_USER=admin
WEB_ADMIN_PASSWORD=管理面随机强密码
EMAIL_WEBHOOK_SECRET=邮件Webhook随机强密钥
```

可使用下面的命令生成两个不同的随机值：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

填完后再次执行：

```bash
bash deploy/vps/deploy.sh
```

访问 `https://你的域名`，浏览器会要求输入管理账号和密码。

## 3. 配置 OpenAI-CPA 邮箱

在 Web 控制台的“配置管理”中：

1. 邮箱服务商选择 `OpenAI-CPA 内存池`。
2. `defaultDomains` 填已接入 Cloudflare Email Routing 的收件域名。
3. 通信密钥可留空，因为 VPS 的 `.env.vps` 中 `EMAIL_WEBHOOK_SECRET` 优先级更高。
4. 保存配置。

Cloudflare 邮件 Worker 变量：

```text
EMAIL_WEBHOOK_URL=https://你的域名
EMAIL_WEBHOOK_SECRET=与 .env.vps 完全相同的值
```

Worker 会自动推送到：

```text
https://你的域名/api/webhook/email
```

## 4. 代理注意事项

原 Windows 配置里的 `127.0.0.1:10809` 在容器中代表容器自身，不能直接使用。请改成 VPS/容器可以访问的代理地址。

如果代理运行在 VPS 宿主机，可尝试：

```text
http://host.docker.internal:代理端口
```

但宿主机代理必须监听 Docker 网桥可访问的地址，不能只监听宿主机 `127.0.0.1`。

## 5. 常用命令

```bash
# 查看状态
docker compose --env-file .env.vps -f docker-compose.vps.yml ps

# 查看日志
docker compose --env-file .env.vps -f docker-compose.vps.yml logs -f --tail=200

# 重启
docker compose --env-file .env.vps -f docker-compose.vps.yml restart

# 更新代码后重建
docker compose --env-file .env.vps -f docker-compose.vps.yml up -d --build

# 停止（保留账号、配置和证书）
docker compose --env-file .env.vps -f docker-compose.vps.yml down
```

持久化文件仍保存在项目根目录的 `config.json`、`accounts_cli.txt`、`cpa_auths/` 等路径中。备份这些文件即可迁移。
