#!/usr/bin/env bash
# TG Recovery — деинсталлятор (зеркало install.sh).
# Останавливает проект, файлы/базу/сессии оставляет на месте.  sudo bash uninstall.sh
set -uo pipefail

SERVICE_NAME="${SERVICE_NAME:-tg_recovery}"
SERVICE_USER="${SERVICE_USER:-tgrecovery}"
INSTALL_DIR="${INSTALL_DIR:-/opt/tg_recovery}"
DOMAIN="${DOMAIN:-$(grep -s '^DOMAIN=' "${INSTALL_DIR}/.env" | cut -d= -f2- || true)}"
DOMAIN="${DOMAIN:-<ваш домен>}"

C_G="\033[0;32m"; C_B="\033[0;34m"; C_Y="\033[1;33m"; C_R="\033[0;31m"; C_0="\033[0m"
step(){ echo -e "\n${C_B}==>${C_0} $1"; }; ok(){ echo -e "${C_G}  ✓${C_0} $1"; }; warn(){ echo -e "${C_Y}  !${C_0} $1"; }

[ "$(id -u)" -eq 0 ] || { echo -e "${C_R}Запустите от root (sudo bash uninstall.sh).${C_0}"; exit 1; }

echo -e "${C_Y}\n  TG Recovery — остановка проекта${C_0}"
echo "  Остановятся: сервис, автозапуск, cron-бэкап, раздача сайта в Nginx."
echo "  НЕ удалятся:  файлы, база, сессии, зависимости, SSL-сертификат."
echo ""
read -r -p "  Продолжить? (y/N): " ANS
[ "$ANS" = "y" ] || [ "$ANS" = "Y" ] || { echo "Отменено."; exit 0; }

step "Остановка и отключение сервиса"
if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
    systemctl stop "$SERVICE_NAME" 2>/dev/null && ok "Сервис остановлен" || warn "Уже не запущен"
    systemctl disable "$SERVICE_NAME" 2>/dev/null && ok "Автозапуск отключён" || true
else warn "Unit-файл не найден"; fi

step "Удаление задачи бэкапа из cron"
if id "$SERVICE_USER" &>/dev/null; then
    if sudo -u "$SERVICE_USER" crontab -l 2>/dev/null | grep -q 'scripts.backup'; then
        sudo -u "$SERVICE_USER" crontab -l 2>/dev/null | grep -v 'scripts.backup' | sudo -u "$SERVICE_USER" crontab - 2>/dev/null
        ok "Задача бэкапа убрана"
    else warn "Задача бэкапа не найдена"; fi
else warn "Пользователь $SERVICE_USER не найден"; fi

step "Отключение сайта в Nginx"
if [ -L "/etc/nginx/sites-enabled/$SERVICE_NAME" ]; then
    rm -f "/etc/nginx/sites-enabled/$SERVICE_NAME"
    ok "Сайт отключён (конфиг сохранён в sites-available)"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx && ok "Nginx перезагружен" || warn "Проверьте конфиг Nginx"
else warn "Активный сайт Nginx не найден"; fi

echo -e "\n${C_G}════════════ Проект остановлен ════════════${C_0}\n"
echo "  Сайт https://$DOMAIN больше не обслуживается. Данные сохранены в $INSTALL_DIR"
echo ""
echo -e "  ${C_B}Вернуть в работу:${C_0}"
echo "    sudo ln -sf /etc/nginx/sites-available/$SERVICE_NAME /etc/nginx/sites-enabled/"
echo "    sudo systemctl enable --now $SERVICE_NAME && sudo systemctl reload nginx"
echo ""
echo -e "  ${C_Y}Удалить полностью:${C_0}"
echo "    sudo rm -f /etc/systemd/system/$SERVICE_NAME.service && sudo systemctl daemon-reload"
echo "    sudo rm -f /etc/nginx/sites-available/$SERVICE_NAME"
echo "    sudo rm -rf $INSTALL_DIR && sudo userdel -r $SERVICE_USER"
echo "    sudo certbot delete --cert-name $DOMAIN"
echo ""
