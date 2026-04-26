"""Multi-tenant FunPay keepalive worker.

Каждому fp_-токену соответствует один FunPay-аккаунт (golden_key), который
держится онлайн на VPS. Один общий keepalive-поток раз в N секунд дёргает
account.get() на всех живых сессиях, чтобы FunPay показывал юзера в онлайне
даже когда его ПК выключен.

Хранилище:
    accounts/<fp_token>.json — {"gk": "<encrypted>", "active": bool, ...}

Шифрование:
    Используется Fernet (AES-128-CBC + HMAC-SHA256). Ключ берётся из env
    FP_GOLDEN_KEY_AES (32-байтовый key в base64). Если env не задан —
    падаем при первой загрузке, чтобы не хранить golden_key в plaintext
    случайно.

В Phase 1 воркер только держит сессию онлайн и отдаёт минимальный статус
(username/balance/active_sales). Авто-ответ, плагины и прочая автоматизация
переедут на VPS в Phase 2.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

ACCOUNTS_DIR = Path("accounts")
ACCOUNTS_DIR.mkdir(exist_ok=True)

KEEPALIVE_INTERVAL = 90  # секунд между .get() на каждом аккаунте

_lock = threading.Lock()
# fp_token -> dict с FunPayAPI.Account и кэшем последних значений
_workers: dict[str, dict[str, Any]] = {}


# ─── Crypto ───────────────────────────────────────────────────────────────────
def _get_fernet():
    """Возвращает экземпляр Fernet из env-ключа. Бросает если ключ не задан."""
    from cryptography.fernet import Fernet  # type: ignore
    key = os.environ.get("FP_GOLDEN_KEY_AES", "").strip()
    if not key:
        raise RuntimeError(
            "FP_GOLDEN_KEY_AES не задан в окружении. Сгенерируй ключ:\n"
            "    python3 -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'\n"
            "и добавь в /etc/funpay-server.env, потом systemctl restart funpay-server."
        )
    return Fernet(key.encode())


def encrypt_gk(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_gk(enc: str) -> str:
    return _get_fernet().decrypt(enc.encode()).decode()


# ─── Storage ──────────────────────────────────────────────────────────────────
def _path(fp_token: str) -> Path:
    return ACCOUNTS_DIR / f"{fp_token}.json"


def store_account(fp_token: str, golden_key: str, user_agent: str = "") -> None:
    enc = encrypt_gk(golden_key)
    data = {
        "gk": enc,
        "user_agent": user_agent,
        "active": True,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    p = _path(fp_token)
    if p.exists():
        try:
            old = json.loads(p.read_text())
            data["created_at"] = old.get("created_at", data["created_at"])
        except Exception:
            pass
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_account_data(fp_token: str) -> Optional[dict]:
    p = _path(fp_token)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def delete_account(fp_token: str) -> None:
    stop_worker(fp_token)
    p = _path(fp_token)
    if p.exists():
        p.unlink()


def _set_active(fp_token: str, active: bool) -> None:
    d = load_account_data(fp_token) or {}
    d["active"] = active
    d["updated_at"] = time.time()
    _path(fp_token).write_text(json.dumps(d, ensure_ascii=False, indent=2))


# ─── Worker lifecycle ─────────────────────────────────────────────────────────
def _make_account(golden_key: str, user_agent: str = ""):
    import FunPayAPI  # type: ignore
    ua = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    return FunPayAPI.Account(golden_key, user_agent=ua)


def _refresh_state(fp_token: str, acc) -> dict[str, Any]:
    bal = (
        getattr(acc, "total_balance", None)
        or getattr(acc, "balance", None)
        or 0
    )
    return {
        "username": getattr(acc, "username", "") or "",
        "user_id": getattr(acc, "id", None),
        "balance": bal,
        "currency": str(getattr(acc, "currency", "") or ""),
        "active_sales": getattr(acc, "active_sales", 0) or 0,
        "active_purchases": getattr(acc, "active_purchases", 0) or 0,
        "last_ping": time.time(),
        "last_error": None,
        "online": True,
    }


def start_worker(fp_token: str) -> dict:
    """Поднимает воркер для аккаунта, делает первый .get() для логина."""
    data = load_account_data(fp_token)
    if not data:
        return {"ok": False, "error": "Аккаунт не загружен. Сначала /api/account/upload_key."}
    try:
        gk = decrypt_gk(data["gk"])
    except Exception as e:
        return {"ok": False, "error": f"decrypt failed: {e}"}
    try:
        acc = _make_account(gk, data.get("user_agent", ""))
        acc.get()
        if not acc.is_initiated:
            return {"ok": False, "error": "Аккаунт не инициализирован — проверьте golden_key"}
    except Exception as e:
        return {"ok": False, "error": f"login failed: {e}"}
    state = _refresh_state(fp_token, acc)
    state["account"] = acc
    with _lock:
        _workers[fp_token] = state
    _set_active(fp_token, True)
    return {
        "ok": True,
        "username": state["username"],
        "balance": state["balance"],
        "active_sales": state["active_sales"],
    }


def stop_worker(fp_token: str) -> dict:
    with _lock:
        _workers.pop(fp_token, None)
    if _path(fp_token).exists():
        _set_active(fp_token, False)
    return {"ok": True}


def get_status(fp_token: str) -> dict:
    with _lock:
        st = _workers.get(fp_token)
    if st:
        return {
            "online": st.get("online", False),
            "stored": True,
            "active": True,
            "username": st.get("username", ""),
            "user_id": st.get("user_id"),
            "balance": st.get("balance", 0),
            "currency": st.get("currency", ""),
            "active_sales": st.get("active_sales", 0),
            "active_purchases": st.get("active_purchases", 0),
            "last_ping": st.get("last_ping"),
            "last_error": st.get("last_error"),
        }
    d = load_account_data(fp_token)
    if d:
        return {
            "online": False,
            "stored": True,
            "active": bool(d.get("active")),
            "last_error": None,
        }
    return {"online": False, "stored": False, "active": False}


# ─── Keepalive thread ─────────────────────────────────────────────────────────
def _keepalive_loop():
    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        with _lock:
            tokens = list(_workers.keys())
        for tok in tokens:
            with _lock:
                st = _workers.get(tok)
            if not st:
                continue
            acc = st.get("account")
            if not acc:
                continue
            try:
                acc.get()
                upd = _refresh_state(tok, acc)
                upd["account"] = acc
                with _lock:
                    if tok in _workers:
                        _workers[tok].update(upd)
            except Exception as e:
                with _lock:
                    if tok in _workers:
                        _workers[tok]["last_error"] = str(e)[:300]
                        _workers[tok]["online"] = False


_started = False

def start_keepalive_thread():
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_keepalive_loop, daemon=True, name="fp-keepalive")
    t.start()


def restore_workers():
    """Поднимает воркеры всех аккаунтов с active=True. Вызывается при старте VPS."""
    for f in ACCOUNTS_DIR.glob("*.json"):
        fp_token = f.stem
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if not d.get("active"):
            continue
        res = start_worker(fp_token)
        if not res.get("ok"):
            print(f"[restore] {fp_token}: {res.get('error')}")
        else:
            print(f"[restore] {fp_token}: OK ({res.get('username')})")


def list_active() -> list[str]:
    with _lock:
        return list(_workers.keys())
