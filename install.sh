#!/usr/bin/env bash
# TG Recovery — установщик для Ubuntu VPS.
#   sudo bash install.sh
# Повторный запуск безопасен: код обновится, база/сессии/ключи сохранятся.
#
# Секреты в этот файл не вписываются — он лежит в git. Значения берутся из
# окружения, а чего нет — установщик спросит:
#   sudo TG_API_ID=... TG_API_HASH=... DOMAIN=example.com \
#        LE_EMAIL=you@example.com ADMIN_PASS='...' bash install.sh
set -euo pipefail

TG_API_ID="${TG_API_ID:-}"
TG_API_HASH="${TG_API_HASH:-}"
DOMAIN="${DOMAIN:-}"
LE_EMAIL="${LE_EMAIL:-}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-}"
APP_PORT="${APP_PORT:-8011}"
INSTALL_DIR="${INSTALL_DIR:-/opt/tg_recovery}"
SERVICE_USER="${SERVICE_USER:-tgrecovery}"
SERVICE_NAME="${SERVICE_NAME:-tg_recovery}"
AUTO_DELETE_CODES_DAYS="${AUTO_DELETE_CODES_DAYS:-30}"

C_G="\033[0;32m"; C_B="\033[0;34m"; C_Y="\033[1;33m"; C_R="\033[0;31m"; C_0="\033[0m"
step(){ echo -e "\n${C_B}==>${C_0} $1"; }; ok(){ echo -e "${C_G}  ✓${C_0} $1"; }; warn(){ echo -e "${C_Y}  !${C_0} $1"; }; die(){ echo -e "${C_R}  ✗ $1${C_0}" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запустите от root (sudo bash install.sh)."
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SRC_DIR/app/main.py" ] || die "Запускайте из папки проекта (где app/main.py)."

ask(){  # ask ПЕРЕМЕННАЯ "Подсказка"
    local __var="$1" __prompt="$2" __val
    __val="${!__var}"
    while [ -z "$__val" ]; do
        read -r -p "  $__prompt: " __val </dev/tty || die "Нужен интерактивный ввод или переменная $__var."
    done
    printf -v "$__var" '%s' "$__val"
}
ask_secret(){
    local __var="$1" __prompt="$2" __val __again
    __val="${!__var}"
    while [ -z "$__val" ]; do
        read -r -s -p "  $__prompt: " __val </dev/tty; echo
        [ ${#__val} -ge 8 ] || { echo "    минимум 8 символов"; __val=""; continue; }
        read -r -s -p "  Повторите: " __again </dev/tty; echo
        [ "$__val" = "$__again" ] || { echo "    не совпадают"; __val=""; }
    done
    printf -v "$__var" '%s' "$__val"
}

step "Параметры установки"
ask TG_API_ID   "TG_API_ID (my.telegram.org)"
ask TG_API_HASH "TG_API_HASH"
ask DOMAIN      "Домен (например example.com)"
ask LE_EMAIL    "E-mail для Let's Encrypt"
ask_secret ADMIN_PASS "Пароль администратора '$ADMIN_USER'"

echo -e "${C_G}\n  TG Recovery — установка${C_0}\n  Домен: $DOMAIN   Порт: $APP_PORT   Папка: $INSTALL_DIR\n"

step "Проверка порта $APP_PORT"
# Если наш сервис уже установлен и слушает порт — это обновление, а не конфликт: останавливаем перед проверкой.
if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
    warn "Останавливаю прежний экземпляр $SERVICE_NAME"
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    sleep 1
fi
if ss -ltn 2>/dev/null | grep -q ":$APP_PORT "; then
    HOLDER="$(ss -ltnp 2>/dev/null | grep ":$APP_PORT " | head -1)"
    echo -e "${C_Y}    Порт держит:${C_0} $HOLDER"
    die "Порт $APP_PORT занят другим процессом. Освободите его или поменяйте APP_PORT вверху install.sh и запустите снова."
fi
ok "Порт свободен"

step "Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx git curl certbot python3-certbot-nginx rsync >/dev/null
ok "Установлены"

step "Пользователь '$SERVICE_USER'"
if id "$SERVICE_USER" &>/dev/null; then ok "Уже есть"; else useradd -r -s /bin/bash -m -d "/home/$SERVICE_USER" "$SERVICE_USER"; ok "Создан"; fi

step "Копирование файлов в $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rsync -a --delete --exclude venv --exclude data --exclude sessions --exclude .env --exclude __pycache__ "$SRC_DIR"/ "$INSTALL_DIR"/
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/sessions"
ok "Скопировано"

step "venv и зависимости (1-2 минуты)"
[ -d "$INSTALL_DIR/venv" ] || python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
ok "Готово"

step "Конфигурация (.env)"
ENV_FILE="$INSTALL_DIR/.env"
if [ -f "$ENV_FILE" ] && grep -q '^ENCRYPTION_KEY=' "$ENV_FILE" && ! grep -q 'replace_with' "$ENV_FILE"; then
    SECRET_KEY="$(grep '^SECRET_KEY=' "$ENV_FILE" | cut -d= -f2-)"
    ENCRYPTION_KEY="$(grep '^ENCRYPTION_KEY=' "$ENV_FILE" | cut -d= -f2-)"
    warn "Сохраняю прежние ключи шифрования (аккаунты и пароли не потеряются)"
else
    SECRET_KEY="$(openssl rand -hex 32)"; ENCRYPTION_KEY="$(openssl rand -hex 32)"; ok "Новые ключи сгенерированы"
fi
cat > "$ENV_FILE" <<ENVEOF
TG_API_ID=$TG_API_ID
TG_API_HASH=$TG_API_HASH
SECRET_KEY=$SECRET_KEY
ENCRYPTION_KEY=$ENCRYPTION_KEY
HOST=127.0.0.1
PORT=$APP_PORT
DOMAIN=$DOMAIN
ENV=production
DATABASE_PATH=./data/recovery.db
SESSIONS_DIR=./sessions
AUTO_DELETE_CODES_DAYS=$AUTO_DELETE_CODES_DAYS
LOGIN_RATE_LIMIT=5
# Перед приложением стоит ровно один nginx — берём IP, который дописал он.
TRUSTED_PROXY_HOPS=1
# Security-заголовки уже отдаёт nginx, дублировать не нужно.
SEND_SECURITY_HEADERS=0
ENVEOF
ok ".env записан"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 600 "$ENV_FILE"; chmod 700 "$INSTALL_DIR/sessions" "$INSTALL_DIR/data"

step "База и администратор '$ADMIN_USER'"
sudo -u "$SERVICE_USER" bash -c 'cd "$1" && TGR_ADMIN_USER="$2" TGR_ADMIN_PASS="$3" venv/bin/python -m scripts.init_admin' _ "$INSTALL_DIR" "$ADMIN_USER" "$ADMIN_PASS"
ok "Готово"

step "systemd-сервис"
cat > "/etc/systemd/system/$SERVICE_NAME.service" <<UNITEOF
[Unit]
Description=TG Recovery
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment="PATH=$INSTALL_DIR/venv/bin"
ExecStart=$INSTALL_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $APP_PORT --workers 1
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$INSTALL_DIR/data $INSTALL_DIR/sessions
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME"
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then ok "Запущен"; else warn "Не запустился: journalctl -u $SERVICE_NAME -n 50"; fi

step "Nginx (HTTP для проверки сертификата)"
NGINX_SITE="/etc/nginx/sites-available/$SERVICE_NAME"
cat > "$NGINX_SITE" <<NGINXEOF
server {
    listen 80; listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINXEOF
mkdir -p /var/www/html
ln -sf "$NGINX_SITE" "/etc/nginx/sites-enabled/$SERVICE_NAME"
nginx -t >/dev/null 2>&1 && systemctl reload nginx
ok "HTTP настроен"

step "SSL (Let's Encrypt)"
warn "Если домен за Cloudflare (оранжевое облако) — проверка может не пройти. Включите 'DNS only' и запустите снова."
if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect -m "$LE_EMAIL" >/dev/null 2>&1; then
    ok "Сертификат получен"; SSL_OK=1
else
    warn "Сертификат не получен. Сайт пока на http://$DOMAIN"; SSL_OK=0
fi

CSP="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; img-src 'self' data:; font-src 'self' data: https://fonts.gstatic.com; connect-src 'self';"

if [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    step "Защищённая конфигурация Nginx (HTTPS)"
    cat > "$NGINX_SITE" <<NGINXEOF
server {
    listen 80; listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl http2; listen [::]:443 ssl http2;
    server_name $DOMAIN;
    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:10m;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Content-Security-Policy "$CSP" always;
    client_max_body_size 1M;
    access_log /var/log/nginx/${SERVICE_NAME}_access.log;
    error_log  /var/log/nginx/${SERVICE_NAME}_error.log;
    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
    location /static/ { proxy_pass http://127.0.0.1:$APP_PORT; expires 7d; add_header Cache-Control "public"; }
}
NGINXEOF
    nginx -t >/dev/null 2>&1 && systemctl reload nginx
    ok "HTTPS применён"
fi

step "Файрвол"
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow 'Nginx Full' >/dev/null 2>&1 || true
ufw status | grep -q "Status: active" || ufw --force enable >/dev/null 2>&1 || warn "ufw не включён"
ok "Готово"

step "Ежедневный бэкап (cron)"
CRON="0 3 * * * cd $INSTALL_DIR && $INSTALL_DIR/venv/bin/python -m scripts.backup >> $INSTALL_DIR/data/backup.log 2>&1"
( sudo -u "$SERVICE_USER" crontab -l 2>/dev/null | grep -v 'scripts.backup' ; echo "$CRON" ) | sudo -u "$SERVICE_USER" crontab -
ok "Прописан"

echo -e "\n${C_G}════════════ Установка завершена ════════════${C_0}\n"
if [ "${SSL_OK:-0}" -eq 1 ]; then echo -e "  ${C_B}https://$DOMAIN${C_0}"; else echo -e "  ${C_B}http://$DOMAIN${C_0}  (HTTPS — после Cloudflare DNS-only)"; fi
echo -e "  Логин: ${ADMIN_USER}   Пароль: тот, что вы ввели при установке\n"
echo -e "  ${C_Y}После обновления откройте сайт в режиме инкогнито или очистите кэш — иначе браузер покажет старые стили.${C_0}"
echo -e "  Управление: systemctl status|restart $SERVICE_NAME · journalctl -u $SERVICE_NAME -f\n"
