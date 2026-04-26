# Phase 1: VPS multi-tenant FunPay keepalive — deployment

С этой версии VPS-сервер не только раздаёт обновления, но и держит сессии
FunPay-аккаунтов клиентов онлайн 24/7 (даже когда у пользователя выключен ПК).

## Что нужно сделать на VPS один раз

### 1. Установить новые зависимости

```bash
cd /opt/funpay-server
source venv/bin/activate    # если venv был
pip install -r requirements.txt   # подтянет FunPayAPI, cryptography
```

### 2. Сгенерировать ключ шифрования golden_key

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Скопируй вывод (строка вида `xZk...=`).

### 3. Положить ключ в env-файл systemd-сервиса

Открой файл `/etc/systemd/system/funpay-server.service` (или где у тебя
лежит unit) и добавь в секцию `[Service]`:

```ini
Environment=FP_GOLDEN_KEY_AES=<вставь_сюда_сгенерированный_ключ>
```

Или, если используешь EnvironmentFile:

```bash
echo 'FP_GOLDEN_KEY_AES=<key>' >> /etc/funpay-server.env
```

### 4. Перезапустить сервис

```bash
systemctl daemon-reload
systemctl restart funpay-server
journalctl -u funpay-server -f --since '10s ago'
```

В логах должно появиться:

```
[startup] funpay_worker запущен, активных воркеров: 0
```

Если видишь `[funpay_worker] not available: No module named 'FunPayAPI'` —
зависимости не установились в правильный venv.

### 5. Проверка endpoint'ов

```bash
curl -sS -X POST http://localhost:9000/api/account/upload_key \
  -H "X-Token: fp_<твой_тестовый_токен>" \
  -H "Content-Type: application/json" \
  -d '{"golden_key":"<реальный_golden_key>"}'
# {"ok":true}

curl -sS -X POST http://localhost:9000/api/account/start \
  -H "X-Token: fp_<твой_тестовый_токен>"
# {"ok":true,"username":"...","balance":...,"active_sales":0}

curl -sS http://localhost:9000/api/account/status \
  -H "X-Token: fp_<твой_тестовый_токен>"
# {"online":true,...}
```

## Безопасность

* `FP_GOLDEN_KEY_AES` хранится только в env systemd-сервиса. Файл `accounts/*.json`
  содержит только зашифрованный golden_key — скомпрометированный бэкап файлов
  бесполезен без ключа.
* Доступ к endpoint'ам `/api/account/*` защищён `X-Token` (тот же fp_-токен,
  который клиент использует для скачивания обновлений).

## Что дальше (Phase 2/3)

Эта итерация делает только keepalive-онлайн. Авто-ответы, ЧС, плагины
по-прежнему работают на стороне клиента (на ПК). В Phase 2 они переедут
на VPS, чтобы продолжать работать пока ПК выключен.
