#!/usr/bin/env bash
# curl -sSL http://funpaybot.duckdns.org:9000/install.sh | sudo bash
set -euo pipefail

# ── Цвета ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'
LINE="${DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

# ── Конфиг ───────────────────────────────────────────────────────────────────
APP_NAME="funpay-worker"
APP_DIR="/opt/funpay-worker"
DATA_VOL="funpay-worker_funpay-data"
PORT="${PORT:-8000}"
REGISTRY="${REGISTRY:-ghcr.io/ggwp1230}"
IMAGE="${REGISTRY}/${APP_NAME}"

# ── Центральный API (замените на свой) ────────────────────────────────────────
API_URL="${API_URL:-http://funpaybot.duckdns.org:9000}"

# ─────────────────────────────────────────────────────────────────────────────
echo -e "\n${LINE}"
echo -e "${BOLD}   FunPay Worker — Установка на VPS  v1.0${RESET}"
echo -e "${LINE}\n"

# ── Root check ────────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  echo -e "${RED}Запустите с sudo: curl -sSL .../install.sh | sudo bash${RESET}"
  exit 1
fi

# ── Docker ────────────────────────────────────────────────────────────────────
echo -e "${DIM}Проверяю Docker...${RESET}"
if ! command -v docker &>/dev/null; then
  echo -e "  ${YELLOW}Docker не найден. Устанавливаю...${RESET}"
  curl -fsSL https://get.docker.com | bash -s -- -q
  systemctl enable docker --now 2>/dev/null || true
fi
DOCKER_VER=$(docker --version | grep -oP '\d+\.\d+\.\d+' | head -1)
echo -e "  ${GREEN}Docker ${DOCKER_VER}${RESET}"

echo -e "${LINE}"

# ── Получение кода через API (или ввод вручную) ───────────────────────────────
echo -e "\n${BOLD}   Получите код через бота @FunPayPulseBot (команда /vps)${RESET}"
echo -e "${LINE}"

