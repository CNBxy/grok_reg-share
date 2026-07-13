#!/usr/bin/env sh
set -eu

REPO_URL="${REPO_URL:-https://github.com/sosoveooo-bit/grok_reg-share.git}"
REPO_REF="${REPO_REF:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/grok_reg-share}"

log() {
    printf '%s\n' "[*] $*"
}

die() {
    printf '%s\n' "[!] $*" >&2
    exit 1
}

generate_secret() {
    openssl rand -hex 24
}

if [ "$(id -u)" -ne 0 ]; then
    die "请使用 root 运行，推荐：curl ... | sudo sh"
fi

if ! command -v apt-get >/dev/null 2>&1; then
    die "当前一键安装脚本仅支持 Ubuntu 或 Debian"
fi

export DEBIAN_FRONTEND=noninteractive
log "安装基础依赖"
apt-get update
apt-get install -y ca-certificates curl git openssl

if ! command -v docker >/dev/null 2>&1; then
    log "安装 Docker Engine 与 Compose 插件"
    curl -fsSL https://get.docker.com | sh
fi

docker compose version >/dev/null 2>&1 || die "Docker Compose 插件不可用"
systemctl enable --now docker >/dev/null 2>&1 || true

if [ -d "$INSTALL_DIR/.git" ]; then
    log "更新现有项目"
    git -C "$INSTALL_DIR" fetch origin "$REPO_REF"
    git -C "$INSTALL_DIR" checkout "$REPO_REF"
    git -C "$INSTALL_DIR" pull --ff-only origin "$REPO_REF"
elif [ -e "$INSTALL_DIR" ]; then
    die "安装目录已存在但不是 Git 仓库：$INSTALL_DIR"
else
    log "克隆项目到 $INSTALL_DIR"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --branch "$REPO_REF" --single-branch "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

generated_credentials=0
if [ ! -f .env.vps ]; then
    domain="${DOMAIN:-}"
    if [ -z "$domain" ]; then
        if [ ! -r /dev/tty ]; then
            die "无法读取交互输入，请通过 DOMAIN=reg.example.com 传入域名"
        fi
        printf '请输入已指向此 VPS 的域名（不带 http/https）：' >/dev/tty
        IFS= read -r domain </dev/tty
    fi

    case "$domain" in
        ''|http://*|https://*|*/*|*:*|*' '*)
            die "域名格式无效，请填写类似 reg.example.com 的纯域名"
            ;;
    esac

    admin_user="${WEB_ADMIN_USER:-admin}"
    admin_password="${WEB_ADMIN_PASSWORD:-$(generate_secret)}"
    webhook_secret="${EMAIL_WEBHOOK_SECRET:-$(generate_secret)}"

    [ "${#admin_password}" -ge 16 ] || die "WEB_ADMIN_PASSWORD 至少需要 16 位"
    [ "${#webhook_secret}" -ge 16 ] || die "EMAIL_WEBHOOK_SECRET 至少需要 16 位"

    umask 077
    cat >.env.vps <<EOF
DOMAIN=$domain
WEB_ADMIN_USER=$admin_user
WEB_ADMIN_PASSWORD=$admin_password
EMAIL_WEBHOOK_SECRET=$webhook_secret
OPENAI_CPA_CLOUDMAIL_FALLBACK=false
TZ=Asia/Shanghai
CLOUDMAIL_URL=
CLOUDMAIL_ADMIN_EMAIL=
CLOUDMAIL_PASSWORD=
GROK2API_APP_KEY=
EOF
    generated_credentials=1
else
    log "保留现有 .env.vps 配置"
fi

log "构建并启动服务"
sh deploy/vps/deploy.sh

domain="$(sed -n 's/^DOMAIN=//p' .env.vps | tail -n 1)"
admin_user="$(sed -n 's/^WEB_ADMIN_USER=//p' .env.vps | tail -n 1)"

printf '\n部署完成：%s\n' "https://$domain"
printf '管理账号：%s\n' "${admin_user:-admin}"

if [ "$generated_credentials" -eq 1 ]; then
    printf '管理密码：%s\n' "$admin_password"
    printf 'Webhook 密钥：%s\n' "$webhook_secret"
    printf '%s\n' '请立即安全保存以上两个密钥。'
fi

printf '%s\n' 'Cloudflare Worker 的 EMAIL_WEBHOOK_URL 填上面的 HTTPS 地址。'
printf '%s\n' 'VPS 防火墙只开放 22、80、443，不要开放 5000。'
