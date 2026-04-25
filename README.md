# ⚡ FP Nexus

Приложение для автоматизации FunPay на базе **Electron + Python FastAPI**.

---

## 📁 Структура проекта

```
FPNexus/
├── backend/
│   ├── main.py            ← FastAPI-сервер (бизнес-логика)
│   ├── requirements.txt   ← Python-зависимости
│   ├── FunPayAPI/         ← Библиотека FunPay API
│   ├── config/
│   │   └── settings.json  ← Настройки (создаётся автоматически)
│   └── logs/
│       └── backend.log    ← Лог Python-бэкенда
│
├── electron/
│   ├── main.js            ← Electron main process
│   ├── preload.js         ← Context bridge (IPC)
│   └── src/
│       └── index.html     ← UI приложения
│
├── package.json           ← npm/Electron конфиг
│
│   === .bat ФАЙЛЫ ===
├── start.bat              ← 🚀 Запуск приложения
├── diagnose.bat           ← 🔍 Полная диагностика
├── install_deps.bat       ← 📦 Установка зависимостей
└── backend_only.bat       ← 🐍 Только бэкенд (для отладки)
```

---

## 🚀 Быстрый старт

### Требования
- **Python 3.10+** — [python.org](https://www.python.org/downloads/)  
  ⚠️ При установке обязательно отметь «Add Python to PATH»
- **Node.js 18+** — [nodejs.org](https://nodejs.org/)

### Установка и запуск

1. Запусти `diagnose.bat` → убедись что всё OK
2. Запусти `install_deps.bat` → установит все зависимости
3. Запусти `start.bat` → откроет приложение

---

## 🔧 .bat файлы

| Файл | Что делает |
|------|-----------|
| `start.bat` | Проверяет окружение и запускает Electron-приложение |
| `diagnose.bat` | **Полная диагностика**: Python, Node, зависимости, файлы, логи |
| `install_deps.bat` | Устанавливает pip и npm зависимости |
| `backend_only.bat` | Запускает только Python бэкенд (удобно для отладки API) |

### Что показывает `diagnose.bat`
- ✅/❌ Python и его версия
- ✅/❌ pip
- ✅/❌ Каждая Python-зависимость по отдельности
- ✅/❌ Node.js и npm
- ✅/❌ Каждый файл проекта
- ✅/❌ Electron в node_modules
- ✅/❌ Конфиг и наличие golden_key
- ✅/❌ Запущен ли backend прямо сейчас
- 📋 Последние 20 строк из backend.log

---

## ⚙️ Настройка

1. Открой приложение → «Настройки»
2. Вставь `golden_key` из куки FunPay
3. Нажми «Сохранить»
4. Перейди на Дашборд → «Запустить»

### Как найти golden_key
1. Зайди на [funpay.com](https://funpay.com) (будь авторизован)
2. F12 → Application → Cookies → funpay.com
3. Скопируй значение куки `golden_key`

---

## 🤖 Возможности

| Функция | Описание |
|---------|---------|
| **Авто-ответы** | Отвечает на сообщения по ключевым словам |
| **Авто-поднятие** | Поднимает лоты по расписанию |
| **Авто-отзыв** | Ставит отзыв после закрытия заказа |
| **Приветствие** | Приветствует новых пользователей |
| **Живые логи** | Все события в реальном времени через WebSocket |
| **Трей** | Работает в фоне, иконка в трее |

---

## 🛠 Сборка .exe (установщик)

```bash
npm run build
# → dist/FP Nexus Setup 1.0.0.exe
```

---

## 🐍 API документация (backend)

Запусти `backend_only.bat` и открой:
- **Swagger UI:** http://127.0.0.1:8765/docs
- **ReDoc:** http://127.0.0.1:8765/redoc

---

## ✅ Исправленные ошибки из FunPayPulse

- `'Account' object has no attribute 'total_balance'` — `account.get()` теперь вызывается до обращения к любым атрибутам аккаунта
- Защита от `None` для всех полей (`active_sales`, `total_balance`, `currency`)
- Thread-safe логи и статистика
- Корректная обработка `UnauthorizedError` с понятным сообщением
