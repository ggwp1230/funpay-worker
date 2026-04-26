#!/usr/bin/env bash
# FunPay Worker — one-line installer
# Запуск:
#   curl -sSL https://raw.githubusercontent.com/ggwp1230/funpay-worker/main/vps_server/install.sh | sudo bash
#
# Что делает скрипт:
#   1. Ставит Docker (если нет)
#   2. Спрашивает 6-значный код у @FPNexusBot (/vps) — регистрирует VPS на
#      центральном API и получает fp-токен пользователя
#   3. Спрашивает golden_key с https://funpay.com/account/profile (cookie golden_key)
#   4. Клонирует репозиторий funpay-worker и собирает Docker-образ
#   5. Запускает контейнер и cron на авто-обновление образа
#   6. Выводит fp-токен + URL для вставки в Electron-приложение
set -euo pipefail

# ── Цвета ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'
LINE="${DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

# ── Конфиг ───────────────────────────────────────────────────────────────────
APP_NAME="funpay-worker"
APP_DIR="/opt/funpay-worker"
SRC_DIR="${APP_DIR}/src"
DATA_VOL="funpay-worker-data"
PORT="${PORT:-8000}"
IMAGE_TAG="funpay-worker:local"
REPO_URL="${REPO_URL:-https://github.com/ggwp1230/funpay-worker}"
REPO_BRANCH="${REPO_BRANCH:-main}"
API_URL="${API_URL:-http://funpaybot.duckdns.org:9000}"

echo -e "\n${LINE}"
echo -e "${BOLD}   FunPay Worker — установка на VPS${RESET}"
echo -e "${LINE}\n"

# ── Root check ────────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  echo -e "${RED}Запустите от root:${RESET}"
  echo -e "  curl -sSL ${REPO_URL}/raw/${REPO_BRANCH}/vps_server/install.sh | sudo bash"
  exit 1
fi

# ── Docker ───────────────────────────────────────────────────────────────────
echo -e "${DIM}Проверяю Docker...${RESET}"
if ! command -v docker &>/dev/null; then
  echo -e "  ${YELLOW}Docker не найден. Устанавливаю...${RESET}"
  curl -fsSL https://get.docker.com | bash -s -- -q
  systemctl enable docker --now 2>/dev/null || true
fi
if ! docker compose version &>/dev/null; then
  echo -e "  ${YELLOW}Docker Compose plugin не найден. Устанавливаю...${RESET}"
  apt-get update -qq && apt-get install -y -qq docker-compose-plugin || true
fi
DOCKER_VER=$(docker --version | grep -oP '\d+\.\d+\.\d+' | head -1)
echo -e "  ${GREEN}Docker ${DOCKER_VER}${RESET}"

# ── Registry mirrors ─────────────────────────────────────────────────────────
# Если docker.io напрямую не отвечает (бывает у российских/CIS VPS), Docker
# не сможет скачать базовый образ. Прописываем публичные зеркала; если
# daemon.json уже существует — сохраняем все остальные настройки и только
# мержим в него ключ "registry-mirrors".
echo -e "${DIM}Настраиваю зеркала Docker Hub...${RESET}"
if ! curl -fsS --max-time 4 https://registry-1.docker.io/v2/ -o /dev/null 2>/dev/null; then
  echo -e "  ${YELLOW}registry-1.docker.io недоступен — включаю зеркала${RESET}"
fi
mkdir -p /etc/docker
DAEMON_JSON="/etc/docker/daemon.json"
MIRRORS='["https://mirror.gcr.io","https://dockerhub.timeweb.cloud","https://docker.rainbond.cc","https://huecker.io"]'

_write_fresh_daemon_json() {
  cat > "$DAEMON_JSON" <<JSON
{
  "registry-mirrors": ${MIRRORS}
}
JSON
}

if [[ -f "$DAEMON_JSON" ]]; then
  cp "$DAEMON_JSON" "${DAEMON_JSON}.bak.$(date +%s)"
  # Сохраняем все существующие ключи, меняем только registry-mirrors. Если
  # python3 нет или файл битый — пишем минимальный с нуля.
  if command -v python3 &>/dev/null; then
    python3 - "$DAEMON_JSON" "$MIRRORS" <<'PY' || _write_fresh_daemon_json
import json, sys
path, mirrors = sys.argv[1], sys.argv[2]
try:
    with open(path) as f: d = json.load(f)
