"""
FP Nexus — FastAPI Backend
Запускается автоматически из Electron или вручную через start.bat
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Принудительно UTF-8 для stdout/stderr — иначе на Windows
# любой emoji или ✓ в логах валит cp1251 кодек.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import asyncio
import threading
import time

try:
    from updater import Updater, get_local_version
    UPDATER_AVAILABLE = True
except ImportError:
    UPDATER_AVAILABLE = False
    def get_local_version(): return '0.0.0'

try:
    from plugin_system import PluginManager
    PLUGINS_AVAILABLE = True
except ImportError:
    PLUGINS_AVAILABLE = False
    PluginManager = None  # type: ignore

import json
import logging
import base64
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

# ─── Key protection ───────────────────────────────────────────────────────────
# golden_key хранится в Electron safeStorage (Windows DPAPI / macOS Keychain)
# и передаётся в бэкенд через переменную окружения FUNPAY_GOLDEN_KEY.
# В settings.json ключ НЕ сохраняется — это намеренно.

def get_secure_golden_key() -> str:
    """Получает golden_key из env (установлен Electron при запуске)."""
    return os.environ.get("FUNPAY_GOLDEN_KEY", "").strip()

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── Logging ────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "backend.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("FPNexus")

# ─── Config ─────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config" / "settings.json"
CONFIG_PATH.parent.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "golden_key": "",
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "auto_response": {"enabled": False, "triggers": []},
    "auto_raise": {
        "enabled": False,
        "interval_minutes": 60,
        "categories": [],
        "schedule_enabled": False,
        "schedule_from": "09:00",
        "schedule_to": "23:00",
    },
    "auto_review": {"enabled": False, "text": "Спасибо за покупку!", "rating": 5},
    "greeting": {"enabled": False, "text": "Привет! Чем могу помочь?", "cooldown_hours": 24},
    "blacklist": {"enabled": False, "user_ids": [], "usernames": []},
    "telegram_notify": {"enabled": False, "bot_token": "", "chat_id": ""},
    "backup": {"enabled": True, "keep_last": 5},
    "update_server": {
        "url": "http://funpaybot.duckdns.org:9000",
        "token": "",
        "auto_check": True,
    },
    "plugins": {
        "enabled": [],
        "config": {},
    },
}

def _deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивно мержит override в base, не теряя вложенные ключи."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            merged = _deep_merge(DEFAULT_CONFIG, data)
            # golden_key читается из env (Electron safeStorage), не из файла
            merged["golden_key"] = get_secure_golden_key()
            return merged
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(data: dict):
    save_data = dict(data)
    # НЕ сохраняем golden_key в файл — он хранится в Electron safeStorage
    save_data.pop("golden_key", None)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)

# ─── Backup ──────────────────────────────────────────────────────────────────
BACKUP_DIR = Path(__file__).parent / "config" / "backups"

def backup_config():
    """Создаёт бэкап settings.json с датой в имени."""
    if not CONFIG_PATH.exists():
        return
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = BACKUP_DIR / f"settings_{ts}.json"
        shutil.copy2(CONFIG_PATH, dst)
        # Удаляем старые бэкапы
        cfg = load_config()
        keep = cfg.get("backup", {}).get("keep_last", 5)
        backups = sorted(BACKUP_DIR.glob("settings_*.json"))
        for old in backups[:-keep]:
            old.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Backup failed: {e}")

def list_backups() -> list:
    if not BACKUP_DIR.exists():
        return []
    return sorted([f.name for f in BACKUP_DIR.glob("settings_*.json")], reverse=True)

def restore_backup(filename: str) -> bool:
    src = BACKUP_DIR / filename
    if not src.exists():
        return False
    shutil.copy2(src, CONFIG_PATH)
    return True


# ─── Telegram notify ──────────────────────────────────────────────────────────
async def tg_notify(text: str, cfg: dict):
    """Отправляет уведомление в Telegram."""
    tg_cfg = cfg.get("telegram_notify", {})
    if not tg_cfg.get("enabled"):
        return
    token   = tg_cfg.get("bot_token", "").strip()
    chat_id = tg_cfg.get("chat_id", "").strip()
    if not token or not chat_id:
        return
    try:
        import urllib.request
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
        req  = urllib.request.Request(url, data=data,
               headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.debug(f"TG notify error: {e}")


# ─── Schedule check ───────────────────────────────────────────────────────────
def is_raise_scheduled(cfg: dict) -> bool:
    """Проверяет разрешено ли поднятие по расписанию прямо сейчас."""
    ar = cfg.get("auto_raise", {})
    if not ar.get("schedule_enabled"):
        return True  # расписание выключено — всегда разрешено
    try:
        now_str  = datetime.now().strftime("%H:%M")
        from_str = ar.get("schedule_from", "00:00")
        to_str   = ar.get("schedule_to",   "23:59")
        return from_str <= now_str <= to_str
    except Exception:
        return True


# ─── Event Log ──────────────────────────────────────────────────────────────
class EventLog:
    def __init__(self, max_size: int = 1000):
        self._entries: List[dict] = []
        self._max = max_size
        self._lock = threading.Lock()
        self._subscribers: List[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def add(self, level: str, category: str, message: str):
        entry = {
            "id": int(time.time() * 1000),
            "time": datetime.now().strftime("%H:%M:%S"),
            "datetime": datetime.now().isoformat(),
            "level": level,
            "category": category,
            "message": message,
        }
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._max:
                self._entries.pop(0)
        loop = self._loop
        for q in list(self._subscribers):
            if loop and loop.is_running():
                loop.call_soon_threadsafe(self._put_nowait, q, entry)
            else:
                try:
                    q.put_nowait(entry)
                except Exception:
                    pass

    @staticmethod
    def _put_nowait(q: asyncio.Queue, entry: dict):
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            pass

    def get_all(self, category: Optional[str] = None) -> List[dict]:
        with self._lock:
            if category and category != "all":
                return [e for e in self._entries if e["category"] == category or e["level"] == category]
            return list(self._entries)

    def clear(self):
        with self._lock:
            self._entries.clear()

    def subscribe(self, q: asyncio.Queue):
        self._subscribers.append(q)

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)


# ─── Stats ───────────────────────────────────────────────────────────────────
class BotStats:
    def __init__(self):
        self.messages_sent = 0
        self.messages_received = 0
        self.orders_processed = 0
        self.lots_raised = 0
        self.reviews_sent = 0
        self.sales_total: float = 0.0   # сумма продаж за сессию
        self.sales_history: list = []   # последние 50 продаж
        self.start_time: Optional[float] = None
        self._lock = threading.Lock()

    def inc(self, field: str, by: int = 1):
        with self._lock:
            setattr(self, field, getattr(self, field) + by)

    def reset(self):
        with self._lock:
            self.messages_sent = 0
            self.messages_received = 0
            self.orders_processed = 0
            self.lots_raised = 0
            self.reviews_sent = 0
            self.sales_total = 0.0
            self.sales_history = []
            self.start_time = None

    def add_sale(self, order_id: str, buyer: str, price: float, title: str = ""):
        with self._lock:
            self.sales_total += price
            self.sales_history.append({
                "order_id": order_id,
                "buyer": buyer,
                "price": price,
                "title": title,
                "time": time.time(),
            })
            if len(self.sales_history) > 500:
                self.sales_history = self.sales_history[-500:]

    def earnings_summary(self) -> dict:
        now = time.time()
        with self._lock:
            today = sum(s["price"] for s in self.sales_history if s["time"] >= now - 86400)
            week  = sum(s["price"] for s in self.sales_history if s["time"] >= now - 86400 * 7)
            total = self.sales_total
            recent = list(reversed(self.sales_history[-10:]))
        return {
            "today": round(today, 2),
            "week":  round(week, 2),
            "total": round(total, 2),
            "recent": recent,
        }

    def to_dict(self) -> dict:
        uptime = "—"
        if self.start_time:
            secs = int(time.time() - self.start_time)
            h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
            uptime = f"{h:02d}:{m:02d}:{s:02d}"
        earnings = self.earnings_summary()
        return {
            "messages_sent":     self.messages_sent,
            "messages_received": self.messages_received,
            "orders_processed":  self.orders_processed,
            "lots_raised":       self.lots_raised,
            "reviews_sent":      self.reviews_sent,
            "sales_total":       round(self.sales_total, 2),
            "sales_history":     self.sales_history[-10:],
            "earnings_today":    earnings["today"],
            "earnings_week":     earnings["week"],
            "uptime":            uptime,
        }


# ─── Bot Core ────────────────────────────────────────────────────────────────
class FPNexus:
    def __init__(self):
        self.log = EventLog()
        self.stats = BotStats()
        self.account = None
        self.runner = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # FIX: используем Event вместо time.sleep для мгновенной остановки raise_loop
        self._raise_stop = threading.Event()
        self._raise_thread: Optional[threading.Thread] = None
        self._status = "stopped"
        self._old_users: Dict[int, float] = {}
        # Таймер следующего поднятия
        self._next_raise_at: Optional[float] = None
        # Updater state
        self._updater: Optional[Any] = None
        self._update_meta: dict = {}
        self._update_progress: dict = {}
        self._update_available: bool = False
        # Кулдаун авто-ответов: chat_id → timestamp последнего ответа
        self._response_cooldowns: Dict[int, float] = {}
        self._RESPONSE_COOLDOWN = 60  # секунд между авто-ответами в одном чате

        # Плагины
        self.plugins: Optional[Any] = None
        if PLUGINS_AVAILABLE and PluginManager is not None:
            try:
                plugins_dir = Path(__file__).parent / "plugins_data"
                plugins_dir.mkdir(parents=True, exist_ok=True)
                self.plugins = PluginManager(
                    plugins_dir=plugins_dir,
                    get_config=load_config,
                    save_config=save_config,
                    send_message_fn=self._plugin_send_message,
                    log_event=self.log.add,
                )
                # Грузим то, что включено в конфиге, прямо на старте.
                self.plugins.load_all_enabled()
            except Exception as e:
                logger.exception("PluginManager init failed: %s", e)
                self.log.add("error", "plugins",
                             f"PluginManager не инициализирован: {e}")
                self.plugins = None

    def _plugin_send_message(self, chat_id: int, text: str,
                             chat_name: Optional[str] = None,
                             interlocutor_id: Optional[int] = None,
                             _from_plugin: Optional[str] = None) -> bool:
        """Адаптер для PluginContext.send_message."""
        if not self.account or not self.account.is_initiated:
            self.log.add("warning", f"plugin:{_from_plugin or '?'}",
                         "send_message пока недоступен — бот не подключён")
            return False
        try:
            self.account.send_message(
                chat_id=chat_id, text=text,
                chat_name=chat_name,
                interlocutor_id=interlocutor_id,
            )
            self.stats.inc("messages_sent")
            return True
        except Exception as e:
            self.log.add("error", f"plugin:{_from_plugin or '?'}",
                         f"send_message упал: {e}")
            return False

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_running(self) -> bool:
        return self._running

    def get_account_info(self) -> dict:
        if not self.account or not self.account.is_initiated:
            return {}
        try:
            # Совместимость с разными версиями FunPayAPI
            # total_balance — актуальное название; balance — старое
            balance = (
                getattr(self.account, "total_balance", None)
                or getattr(self.account, "balance", None)
                or 0
            )
            currency_raw = getattr(self.account, "currency", None)
            if currency_raw is not None:
                # Может быть enum с .value или просто строка
                currency = getattr(currency_raw, "value", str(currency_raw))
            else:
                currency = "₽"
            return {
                "id": getattr(self.account, "id", None),
                "username": getattr(self.account, "username", ""),
                "balance": balance if balance is not None else 0,
                "currency": currency,
                "active_sales": getattr(self.account, "active_sales", 0) or 0,
                "active_purchases": getattr(self.account, "active_purchases", 0) or 0,
            }
        except Exception as e:
            logger.error(f"get_account_info error: {e}")
            return {}

    def get_raise_status(self) -> dict:
        """Возвращает статус авто-поднятия включая таймер до следующего."""
        next_in = None
        if self._next_raise_at is not None:
            remaining = max(0, self._next_raise_at - time.time())
            next_in = int(remaining)
        return {
            "running": self._raise_thread is not None and self._raise_thread.is_alive(),
            "next_raise_in": next_in,
        }

    def connect(self) -> tuple[bool, str]:
        cfg = load_config()
        if not cfg.get("golden_key"):
            return False, "Укажите golden_key в настройках"
        try:
            import FunPayAPI
            account = FunPayAPI.Account(
                golden_key=cfg["golden_key"],
                user_agent=cfg.get("user_agent") or None,
            )
            account.get()

            if not account.is_initiated:
                raise RuntimeError("Аккаунт не инициализирован — проверьте golden_key")

            bal = getattr(account, "total_balance", None) or getattr(account, "balance", 0) or 0
            currency_raw = getattr(account, "currency", None)
            currency = getattr(currency_raw, "value", str(currency_raw)) if currency_raw else ""

            self.account = account
            self.log.add("info", "client",
                f"Аккаунт подключён: {account.username} | "
                f"Баланс: {bal} {currency} | "
                f"Продаж: {account.active_sales or 0} | "
                f"Покупок: {account.active_purchases or 0}")
            return True, f"Подключён как {account.username}"
        except Exception as e:
            self.account = None
            err = str(e)
            if "total_balance" in err or "has no attribute" in err:
                err = "Ошибка подключения к FunPay. Проверьте golden_key."
            self.log.add("error", "client", f"Ошибка подключения: {err}")
            return False, err

    def start(self) -> tuple[bool, str]:
        if self._running:
            return False, "Бот уже запущен"
        cfg = load_config()
        if not cfg.get("golden_key"):
            return False, "Укажите golden_key в настройках"
        self._thread = threading.Thread(target=self._loop_with_restart, daemon=True)
        self._thread.start()
        return True, "Бот запускается..."

    def stop(self):
        self._running = False
        self._status = "stopped"
        # Сбрасываем runner — освобождаем аккаунт для повторного подключения
        self.runner = None
        self.account = None
        # Мгновенно будим raise_loop через Event
        self._raise_stop.set()
        if hasattr(self, "_account_refresh_stop"):
            self._account_refresh_stop.set()
        self.stats.reset()
        self._next_raise_at = None
        self.log.add("info", "system", "Бот остановлен")

    def _loop_with_restart(self):
        """Обёртка над _loop с авто-рестартом при сетевых ошибках."""
        RESTART_DELAY = 30  # секунд до перезапуска
        MAX_RESTARTS  = 10  # максимум перезапусков подряд
        restarts = 0

        while restarts < MAX_RESTARTS:
            self._loop()
            # Если бот остановлен вручную — не перезапускаем
            if self._status == "stopped":
                break
            # Если ошибка авторизации — не перезапускаем
            if self._status == "error" and restarts == 0:
                # Проверяем тип ошибки через лог
                recent = self.log.get_all("client")
                last_errors = [e for e in recent[-5:] if e["level"] == "error"]
                if any("golden_key" in e["message"] or "авторизац" in e["message"].lower()
                       for e in last_errors):
                    self.log.add("error", "client", "Авторизация провалена — авто-рестарт отключён")
                    break

            restarts += 1
            self.log.add("warning", "client",
                f"Бот упал. Перезапуск через {RESTART_DELAY}с... (попытка {restarts}/{MAX_RESTARTS})")
            # Сбрасываем runner и аккаунт перед перезапуском
            self.runner = None
            self.account = None
            # Ждём перед рестартом (прерываемое ожидание)
            for _ in range(RESTART_DELAY):
                if self._status == "stopped":
                    return
                time.sleep(1)

        if restarts >= MAX_RESTARTS:
            self.log.add("error", "client",
                f"Превышено максимальное количество перезапусков ({MAX_RESTARTS}). Остановлен.")
            self._status = "error"

    def _loop(self):
        self._status = "connecting"
        self.log.add("info", "client", "Подключение...")
        try:
            import FunPayAPI
            cfg = load_config()

            if not (self.account and self.account.is_initiated):
                self.account = FunPayAPI.Account(
                    golden_key=cfg["golden_key"],
                    user_agent=cfg.get("user_agent") or None,
                )
                self.log.add("info", "client", "Подключение к FunPay...")
                self.account.get()
            else:
                self.log.add("info", "client", "Используем уже подключённый аккаунт...")

            if not self.account.is_initiated:
                raise RuntimeError("Аккаунт не инициализирован — проверьте golden_key")

            bal = getattr(self.account, "total_balance", None) or getattr(self.account, "balance", 0) or 0
            self.log.add("info", "client",
                f"✓ Авторизован как {self.account.username} | "
                f"Баланс: {bal} | Продажи: {self.account.active_sales or 0}")

            # Пересоздаём Runner — старый должен быть None
            self.runner = None
            time.sleep(1)  # Даём FunPayAPI время освободить аккаунт
            self.runner = FunPayAPI.Runner(self.account)
            self._running = True
            self._status = "running"
            self.stats.start_time = time.time()

            if cfg.get("auto_raise", {}).get("enabled"):
                self._raise_stop.clear()
                self._raise_thread = threading.Thread(target=self._raise_loop, daemon=True)
                self._raise_thread.start()

            # Запускаем фоновый поток обновления данных аккаунта
            self._account_refresh_stop = threading.Event()
            refresh_thread = threading.Thread(target=self._account_refresh_loop, daemon=True)
            refresh_thread.start()

            for event in self.runner.listen(requests_delay=6.0):
                if not self._running:
                    break
                try:
                    self._handle_event(event, cfg)
                    cfg = load_config()
                except Exception as e:
                    logger.error(f"Event handler error: {e}", exc_info=True)
                    self.log.add("error", "handler", f"Ошибка обработки: {e}")

        except Exception as e:
            msg = str(e)
            if "Unauthorized" in type(e).__name__ or "401" in msg:
                self.log.add("error", "client", "Ошибка авторизации: неверный golden_key")
            else:
                self.log.add("error", "client", f"Ошибка подключения: {msg}")
            logger.error(f"Bot loop error: {e}", exc_info=True)
            self._status = "error"
        finally:
            self._running = False

    def _handle_event(self, event, cfg: dict):
        import FunPayAPI
        from FunPayAPI.updater.events import (
            InitialChatEvent, NewMessageEvent,
            NewOrderEvent, OrderStatusChangedEvent, InitialOrderEvent
        )
        from FunPayAPI.common.enums import OrderStatuses

        if isinstance(event, InitialChatEvent):
            self.log.add("debug", "runner", f"Чат инициализирован: {event.chat.name}")

        elif isinstance(event, NewMessageEvent):
            msg = event.message
            self.stats.inc("messages_received")
            if msg.author_id == self.account.id:
                return
            # Чёрный список
            bl = cfg.get("blacklist", {})
            if bl.get("enabled"):
                blocked_ids   = set(bl.get("user_ids", []))
                blocked_names = [n.lower() for n in bl.get("usernames", [])]
                if msg.author_id in blocked_ids:
                    self.log.add("debug", "blacklist", f"Игнор {msg.author} (id в ЧС)")
                    return
                if msg.author and msg.author.lower() in blocked_names:
                    self.log.add("debug", "blacklist", f"Игнор {msg.author} (username в ЧС)")
                    return
            preview = (msg.text or "[изображение]")[:80]
            self.log.add("info", "chat", f"[{msg.chat_name}] {msg.author}: {preview}")

            if cfg.get("auto_response", {}).get("enabled"):
                self._auto_response(msg, cfg)
            if cfg.get("greeting", {}).get("enabled"):
                self._greeting(msg, cfg)
            # Плагины — сначала on_message, ответы плагинов отправляем сразу
            if self.plugins is not None:
                replies = self.plugins.dispatch_message(msg)
                for reply in replies:
                    self._plugin_send_message(
                        chat_id=msg.chat_id,
                        text=reply,
                        chat_name=msg.chat_name,
                        interlocutor_id=msg.author_id,
                        _from_plugin="<reply>",
                    )

        elif isinstance(event, NewOrderEvent):
            order = event.order
            self.stats.inc("orders_processed")
            price = float(getattr(order, "price", 0) or 0)
            currency = str(getattr(order, "currency", "₽") or "₽")
            self.stats.sales_total += price
            # Добавляем в историю продаж
            sale = {
                "id": order.id,
                "buyer": order.buyer_username or "?",
                "price": price,
                "currency": currency,
                "time": datetime.now().strftime("%H:%M"),
            }
            with self.stats._lock:
                self.stats.sales_history.append(sale)
                if len(self.stats.sales_history) > 50:
                    self.stats.sales_history = self.stats.sales_history[-50:]
            self.log.add("info", "order",
                f"🛒 Новый заказ #{order.id} от {order.buyer_username} — {price} {currency}")
            # Уведомление для фронтенда
            self.log.add("info", "new_order",
                f'{{"id":"{order.id}","buyer":"{order.buyer_username}","price":{price},"currency":"{currency}"}}')
            # Telegram уведомление о заказе
            asyncio.get_event_loop().call_soon_threadsafe(
                lambda: asyncio.ensure_future(
                    tg_notify(f'🛒 Новый заказ от {buyer}\n💰 {price} {currency}\n{title}', cfg)
                )
            )
            # Плагины — реакция на оплаченный заказ
            if self.plugins is not None:
                self.plugins.dispatch_order_paid(order)

        elif isinstance(event, OrderStatusChangedEvent):
            order = event.order
            self.log.add("info", "order", f"📦 Заказ #{order.id} → {order.status}")
            if cfg.get("auto_review", {}).get("enabled"):
                if order.status == OrderStatuses.CLOSED:
                    self._auto_review(order, cfg)

        elif isinstance(event, InitialOrderEvent):
            self.log.add("debug", "runner", f"Заказ найден: #{event.order.id}")

    def _auto_response(self, msg, cfg):
        triggers = cfg.get("auto_response", {}).get("triggers", [])
        text_lower = (msg.text or "").lower()
        chat_id = msg.chat_id

        # Проверяем кулдаун — не спамим в один чат
        now = time.time()
        last_response = self._response_cooldowns.get(chat_id, 0)
        if now - last_response < self._RESPONSE_COOLDOWN:
            remaining = int(self._RESPONSE_COOLDOWN - (now - last_response))
            self.log.add("debug", "auto_response",
                f"Кулдаун чата {msg.chat_name}: ещё {remaining}с")
            return

        for trigger in triggers:
            for kw in trigger.get("keywords", []):
                if kw.lower() in text_lower:
                    resp = trigger.get("response", "")
                    if not resp:
                        continue
                    try:
                        self.account.send_message(
                            chat_id=chat_id,
                            text=resp,
                            chat_name=msg.chat_name,
                            interlocutor_id=msg.author_id,
                        )
                        self.stats.inc("messages_sent")
                        self._response_cooldowns[chat_id] = time.time()
                        # Чистим старые записи (старше 1 часа)
                        cutoff = time.time() - 3600
                        self._response_cooldowns = {
                            k: v for k, v in self._response_cooldowns.items() if v > cutoff
                        }
                        self.log.add("info", "auto_response",
                            f"↩ Авто-ответ → {msg.chat_name} (триггер: «{kw}»)")
                    except Exception as e:
                        self.log.add("error", "auto_response", f"Ошибка авто-ответа: {e}")
                    return

    def _greeting(self, msg, cfg):
        chat_id = msg.chat_id
        cooldown = (cfg.get("greeting", {}).get("cooldown_hours") or 24) * 3600
        now = time.time()
        if now - self._old_users.get(chat_id, 0) < cooldown:
            return
        self._old_users[chat_id] = now
        text = cfg.get("greeting", {}).get("text", "")
        if not text:
            return
        try:
            self.account.send_message(
                chat_id=chat_id, text=text,
                chat_name=msg.chat_name,
                interlocutor_id=msg.author_id,
            )
            self.stats.inc("messages_sent")
            self.log.add("info", "greeting", f"👋 Приветствие → {msg.chat_name}")
        except Exception as e:
            logger.debug(f"Greeting error: {e}")

    def _auto_review(self, order, cfg):
        rv_cfg = cfg.get("auto_review", {})
        text = rv_cfg.get("text", "Спасибо за покупку!")
        rating = int(rv_cfg.get("rating", 5))
        try:
            self.account.send_review(order.id, text, rating)
            self.stats.inc("reviews_sent")
            self.log.add("info", "auto_review", f"⭐ Отзыв отправлен для #{order.id}")
            self.log.add("info", "new_review",
                f'{{"order_id":"{order.id}","rating":{rating}}}')
        except Exception as e:
            self.log.add("error", "auto_review", f"Ошибка отзыва: {e}")

    def _raise_loop(self):
        """FIX: используем threading.Event вместо time.sleep — мгновенная остановка."""
        while self._running and not self._raise_stop.is_set():
            try:
                cfg = load_config()
                interval_secs = (cfg.get("auto_raise", {}).get("interval_minutes") or 60) * 60
                categories = cfg.get("auto_raise", {}).get("categories") or []

                # Проверяем расписание
                if not is_raise_scheduled(cfg):
                    h_from = cfg.get("auto_raise", {}).get("schedule_from", "?")
                    h_to   = cfg.get("auto_raise", {}).get("schedule_to", "?")
                    self.log.add("info", "auto_raise",
                        f"⏰ Вне расписания ({h_from}–{h_to}), пропускаю")
                else:
                    for cat_id in categories:
                        if self._raise_stop.is_set():
                            return
                        try:
                            self.account.raise_lots(int(cat_id))
                            self.stats.inc("lots_raised")
                            self.log.add("info", "auto_raise", f"⬆ Лоты категории {cat_id} подняты")
                        except Exception as e:
                            self.log.add("warning", "auto_raise", f"Не удалось поднять {cat_id}: {e}")
                        # Пауза между категориями — тоже через Event
                        self._raise_stop.wait(3)

                # Устанавливаем таймер и ждём
                self._next_raise_at = time.time() + interval_secs
                self.log.add("info", "auto_raise",
                    f"Следующее поднятие через {interval_secs // 60} мин.")
                self._raise_stop.wait(interval_secs)

            except Exception as e:
                logger.error(f"Raise loop error: {e}")
                self._raise_stop.wait(60)

        self._next_raise_at = None

    def _account_refresh_loop(self):
        """Обновляет данные аккаунта (баланс, продажи) каждые 5 минут."""
        INTERVAL = 300  # секунд
        stop = getattr(self, "_account_refresh_stop", threading.Event())
        while self._running and not stop.is_set():
            stop.wait(INTERVAL)
            if not self._running or stop.is_set():
                break
            try:
                self.account.get()
                info = self.get_account_info()
                self.log.add("info", "account",
                    f"Обновлены данные: баланс {info.get('balance')} {info.get('currency')} | "
                    f"продажи: {info.get('active_sales')}")
            except Exception as e:
                logger.debug(f"Account refresh error: {e}")

    def raise_manual(self, category_id: int) -> tuple[bool, str]:
        if not self.account or not self.account.is_initiated:
            return False, "Бот не подключён"
        try:
            self.account.raise_lots(category_id)
            self.stats.inc("lots_raised")
            self.log.add("info", "manual", f"⬆ Лоты категории {category_id} подняты вручную")
            return True, f"Категория {category_id} поднята"
        except Exception as e:
            self.log.add("error", "manual", f"Ошибка поднятия {category_id}: {e}")
            return False, str(e)

    def send_message(self, chat_id: int, text: str, chat_name: str = None) -> tuple[bool, str]:
        if not self.account or not self.account.is_initiated:
            return False, "Бот не подключён"
        try:
            self.account.send_message(chat_id=chat_id, text=text, chat_name=chat_name)
            self.stats.inc("messages_sent")
            self.log.add("info", "chat", f"✉ Сообщение отправлено в чат {chat_id}")
            return True, "Отправлено"
        except Exception as e:
            self.log.add("error", "chat", f"Ошибка отправки: {e}")
            return False, str(e)

    def refresh_account(self) -> tuple[bool, str]:
        if not self.account:
            return False, "Аккаунт не инициализирован"
        try:
            self.account.get()
            return True, "Данные аккаунта обновлены"
        except Exception as e:
            return False, str(e)

    # ─── Updater methods ────────────────────────────────────────────────────────

    def _make_updater(self) -> Optional[Any]:
        if not UPDATER_AVAILABLE:
            return None
        cfg = load_config()
        ucfg = cfg.get("update_server", {})
        url   = ucfg.get("url", "").strip()
        token = ucfg.get("token", "").strip()
        if not url or not token:
            return None
        return Updater(url, token, on_log=lambda m: self.log.add("info", "updater", m))

    def connect_update_server(self, url: str, token: str) -> tuple[bool, str]:
        """Проверяет подключение к серверу обновлений и сохраняет настройки."""
        if not UPDATER_AVAILABLE:
            return False, "Модуль updater не найден"
        try:
            u = Updater(url.strip(), token.strip(),
                        on_log=lambda m: self.log.add("info", "updater", m))
            ok, err = u.ping()
            if not ok:
                return False, f"Сервер недоступен: {err or 'нет ответа'}"
            # Проверяем токен
            has_upd, meta = u.check_update()
            if meta.get("error"):
                return False, f"Ошибка токена: {meta['error']}"
            # Сохраняем
            cfg = load_config()
            cfg["update_server"] = {"url": url.strip(), "token": token.strip(), "auto_check": True}
            save_config(cfg)
            self._updater = u
            self._update_meta = meta
            self._update_available = has_upd
            ver = meta.get("remote_version", "?")
            local = meta.get("local_version", "?")
            msg = f"Подключено к серверу обновлений. Удалённая: v{ver} | Локальная: v{local}"
            if has_upd:
                msg += f" | Доступно обновление!"
            self.log.add("info", "updater", msg)
            return True, msg
        except Exception as e:
            return False, str(e)

    def check_for_updates(self) -> dict:
        """Проверяет наличие обновлений."""
        if not self._updater:
            self._updater = self._make_updater()
        # Если апдейтер не настроен или старый токен не валиден — пробуем
        # перерегистрироваться на VPS прозрачно для пользователя.
        if not self._updater:
            try:
                _bootstrap_update_server()
            except Exception:
                pass
            self._updater = self._make_updater()
        if not self._updater:
            return {"error": "Сервер обновлений не настроен", "has_update": False}
        has_upd, meta = self._updater.check_update()
        # Если токен протух — re-bootstrap и повторяем один раз
        if meta.get("error") and ("ток" in meta["error"].lower() or "403" in meta["error"]):
            try:
                cfg = load_config()
                cfg.setdefault("update_server", {})["token"] = ""
                save_config(cfg)
                _bootstrap_update_server()
                self._updater = self._make_updater()
                if self._updater:
                    has_upd, meta = self._updater.check_update()
            except Exception:
                pass
        self._update_available = has_upd
        self._update_meta = meta
        if meta.get("error"):
            return {"error": meta["error"], "has_update": False}
        return {
            "has_update": has_upd,
            "local_version": meta.get("local_version"),
            "remote_version": meta.get("remote_version"),
            "changelog": meta.get("changelog", ""),
            "size": meta.get("size", 0),
        }

    def apply_update(self) -> dict:
        """Скачивает и применяет обновление."""
        if not self._updater:
            self._updater = self._make_updater()
        if not self._updater:
            return {"error": "Сервер обновлений не настроен"}
        self._update_progress = {"status": "downloading", "pct": 0}
        self.log.add("info", "updater", "Начинаю загрузку обновления...")

        def on_progress(stage, done, total):
            pct = int(done / total * 100) if total else 0
            self._update_progress = {
                "status": stage, "pct": pct, "done": done, "total": total
            }

        try:
            ok, msg = self._updater.download_and_apply(progress_cb=on_progress)
        except Exception as e:
            ok, msg = False, f"Сбой обновления: {e}"

        if ok:
            self._update_progress = {"status": "done", "pct": 100}
            self._update_available = False
        else:
            self._update_progress = {"status": "error", "message": msg}
        self.log.add("info" if ok else "error", "updater", msg)
        return {"ok": ok, "message": msg}

    def get_update_status(self) -> dict:
        cfg = load_config()
        ucfg = cfg.get("update_server", {})
        # URL и токен не светим во фронт — это внутренние детали инфраструктуры.
        return {
            "configured": bool(ucfg.get("url") and ucfg.get("token")),
            "has_update": self._update_available,
            "meta": self._update_meta,
            "progress": self._update_progress,
            "local_version": get_local_version(),
        }

    def get_categories(self) -> list:
        """Возвращает список категорий аккаунта для отображения в UI."""
        if not self.account or not self.account.is_initiated:
            return []
        try:
            return [{"id": c.id, "name": c.name} for c in self.account.categories]
        except Exception:
            return []


# ─── App ─────────────────────────────────────────────────────────────────────
bot = FPNexus()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # FIX: asyncio.get_running_loop() вместо устаревшего get_event_loop()
    bot.log.set_loop(asyncio.get_running_loop())
    bot.log.add("info", "system", "FastAPI backend запущен на порту 8765")
    yield
    bot.stop()

app = FastAPI(title="FP Nexus API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ─── Pydantic models ─────────────────────────────────────────────────────────
class SendMessageBody(BaseModel):
    chat_id: int
    text: str
    chat_name: Optional[str] = None

class RaiseBody(BaseModel):
    category_id: int

class ConfigPatch(BaseModel):
    data: Dict[str, Any]


# ─── Routes ──────────────────────────────────────────────────────────────────
@app.get("/api/status")
def get_status():
    return {
        "status": bot.status,
        "is_running": bot.is_running,
        "account": bot.get_account_info(),
        "stats": bot.stats.to_dict(),
        "raise_status": bot.get_raise_status(),
    }

@app.post("/api/start")
def start_bot():
    ok, msg = bot.start()
    return {"ok": ok, "message": msg}

@app.post("/api/stop")
def stop_bot():
    bot.stop()
    return {"ok": True, "message": "Бот остановлен"}

@app.post("/api/connect")
def connect_account():
    ok, msg = bot.connect()
    return {"ok": ok, "message": msg}

@app.post("/api/refresh")
def refresh_bot():
    ok, msg = bot.refresh_account()
    return {"ok": ok, "message": msg}

@app.get("/api/logs")
def get_logs(category: Optional[str] = None):
    return {"logs": bot.log.get_all(category)}

@app.post("/api/logs/clear")
def clear_logs():
    bot.log.clear()
    return {"ok": True}

@app.get("/api/config")
def get_config():
    cfg = load_config()
    # Никогда не отдаём golden_key целиком во фронтенд
    key = cfg.get("golden_key", "")
    cfg_out = dict(cfg)
    cfg_out["golden_key"] = ""
    cfg_out["has_key"] = bool(key)
    cfg_out["key_hint"] = (key[:4] + "****" + key[-4:]) if len(key) > 8 else ("*" * len(key))
    return cfg_out

@app.post("/api/config")
def set_config(body: ConfigPatch):
    cfg = load_config()
    # Бэкап перед изменением
    backup_config()
    for key, value in body.data.items():
        if "." in key:
            parts = key.split(".")
            d = cfg
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = value
        else:
            cfg[key] = value
    save_config(cfg)
    return {"ok": True, "message": "Настройки сохранены"}


@app.get("/api/backups")
def get_backups():
    return {"backups": list_backups()}


@app.post("/api/backups/restore")
def restore_backup_api(data: dict):
    filename = data.get("filename", "")
    if not filename or not restore_backup(filename):
        raise HTTPException(400, "Бэкап не найден")
    return {"ok": True, "message": f"Восстановлено из {filename}"}

@app.post("/api/raise")
def raise_lots(body: RaiseBody):
    ok, msg = bot.raise_manual(body.category_id)
    return {"ok": ok, "message": msg}

@app.post("/api/send_message")
def send_message(body: SendMessageBody):
    ok, msg = bot.send_message(body.chat_id, body.text, body.chat_name)
    return {"ok": ok, "message": msg}

@app.get("/api/categories")
def get_categories():
    return {"categories": bot.get_categories()}

@app.get("/api/ping")
def ping():
    return {"pong": True, "time": datetime.now().isoformat()}


@app.get("/api/key/status")
def key_status():
    """Проверяет есть ли golden_key в окружении."""
    key = get_secure_golden_key()
    return {"has_key": bool(key)}


@app.get("/api/earnings")
def get_earnings():
    return bot.stats.earnings_summary()


@app.post("/api/notify/test")
async def test_notify():
    cfg = load_config()
    await tg_notify("✅ FP Nexus — тестовое уведомление работает!", cfg)
    return {"ok": True, "message": "Уведомление отправлено"}


@app.post("/api/backups/create")
def create_backup_api():
    backup_config()
    return {"ok": True, "message": "Бэкап создан", "backups": list_backups()}


# ─── WebSocket for live logs ──────────────────────────────────────────────────
@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    bot.log.subscribe(q)
    # Отправляем существующие логи
    for entry in bot.log.get_all():
        try:
            await websocket.send_json(entry)
        except Exception:
            bot.log.unsubscribe(q)
            return
    try:
        while True:
            try:
                entry = await asyncio.wait_for(q.get(), timeout=30)
                await websocket.send_json(entry)
            except asyncio.TimeoutError:
                # Heartbeat ping чтобы держать соединение
                await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        bot.log.unsubscribe(q)


# ─── Update server routes ─────────────────────────────────────────────────────

class ConnectUpdateServerBody(BaseModel):
    url: str
    token: str

@app.post("/api/update/connect")
def update_connect(body: ConnectUpdateServerBody):
    ok, msg = bot.connect_update_server(body.url, body.token)
    return {"ok": ok, "message": msg}

@app.get("/api/update/status")
def update_status():
    return bot.get_update_status()

@app.post("/api/update/check")
def update_check():
    return bot.check_for_updates()

@app.post("/api/update/apply")
def update_apply():
    """Запускает применение обновления в фоновом потоке."""
    import threading
    t = threading.Thread(target=bot.apply_update, daemon=True)
    t.start()
    return {"ok": True, "message": "Обновление запущено..."}

@app.get("/api/update/progress")
def update_progress():
    return bot._update_progress or {"status": "idle"}


# ─── Plugin routes ────────────────────────────────────────────────────────────

class PluginIdBody(BaseModel):
    id: str

class PluginToggleBody(BaseModel):
    id: str
    enabled: bool

def _vps_session() -> tuple[str, str, dict]:
    """Возвращает (url, token, headers) для походов на сервер обновлений."""
    cfg = load_config()
    ucfg = cfg.get("update_server") or {}
    url = (ucfg.get("url") or "").rstrip("/")
    token = (ucfg.get("token") or "").strip()
    if not url or not token:
        raise HTTPException(400, "Сервер обновлений не настроен")
    return url, token, {"X-Token": token}

def _require_plugins() -> Any:
    if bot.plugins is None:
        raise HTTPException(503, "Подсистема плагинов недоступна")
    return bot.plugins

@app.get("/api/plugins/installed")
def plugins_installed():
    return {"plugins": _require_plugins().list_installed()}

@app.get("/api/plugins/store")
def plugins_store():
    """Список плагинов с VPS."""
    import requests
    url, _, headers = _vps_session()
    try:
        r = requests.get(f"{url}/plugins", headers=headers, timeout=15)
    except Exception as e:
        raise HTTPException(502, f"VPS недоступен: {e}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text[:200])
    return r.json()

@app.post("/api/plugins/install")
def plugins_install(body: PluginIdBody):
    import requests
    pm = _require_plugins()
    url, _, headers = _vps_session()
    try:
        r = requests.get(f"{url}/plugins/{body.id}/download",
                         headers=headers, timeout=60)
    except Exception as e:
        raise HTTPException(502, f"VPS недоступен: {e}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text[:200])
    try:
        meta = pm.install_zip(r.content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "plugin": meta}

@app.post("/api/plugins/uninstall")
def plugins_uninstall(body: PluginIdBody):
    pm = _require_plugins()
    try:
        pm.uninstall(body.id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}

@app.post("/api/plugins/toggle")
def plugins_toggle(body: PluginToggleBody):
    pm = _require_plugins()
    try:
        pm.set_enabled(body.id, body.enabled)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Не удалось загрузить плагин: {e}")
    return {"ok": True}

@app.get("/api/plugins/{plugin_id}/config")
def plugins_get_config(plugin_id: str):
    pm = _require_plugins()
    return {"config": pm.get_config(plugin_id)}

@app.post("/api/plugins/{plugin_id}/config")
def plugins_set_config(plugin_id: str, values: dict):
    pm = _require_plugins()
    try:
        pm.set_config(plugin_id, values)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}

@app.post("/api/plugins/reload")
def plugins_reload():
    pm = _require_plugins()
    pm.reload_all()
    return {"ok": True}


# ─── Bootstrap update server (zero-config) ────────────────────────────────────
# На первом запуске или если токен потерялся — автоматически
# получаем fp_-токен с VPS, чтобы обычный пользователь ничего не
# настраивал руками.
DEFAULT_UPDATE_URL = "http://funpaybot.duckdns.org:9000"

def _bootstrap_update_server() -> None:
    cfg = load_config()
    ucfg = cfg.get("update_server") or {}
    url = (ucfg.get("url") or "").strip() or DEFAULT_UPDATE_URL
    token = (ucfg.get("token") or "").strip()
    if token:
        # Уже настроено — ничего не делаем
        if (ucfg.get("url") or "").strip() != url:
            cfg["update_server"]["url"] = url
            save_config(cfg)
        return
    try:
        import requests
        r = requests.post(f"{url}/api/vps/auto_register", json={}, timeout=8)
        if r.status_code == 200:
            data = r.json()
            new_token = data.get("token")
            if new_token:
                cfg["update_server"] = {"url": url, "token": new_token, "auto_check": True}
                save_config(cfg)
                print(f"[bootstrap] Update server auto-configured ({url}). Token issued.")
                return
        print(f"[bootstrap] auto_register failed: HTTP {r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"[bootstrap] auto_register error: {e}")

try:
    _bootstrap_update_server()
except Exception as _e:
    print(f"[bootstrap] skipped: {_e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
