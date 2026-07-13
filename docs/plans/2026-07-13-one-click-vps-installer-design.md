# VPS 一键安装设计

## 目标

为公开 Fork 提供一条可直接执行的 Ubuntu / Debian 安装命令。安装器负责准备 Docker 环境、获取当前项目、生成安全配置并调用现有 Compose 部署脚本；业务配置仍由 Web 控制台管理，避免在安装脚本中复制注册和邮箱逻辑。

## 方案

根目录新增 `install-vps.sh`。脚本必须以 root 运行，仅支持带 `apt-get` 的 Ubuntu / Debian。它安装 `curl`、`git`、`openssl` 等基础依赖，在 Docker 不存在时使用官方安装脚本安装 Docker Engine 与 Compose 插件，然后将仓库克隆到 `/opt/grok_reg-share`。重复运行时仅执行 `git pull --ff-only`，保留 `.env.vps`、`config.json`、账号文件和 CPA 凭证等 Git 忽略的数据。

首次安装通过 `/dev/tty` 读取纯域名，自动生成两个不同的 48 位十六进制随机值，分别作为管理密码和邮件 Webhook 密钥。脚本以权限受限的 `.env.vps` 保存配置，再复用 `deploy/vps/deploy.sh` 完成目录初始化、Compose 构建和健康状态展示。完成时只在首次生成凭证时将密码显示给操作者，并提醒不要开放 5000 端口。

## 验证

使用 `sh -n install-vps.sh` 检查 POSIX Shell 语法；使用现有 Python 单元测试验证邮件内存池、Web 安全和生产入口；使用 `git diff --check` 检查补丁格式。GitHub 发布前再次确认 `.env.vps`、`config.json`、账号、Cookie、截图和 CPA 凭证均未进入 Git 索引。