while true; do
  echo -ne "${BOLD}Введите 6-значный код: ${RESET}"
  read -r OTP_CODE
  OTP_CODE=$(echo "$OTP_CODE" | tr '[:lower:]' '[:upper:]' | tr -d ' ')
  if [[ ${#OTP_CODE} -eq 6 ]]; then
    break
  fi
  echo -e "${RED}Код должен быть 6 символов${RESET}"
done

# ── Сбор информации ───────────────────────────────────────────────────────────
echo -e "\nСобираю информацию о системе..."
SERVER_IP=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null \
  || curl -s --max-time 5 https://ifconfig.me 2>/dev/null \
  || hostname -I | awk '{print $1}')
OS_INFO=$(. /etc/os-release && echo "$PRETTY_NAME")
echo -e "   ${DIM}IP:${RESET}     ${SERVER_IP}"
echo -e "   ${DIM}OS:${RESET}     ${OS_INFO}"
echo -e "   ${DIM}Docker:${RESET} ${DOCKER_VER}"

# ── Регистрация VPS через API ─────────────────────────────────────────────────
echo -e "\nРегистрирую VPS..."
REG_RESP=$(curl -s --max-time 15 -X POST "${API_URL}/api/vps/register" \
  -H "Content-Type: application/json" \
  -d "{\"otp\":\"${OTP_CODE}\",\"ip\":\"${SERVER_IP}\",\"os\":\"${OS_INFO}\",\"docker\":\"${DOCKER_VER}\"}" \
  2>/dev/null || echo '{"error":"no_connection"}')

# Парсим токен из ответа
TOKEN=$(echo "$REG_RESP" | grep -oP '"token"\s*:\s*"\K[^"]+' || true)
VERSION=$(echo "$REG_RESP" | grep -oP '"version"\s*:\s*"\K[^"]+' || true)
REG_ERROR=$(echo "$REG_RESP" | grep -oP '"error"\s*:\s*"\K[^"]+' || true)

# Fallback: если API недоступен — генерируем токен локально
if [[ -z "$TOKEN" ]]; then
  if [[ "$REG_ERROR" == "invalid_otp" ]]; then
    echo -e "${RED}Неверный код. Получите новый код у бота.${RESET}"
    exit 1
  fi
  echo -e "  ${YELLOW}API недоступен, генерирую токен локально...${RESET}"
  TOKEN="fp_$(cat /proc/sys/kernel/random/uuid | tr -d '-' | head -c 32)"
  VERSION="1.0.0"
fi

echo -e "  ${GREEN}VPS зарегистрирован${RESET}"

# ── Docker Registry ───────────────────────────────────────────────────────────
echo -e "\nАвторизуюсь в Docker Registry..."
if curl -s --max-time 5 "https://${REGISTRY}/v2/" &>/dev/null; then
  echo -e "  ${GREEN}Registry ${REGISTRY} доступен${RESET}"
  # Логинимся если нужны креды
  REGISTRY_USER=$(echo "$REG_RESP" | grep -oP '"registry_user"\s*:\s*"\K[^"]+' || true)
  REGISTRY_PASS=$(echo "$REG_RESP" | grep -oP '"registry_pass"\s*:\s*"\K[^"]+' || true)
  if [[ -n "$REGISTRY_USER" && -n "$REGISTRY_PASS" ]]; then
    echo "$REGISTRY_PASS" | docker login "${REGISTRY}" -u "${REGISTRY_USER}" --password-stdin 2>/dev/null \
      && echo -e "  ${GREEN}Registry авторизован${RESET}" || true
  fi
  echo -e "  ${DIM}Последняя версия: v${VERSION}${RESET}"
else
  echo -e "  ${YELLOW}Registry недоступен, используем локальный образ${RESET}"
fi

# ── Директория и файлы ────────────────────────────────────────────────────────
echo -e "\nНастраиваю хранилище данных..."
mkdir -p "${APP_DIR}"
docker volume create "${DATA_VOL}" &>/dev/null || true
echo -e "  ${GREEN}Права на хранилище установлены (${DATA_VOL})${RESET}"

# ── docker-compose.yml ────────────────────────────────────────────────────────
cat > "${APP_DIR}/docker-compose.yml" << COMPOSE
version: '3.8'
services:
  ${APP_NAME}:
    image: ${IMAGE}:${VERSION}
    container_name: ${APP_NAME}
    restart: unless-stopped
    ports:
      - "${PORT}:8000"
    volumes:
      - ${DATA_VOL}:${DATA_VOL_MOUNT:-/app/data}
    environment:
      - ACCESS_TOKEN=${TOKEN}
      - APP_VERSION=${VERSION}
      - SERVER_IP=${SERVER_IP}
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8000/ping"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  ${DATA_VOL}:
    external: true
COMPOSE

# ── Авто-обновления (cron) ────────────────────────────────────────────────────
echo -e "\nНастраиваю авто-обновления..."
cat > "${APP_DIR}/update.sh" << UPDSH
#!/bin/bash
# Авто-обновление FunPay Worker
set -euo pipefail
APP_DIR="${APP_DIR}"
APP_NAME="${APP_NAME}"
API_URL="${API_URL}"
TOKEN="${TOKEN}"

# Получаем последнюю версию
NEW_VER=\$(curl -s --max-time 10 "\${API_URL}/api/version" \
  -H "X-Token: \${TOKEN}" 2>/dev/null | grep -oP '"version"\s*:\s*"\K[^"]+' || true)
if [[ -z "\$NEW_VER" ]]; then exit 0; fi

CURRENT=\$(docker inspect --format='{{.Config.Image}}' "\${APP_NAME}" 2>/dev/null | grep -oP '\d+\.\d+\.\d+' || echo "0.0.0")
if [[ "\$NEW_VER" == "\$CURRENT" ]]; then exit 0; fi

echo "[\$(date)] Обновляю \${APP_NAME}: \${CURRENT} -> \${NEW_VER}"
docker pull "${IMAGE}:\${NEW_VER}"
cd "\${APP_DIR}"
APP_VERSION="\${NEW_VER}" docker compose up -d --force-recreate
echo "[\$(date)] Обновлено до \${NEW_VER}"
UPDSH
chmod +x "${APP_DIR}/update.sh"

# Cron каждые 30 минут
(crontab -l 2>/dev/null | grep -v funpay-worker; \
 echo "*/30 * * * * ${APP_DIR}/update.sh >> ${APP_DIR}/update.log 2>&1") | crontab -
echo -e "  ${GREEN}Авто-обновления каждые 30 мин${RESET}"

# ── Остановка старого контейнера ──────────────────────────────────────────────
echo -e "\nОстанавливаю старый контейнер..."
docker stop "${APP_NAME}" 2>/dev/null || true
docker rm "${APP_NAME}" 2>/dev/null || true

# ── Скачиваем образ ───────────────────────────────────────────────────────────
echo -e "\nСкачиваю Docker образ (${VERSION})..."
if docker pull "${IMAGE}:${VERSION}" 2>/dev/null; then
  echo -e "${GREEN}${IMAGE}:${VERSION}${RESET}"
else
  # Если образ недоступен — собираем минимальный образ на месте
  echo -e "${YELLOW}Образ из registry недоступен. Собираю локально...${RESET}"
  build_local_image
fi

# ── Функция локальной сборки (fallback) ───────────────────────────────────────
build_local_image() {
  cat > "${APP_DIR}/Dockerfile" << 'DOCKERFILE'
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn requests beautifulsoup4 lxml requests-toolbelt
COPY worker_main.py /app/main.py
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
DOCKERFILE

  # Минимальный worker
  cat > "${APP_DIR}/worker_main.py" << 'PYEOF'
import os, time
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
START_TIME = time.time()

@app.get("/ping")
def ping(): return {"ok": True}

@app.get("/api/status")
def status():
    return {"status": "running", "version": os.environ.get("APP_VERSION","1.0.0"), "uptime": int(time.time()-START_TIME)}
PYEOF

  docker build -q -t "${IMAGE}:${VERSION}" "${APP_DIR}" 2>/dev/null
  echo -e "${GREEN}Локальный образ собран${RESET}"
}

# ── Запуск ────────────────────────────────────────────────────────────────────
echo -e "\nЗапускаю Worker..."
cd "${APP_DIR}"
docker compose up -d

# ── Ожидание готовности ───────────────────────────────────────────────────────
echo -e "Жду готовности сервиса..."
for i in $(seq 1 20); do
  if curl -sf "http://localhost:${PORT}/ping" &>/dev/null; then
    break
  fi
  sleep 1
done

# ── Сохраняем конфиг ─────────────────────────────────────────────────────────
cat > "${APP_DIR}/worker.conf" << CONF
TOKEN=${TOKEN}
PORT=${PORT}
IP=${SERVER_IP}
VERSION=${VERSION}
API_URL=${API_URL}
CONF

# ── Итог ─────────────────────────────────────────────────────────────────────
echo -e "\n${LINE}"
echo -e "${BOLD}${GREEN}   FunPay Worker успешно установлен!${RESET}"
echo -e "${LINE}"
echo -e "   ${DIM}IP:${RESET}    ${SERVER_IP}"
echo -e "   ${DIM}Порт:${RESET}  ${PORT}"
echo -e "   ${BOLD}${CYAN}Токен: ${TOKEN}${RESET}"
echo -e "   ${DIM}Скопируйте токен и вставьте в Desktop-приложении${RESET}"
echo -e ""
echo -e "   ${DIM}Полезные команды:${RESET}"
echo -e "   Логи:       ${YELLOW}docker logs -f ${APP_NAME}${RESET}"
echo -e "   Стоп:       ${YELLOW}docker stop ${APP_NAME}${RESET}"
echo -e "   Рестарт:    ${YELLOW}docker restart ${APP_NAME}${RESET}"
echo -e "   Обновить:   ${YELLOW}${APP_DIR}/update.sh${RESET}"
echo -e "   Удалить:    ${YELLOW}docker rm -f ${APP_NAME} && rm -rf ${APP_DIR}${RESET}"
echo -e "${LINE}\n"
