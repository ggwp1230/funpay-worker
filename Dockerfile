# FunPay Worker — образ для запуска на пользовательском VPS.
#
# Контейнер держит сессию FunPay 24/7 и слушает HTTP API, к которому
# подключается Electron-приложение пользователя. golden_key передаётся
# через env FUNPAY_GOLDEN_KEY, fp-токен (для авторизации входящих
# запросов) — через ACCESS_TOKEN.
#
# Конфиг бота (авто-ответы, ЧС, плагины и т.п.) хранится в /app/data,
# которая в docker-compose.yml монтируется как именованный volume,
# чтобы переживать обновления.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    AUTO_START=1

# Системные зависимости для lxml и сборок requests-toolbelt
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libxml2-dev \
        libxslt-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY backend/ /app/

# Конфиги и плагины живут в data/, которая монтируется как volume.
# Бэкенд пишет в /app/config — делаем туда симлинк, чтобы между
# обновлениями образа конфиг не терялся.
RUN mkdir -p /app/data && ln -s /app/data /app/config 2>/dev/null || true

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/ping || exit 1

CMD ["python", "-u", "main.py"]
