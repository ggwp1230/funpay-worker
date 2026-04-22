#!/bin/bash
# Установка Telegram бота на VPS
set -e

echo "Устанавливаю зависимости бота..."
pip3 install python-telegram-bot --break-system-packages -q

echo "Копирую bot.py..."
cp /opt/funpay-server/bot.py /opt/funpay-server/bot.py

# Создаём systemd сервис
cat > /etc/systemd/system/funpay-bot.service << SERVICE
[Unit]
Description=FunPay Pulse Telegram Bot
After=network.target

[Service]
WorkingDirectory=/opt/funpay-server
Environment="BOT_TOKEN=${BOT_TOKEN}"
Environment="ADMIN_IDS=${ADMIN_IDS}"
ExecStart=/usr/bin/python3 /opt/funpay-server/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable funpay-bot
systemctl start funpay-bot
echo "Бот запущен!"
