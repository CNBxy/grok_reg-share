#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f .env.vps ]; then
    cp .env.vps.example .env.vps
    chmod 600 .env.vps
    echo "已生成 .env.vps。请先填写 DOMAIN、WEB_ADMIN_PASSWORD 和 EMAIL_WEBHOOK_SECRET，然后重新执行："
    echo "  bash deploy/vps/deploy.sh"
    exit 1
fi

if grep -Eq '^DOMAIN=(|reg\.example\.com)$' .env.vps \
    || grep -Eq '^WEB_ADMIN_PASSWORD=(|replace-with-)' .env.vps \
    || grep -Eq '^EMAIL_WEBHOOK_SECRET=(|replace-with-)' .env.vps; then
    echo "部署已停止：请先把 .env.vps 中的域名和两个示例密钥替换为真实值。"
    #exit 1
fi

ADMIN_PASSWORD="$(sed -n 's/^WEB_ADMIN_PASSWORD=//p' .env.vps | tail -n 1)"
WEBHOOK_SECRET="$(sed -n 's/^EMAIL_WEBHOOK_SECRET=//p' .env.vps | tail -n 1)"
if [ "${#ADMIN_PASSWORD}" -lt 16 ] || [ "${#WEBHOOK_SECRET}" -lt 16 ]; then
    echo "部署已停止：WEB_ADMIN_PASSWORD 和 EMAIL_WEBHOOK_SECRET 都必须至少 16 位。"
    exit 1
fi

if [ ! -f config.json ]; then
    cp config.example.json config.json
else
    # 已有 config.json：容器启动时会自动 merge config.example.json 中的新增字段
    echo "检测到已有 config.json，将在容器启动时自动合并新增配置项..."
fi

touch accounts_cli.txt emails_used.txt emails_error.txt
mkdir -p cpa_auths cookies screenshots
chmod 600 .env.vps config.json accounts_cli.txt emails_used.txt emails_error.txt
chmod 700 cpa_auths cookies screenshots

docker compose --env-file .env.vps -f docker-compose.vps.yml up -d --build
docker compose --env-file .env.vps -f docker-compose.vps.yml ps
