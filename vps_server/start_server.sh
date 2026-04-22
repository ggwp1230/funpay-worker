#!/bin/bash
# Запуск сервера обновлений на VPS
# Использование: bash start_server.sh

export ADMIN_TOKEN="${ADMIN_TOKEN:-change_me_admin}"
export ACCESS_TOKEN="${ACCESS_TOKEN:-change_me_user}"
export PORT="${PORT:-9000}"

pip install -r requirements.txt -q
echo "[Server] Admin panel: http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_VPS_IP'):$PORT/admin"
echo "[Server] ADMIN_TOKEN: $ADMIN_TOKEN"
echo "[Server] ACCESS_TOKEN (для клиентов): $ACCESS_TOKEN"
python server.py