except Exception:
    d = {}
if not isinstance(d, dict): d = {}
d["registry-mirrors"] = json.loads(mirrors)
with open(path, "w") as f: json.dump(d, f, indent=2)
PY
  else
    _write_fresh_daemon_json
  fi
else
  _write_fresh_daemon_json
fi

systemctl restart docker 2>/dev/null || service docker restart 2>/dev/null || true
sleep 2
echo -e "  ${GREEN}Зеркала прописаны в ${DAEMON_JSON}${RESET}"

# ── git ──────────────────────────────────────────────────────────────────────
if ! command -v git &>/dev/null; then
  apt-get update -qq && apt-get install -y -qq git
fi

# ── OTP ───────────────────────────────────────────────────────────────────────
echo -e "\n${LINE}"
echo -e "${BOLD}   Получите 6-значный код у бота @FPNexusBot — команда /vps${RESET}"
echo -e "${LINE}"
while true; do
  echo -ne "${BOLD}Код из бота: ${RESET}"
  read -r OTP_CODE </dev/tty
  OTP_CODE=$(echo "$OTP_CODE" | tr -d ' \r\n' | tr '[:lower:]' '[:upper:]')
  if [[ ${#OTP_CODE} -eq 6 ]]; then break; fi
  echo -e "${RED}Код должен быть 6 символов${RESET}"
done

# ── Регистрация VPS на API ───────────────────────────────────────────────────
# golden_key пользователь введёт уже в Electron-приложении — оно само пушит
# его на VPS через POST /api/config. На этом этапе ключ не нужен.
echo -e "\n${DIM}Регистрирую VPS на ${API_URL}...${RESET}"
SERVER_IP=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null \
  || curl -s --max-time 5 https://ifconfig.me 2>/dev/null \
  || hostname -I | awk '{print $1}')
OS_INFO=$(. /etc/os-release && echo "$PRETTY_NAME")

REG_RESP=$(curl -s --max-time 15 -X POST "${API_URL}/api/vps/register" \
  -H "Content-Type: application/json" \
  -d "{\"otp\":\"${OTP_CODE}\",\"ip\":\"${SERVER_IP}\",\"os\":\"${OS_INFO}\",\"docker\":\"${DOCKER_VER}\"}" \
  2>/dev/null || echo '{"error":"no_connection"}')

TOKEN=$(echo "$REG_RESP" | grep -oP '"token"\s*:\s*"\K[^"]+' || true)
REG_ERROR=$(echo "$REG_RESP" | grep -oP '"error"\s*:\s*"\K[^"]+' || true)

if [[ -z "$TOKEN" ]]; then
  if [[ "$REG_ERROR" == "invalid_otp" ]]; then
    echo -e "${RED}Неверный или просроченный код. Получите новый у бота /vps.${RESET}"
    exit 1
  fi
  echo -e "${RED}Не удалось зарегистрироваться: ${REG_ERROR:-нет соединения}${RESET}"
  exit 1
fi
echo -e "  ${GREEN}VPS зарегистрирован, токен получен${RESET}"

# ── Клонирование исходников ──────────────────────────────────────────────────
echo -e "\n${DIM}Скачиваю исходники...${RESET}"
mkdir -p "${APP_DIR}"
if [[ -d "${SRC_DIR}/.git" ]]; then
  git -C "${SRC_DIR}" fetch --depth=1 origin "${REPO_BRANCH}" 2>/dev/null
  git -C "${SRC_DIR}" reset --hard "origin/${REPO_BRANCH}" 2>/dev/null
else
  rm -rf "${SRC_DIR}"
  git clone --depth=1 --branch "${REPO_BRANCH}" "${REPO_URL}" "${SRC_DIR}"
fi
echo -e "  ${GREEN}Исходники готовы (${SRC_DIR})${RESET}"

# ── Сборка образа ────────────────────────────────────────────────────────────
echo -e "\n${DIM}Собираю Docker-образ (~2 мин)...${RESET}"
docker build -t "${IMAGE_TAG}" "${SRC_DIR}" >/dev/null
echo -e "  ${GREEN}Образ ${IMAGE_TAG} собран${RESET}"

# ── docker volume ────────────────────────────────────────────────────────────
docker volume create "${DATA_VOL}" &>/dev/null || true

# ── docker-compose.yml ───────────────────────────────────────────────────────
cat > "${APP_DIR}/docker-compose.yml" <<COMPOSE
services:
  ${APP_NAME}:
    image: ${IMAGE_TAG}
    container_name: ${APP_NAME}
    restart: unless-stopped
    ports:
      - "${PORT}:8000"
    environment:
      # golden_key приходит через POST /api/config из приложения и хранится
      # в data-volume (/app/config/golden_key.dat). FUNPAY_GOLDEN_KEY оставлен
      # как опциональный fallback, но в обычном флоу он не используется.
      - ACCESS_TOKEN=${TOKEN}
      - AUTO_START=1
      - HOST=0.0.0.0
      - PORT=8000
    volumes:
      - ${DATA_VOL}:/app/data
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/ping"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s

volumes:
  ${DATA_VOL}:
    external: true
COMPOSE

# ── Запуск ───────────────────────────────────────────────────────────────────
echo -e "\n${DIM}Запускаю контейнер...${RESET}"
docker stop "${APP_NAME}" 2>/dev/null || true
docker rm   "${APP_NAME}" 2>/dev/null || true
(cd "${APP_DIR}" && docker compose up -d) >/dev/null

# ── Авто-обновление через cron ──────────────────────────────────────────────
cat > "${APP_DIR}/update.sh" <<UPD
#!/usr/bin/env bash
# Каждые 30 минут пересобираем образ из последнего main, если что-то поменялось.
set -euo pipefail
cd "${SRC_DIR}"
LOCAL=\$(git rev-parse HEAD)
git fetch --depth=1 origin "${REPO_BRANCH}" >/dev/null 2>&1 || exit 0
REMOTE=\$(git rev-parse origin/${REPO_BRANCH})
if [[ "\$LOCAL" == "\$REMOTE" ]]; then exit 0; fi
git reset --hard "origin/${REPO_BRANCH}"
docker build -t "${IMAGE_TAG}" "${SRC_DIR}" >/dev/null
cd "${APP_DIR}" && docker compose up -d --force-recreate >/dev/null
echo "[\$(date)] обновлено до \${REMOTE:0:7}"
UPD
chmod +x "${APP_DIR}/update.sh"

CRON_LINE="*/30 * * * * ${APP_DIR}/update.sh >> ${APP_DIR}/update.log 2>&1"
( (crontab -l 2>/dev/null || true) | grep -v "${APP_DIR}/update.sh" ; echo "${CRON_LINE}" ) | crontab -

# ── Ожидание готовности ──────────────────────────────────────────────────────
echo -ne "${DIM}Жду пока FunPay-сессия инициализируется"
for i in $(seq 1 30); do
  if curl -sf "http://localhost:${PORT}/ping" &>/dev/null; then
    echo -e " ${GREEN}готово${RESET}"
    break
  fi
  echo -n "."; sleep 1
done

# ── Сохраняем конфиг для пользователя ────────────────────────────────────────
cat > "${APP_DIR}/worker.conf" <<CONF
URL=http://${SERVER_IP}:${PORT}
TOKEN=${TOKEN}
PORT=${PORT}
IP=${SERVER_IP}
INSTALLED_AT=$(date -Iseconds)
CONF
chmod 600 "${APP_DIR}/worker.conf"

# ── Финальный вывод ──────────────────────────────────────────────────────────
echo -e "\n${LINE}"
echo -e "${GREEN}${BOLD}   Готово! VPS-воркер запущен.${RESET}"
echo -e "${LINE}\n"
echo -e "${BOLD}Адрес воркера:${RESET}    ${CYAN}http://${SERVER_IP}:${PORT}${RESET}"
echo -e "${BOLD}fp-токен:${RESET}         ${CYAN}${TOKEN}${RESET}"
echo -e ""
echo -e "${DIM}Скопируйте оба значения и вставьте в приложение FP Nexus при первом запуске."
echo -e "Там же приложение попросит ваш golden_key (cookie с funpay.com) и сразу"
echo -e "запустит бота — после этого аккаунт уйдёт в онлайн на 24/7.${RESET}"
echo -e ""
echo -e "${DIM}Управление:${RESET}"
echo -e "  ${DIM}docker logs -f ${APP_NAME}${RESET}              просмотр логов"
echo -e "  ${DIM}docker compose -f ${APP_DIR}/docker-compose.yml restart${RESET}   рестарт"
echo -e "  ${DIM}cat ${APP_DIR}/worker.conf${RESET}              вывести токен повторно"
echo -e ""
