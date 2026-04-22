"""
FunPay Bot — FastAPI Backend
Запускается автоматически из Electron или вручную через start.bat
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import threading
import time

try:
    from updater import Updater, get_local_version
    UPDATER_AVAILABLE = True
except ImportError:
    UPDATER_AVAILABLE = False
    def get_local_version(): return '0.0.0'

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

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
logger = logging.getLogger("FunPayBot")

# ─── Config ─────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config" / "settings.json"
CONFIG_PATH.parent.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "golden_key": "",
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "auto_response": {"enabled": False, "triggers": []},
    "auto_raise": {"enabled": False, "interval_minutes": 60, "categories": []},
    "auto_review": {"enabled": False, "text": "Спасибо за покупку!", "rating": 5},
    "greeting": {"enabled": False, "text": "Привет! Чем могу помочь?", "cooldown_hours": 24},
    "update_server": {
        "url": "",
        "token": "",
        "auto_check": True,
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
            return _deep_merge(DEFAULT_CONFIG, data)
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(data: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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
            self.start_time = None

    def to_dict(self) -> dict:
        uptime = "—"
        if self.start_time:
            secs = int(time.time() - self.start_time)
            h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
            uptime = f"{h:02d}:{m:02d}:{s:02d}"
        return {
            "messages_sent": self.messages_sent,
            "messages_received": self.messages_received,
            "orders_processed": self.orders_processed,
            "lots_raised": self.lots_raised,
            "reviews_sent": self.reviews_sent,
            "uptime": uptime,
        }


# ─── Bot Core ────────────────────────────────────────────────────────────────
class FunPayBot:
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
            self.account = FunPayAPI.Account(
                golden_key=cfg["golden_key"],
                user_agent=cfg.get("user_agent") or None,
            )
            self.account.get()
            self.log.add("info", "client",
                f"Аккаунт подключён: {self.account.username} | "
                f"Баланс: {self.account.total_balance} {self.account.currency.value if self.account.currency else ''} | "
                f"Продаж: {self.account.active_sales or 0} | "
                f"Покупок: {self.account.active_purchases or 0}")
            return True, f"Подключён как {self.account.username}"
        except Exception as e:
            self.account = None
            self.log.add("error", "client", f"Ошибка подключения: {e}")
            return False, str(e)

    def start(self) -> tuple[bool, str]:
        if self._running:
            return False, "Бот уже запущен"
        cfg = load_config()
        if not cfg.get("golden_key"):
            return False, "Укажите golden_key в настройках"
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True, "Бот запускается..."

    def stop(self):
        self._running = False
        self._status = "stopped"
        # FIX: мгновенно будим raise_loop через Event
        self._raise_stop.set()
        # Останавливаем поток обновления аккаунта
        if hasattr(self, "_account_refresh_stop"):
            self._account_refresh_stop.set()
        self.stats.reset()
        self._next_raise_at = None
        self.log.add("info", "system", "Бот остановлен")

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

            bal = self.account.total_balance if self.account.total_balance is not None else 0
            self.log.add("info", "client",
                f"✓ Авторизован как {self.account.username} | "
                f"Баланс: {bal} | Продажи: {self.account.active_sales or 0}")

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
            preview = (msg.text or "[изображение]")[:80]
            self.log.add("info", "chat", f"[{msg.chat_name}] {msg.author}: {preview}")

            if cfg.get("auto_response", {}).get("enabled"):
                self._auto_response(msg, cfg)
            if cfg.get("greeting", {}).get("enabled"):
                self._greeting(msg, cfg)

        elif isinstance(event, NewOrderEvent):
            order = event.order
            self.stats.inc("orders_processed")
            self.log.add("info", "order",
                f"🛒 Новый заказ #{order.id} от {order.buyer_username} — {order.price} {order.currency}")
            # FIX: уведомление для фронтенда (отдельная категория для фильтра)
            self.log.add("info", "new_order",
                f'{{"id":"{order.id}","buyer":"{order.buyer_username}","price":{order.price}}}')

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
        for trigger in triggers:
            for kw in trigger.get("keywords", []):
                if kw.lower() in text_lower:
                    resp = trigger.get("response", "")
                    if not resp:
                        continue
                    try:
                        self.account.send_message(
                            chat_id=msg.chat_id,
                            text=resp,
                            chat_name=msg.chat_name,
                            interlocutor_id=msg.author_id,
                        )
                        self.stats.inc("messages_sent")
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
        except Exception as e:
            self.log.add("error", "auto_review", f"Ошибка отзыва: {e}")

    def _raise_loop(self):
        """FIX: используем threading.Event вместо time.sleep — мгновенная остановка."""
        while self._running and not self._raise_stop.is_set():
            try:
                cfg = load_config()
                interval_secs = (cfg.get("auto_raise", {}).get("interval_minutes") or 60) * 60
                categories = cfg.get("auto_raise", {}).get("categories") or []

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
            u = Updater(url.strip(), token.strip())
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
        if not self._updater:
            return {"error": "Сервер обновлений не настроен", "has_update": False}
        has_upd, meta = self._updater.check_update()
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

        def on_progress(done, total):
            pct = int(done / total * 100) if total else 0
            self._update_progress = {"status": "downloading", "pct": pct, "done": done, "total": total}

        ok, msg = self._updater.download_and_apply(progress_cb=on_progress)
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
        return {
            "configured": bool(ucfg.get("url") and ucfg.get("token")),
            "server_url": ucfg.get("url", ""),
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
bot = FunPayBot()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # FIX: asyncio.get_running_loop() вместо устаревшего get_event_loop()
    bot.log.set_loop(asyncio.get_running_loop())
    bot.log.add("info", "system", "FastAPI backend запущен на порту 8765")
    yield
    bot.stop()

app = FastAPI(title="FunPay Bot API", lifespan=lifespan)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
