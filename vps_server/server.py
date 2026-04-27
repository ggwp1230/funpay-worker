"""
FunPay Bot — Update Server
Запускается на VPS. Хранит актуальную версию и раздаёт обновления клиентам.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Multi-tenant FunPay-воркер (Phase 1: keepalive). Импорт обёрнут в try/except,
# чтобы старые установки VPS без FunPayAPI/cryptography всё ещё могли раздавать
# обновления и не падать при импорте.
try:
    import funpay_worker as fw  # type: ignore
    FW_AVAILABLE = True
except Exception as _e:
    print(f"[funpay_worker] not available: {_e}")
    fw = None  # type: ignore
    FW_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────
ADMIN_TOKEN  = os.environ.get("ADMIN_TOKEN", "change_me_secret_admin_token")
PORT         = int(os.environ.get("PORT", 9000))

UPDATES_DIR  = Path("updates")
UPDATES_DIR.mkdir(exist_ok=True)
OTP_DIR      = Path("otps")
OTP_DIR.mkdir(exist_ok=True)
TOKENS_DIR   = Path("tokens")
TOKENS_DIR.mkdir(exist_ok=True)
PLUGINS_DIR  = Path("plugins")
PLUGINS_DIR.mkdir(exist_ok=True)

META_FILE    = UPDATES_DIR / "meta.json"
CURRENT_ZIP  = UPDATES_DIR / "current.zip"

PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")

OTP_TTL      = 300   # 5 минут
TOKEN_TTL    = 86400 * 30  # 30 дней

# ── Helpers ───────────────────────────────────────────────────────────────────
def load_meta() -> dict:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text())
        except Exception:
            pass
    return {"version": "0.0.0", "changelog": "", "uploaded_at": 0, "size": 0, "sha256": ""}

def save_meta(meta: dict):
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def require_admin(token: Optional[str]):
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")

def save_token(token: str, ip: str = "", version: str = "1.0.0", worker_url: str = ""):
    """Сохраняет fp_ токен на диск.
    worker_url — публичный URL VPS-воркера юзера (http://IP:PORT). Используется
    в /api/vps/lookup, чтобы Electron-приложение могло подтянуть адрес VPS
    по одному только токену (без ввода вручную)."""
    data = {
        "token": token,
        "ip": ip,
        "worker_url": (worker_url or "").rstrip("/"),
        "version": version,
        "created_at": time.time(),
    }
    (TOKENS_DIR / f"{token}.json").write_text(json.dumps(data))


def update_token_url(token: str, worker_url: str) -> bool:
    """Обновляет worker_url у уже выданного токена (например при переезде
    юзера на другой VPS — повторный install.sh подхватит тот же токен,
    если юзер перенёс tokens.json, и обновит URL)."""
    if not worker_url:
        return False
    token_file = TOKENS_DIR / f"{token}.json"
    if not token_file.exists():
        return False
    try:
        data = json.loads(token_file.read_text())
        data["worker_url"] = worker_url.rstrip("/")
        data["updated_at"] = time.time()
        token_file.write_text(json.dumps(data))
        return True
    except Exception:
        return False

def validate_fp_token(token: str) -> tuple[bool, dict]:
    """Проверяет fp_ токен. Возвращает (valid, data)."""
    if not token or not token.startswith("fp_"):
        return False, {"error": "Invalid token format"}
    token_file = TOKENS_DIR / f"{token}.json"
    if not token_file.exists():
        return False, {"error": "Invalid access token"}
    try:
        data = json.loads(token_file.read_text())
        if time.time() - data.get("created_at", 0) > TOKEN_TTL:
            token_file.unlink()
            return False, {"error": "Token expired"}
        return True, data
    except Exception:
        return False, {"error": "Invalid token"}

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="FunPay Bot Update Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Client endpoints (защищены fp_ токеном) ───────────────────────────────────

@app.get("/version")
def get_version(x_token: Optional[str] = Header(None)):
    valid, data = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, data.get("error", "Invalid access token"))
    meta = load_meta()
    meta["remote_version"] = meta["version"]
    return meta

@app.get("/download")
def download_update(x_token: Optional[str] = Header(None)):
    valid, data = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, data.get("error", "Invalid access token"))
    if not CURRENT_ZIP.exists():
        raise HTTPException(404, "No update available")
    return FileResponse(CURRENT_ZIP, media_type="application/zip", filename="update.zip")

@app.get("/ping")
def ping():
    return {"ok": True, "time": time.time()}

# ── VPS регистрация через OTP ─────────────────────────────────────────────────

@app.post("/api/vps/register")
def vps_register(data: dict, request: Request):
    otp = data.get("otp", "").upper().strip()
    ip  = (data.get("ip") or "").strip()
    # install.sh передаёт worker_url (http://IP:PORT). Если юзер не передал —
    # пытаемся собрать из ip+port.
    worker_url = (data.get("worker_url") or "").strip().rstrip("/")
    if not worker_url and ip:
        port = str(data.get("port") or 8000)
        worker_url = f"http://{ip}:{port}"
    otp_file = OTP_DIR / f"{otp}.json"

    if not otp_file.exists():
        return {"error": "invalid_otp"}

    try:
        otp_data = json.loads(otp_file.read_text())
    except Exception:
        return {"error": "invalid_otp"}

    if time.time() - otp_data.get("created_at", 0) > OTP_TTL:
        otp_file.unlink(missing_ok=True)
        return {"error": "expired_otp"}

    # OTP одноразовый — удаляем
    otp_file.unlink(missing_ok=True)

    # Генерируем fp_ токен и сохраняем
    token = "fp_" + hashlib.sha256(f"{otp}{time.time()}".encode()).hexdigest()[:32]
    meta  = load_meta()
    save_token(token, ip=ip, version=meta["version"], worker_url=worker_url)

    return {"token": token, "version": meta["version"], "worker_url": worker_url}


@app.post("/api/vps/update_url")
def vps_update_url(data: dict, request: Request):
    """install.sh может вызвать это вместо register, если у пользователя уже
    есть рабочий токен (например он переустанавливает воркер на другом VPS,
    но хочет сохранить тот же fp-токен в Electron-приложении)."""
    token = (data.get("token") or "").strip()
    worker_url = (data.get("worker_url") or "").strip().rstrip("/")
    valid, _ = validate_fp_token(token)
    if not valid:
        raise HTTPException(404, "Токен не найден или истёк")
    ok = update_token_url(token, worker_url)
    return {"ok": ok, "worker_url": worker_url}


@app.get("/api/vps/lookup")
def vps_lookup(token: str = ""):
    """Electron-приложение шлёт сюда fp-токен и получает адрес VPS-воркера
    юзера. Так юзер вводит ОДИН раз только токен — приложение само подтянет URL.
    Не требует Authorization-заголовков: токен сам по себе является аутентификацией."""
    valid, data = validate_fp_token(token)
    if not valid:
        raise HTTPException(404, data.get("error", "Token not found"))
    url = (data.get("worker_url") or "").strip()
    if not url:
        # Совместимость со старыми токенами (выданными до этого PR'а): соберём
        # URL по сохранённому ip и стандартному порту 8000.
        ip = (data.get("ip") or "").strip()
        if ip:
            url = f"http://{ip}:8000"
    if not url:
        raise HTTPException(404, "URL не сохранён для этого токена. Перезапустите install.sh на VPS.")
    return {"ok": True, "worker_url": url}

@app.post("/api/vps/auto_register")
def vps_auto_register(request: Request):
    """Эндпоинт отключён.

    Раньше приложение само получало fp_-токен на старте (zero-config), но это
    позволяло любому в интернете дёргать VPS и получать рабочие токены без
    ограничений. Теперь fp_-токен выдаёт ТОЛЬКО Telegram-бот после OTP — это
    гейт доступа к продукту."""
    raise HTTPException(410, "auto_register отключён — получите fp-токен в Telegram-боте")

@app.get("/api/version")
def api_version():
    return {"version": load_meta()["version"]}


# ── FunPay account endpoints (Phase 1: keepalive) ────────────────────────────
def _require_fp(x_token: Optional[str]) -> str:
    valid, data = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, data.get("error", "Invalid access token"))
    return x_token  # type: ignore[return-value]


def _require_fw():
    if not FW_AVAILABLE:
        raise HTTPException(
            503,
            "FunPay-воркер не установлен на VPS. Установите зависимости: "
            "pip install FunPayAPI cryptography и задайте FP_GOLDEN_KEY_AES в env."
        )


class GoldenKeyBody(BaseModel):
    golden_key: str
    user_agent: Optional[str] = ""


@app.post("/api/account/upload_key")
def account_upload_key(
    body: GoldenKeyBody,
    x_token: Optional[str] = Header(None),
):
    """Сохраняет зашифрованный golden_key для пользователя (по fp-токену)."""
    _require_fw()
    fp_token = _require_fp(x_token)
    if not body.golden_key.strip():
        raise HTTPException(400, "golden_key пустой")
    fw.store_account(fp_token, body.golden_key.strip(), body.user_agent or "")
    return {"ok": True}


@app.post("/api/account/start")
def account_start(x_token: Optional[str] = Header(None)):
    """Поднимает keepalive-воркер для аккаунта. Делает первый login."""
    _require_fw()
    fp_token = _require_fp(x_token)
    return fw.start_worker(fp_token)


@app.post("/api/account/stop")
def account_stop(x_token: Optional[str] = Header(None)):
    """Останавливает воркер (аккаунт уйдёт в офлайн)."""
    _require_fw()
    fp_token = _require_fp(x_token)
    return fw.stop_worker(fp_token)


@app.get("/api/account/status")
def account_status(x_token: Optional[str] = Header(None)):
    """Текущий статус keepalive-воркера: онлайн, баланс, продажи, ошибки."""
    _require_fw()
    fp_token = _require_fp(x_token)
    return fw.get_status(fp_token)


@app.delete("/api/account")
def account_delete(x_token: Optional[str] = Header(None)):
    """Полностью удаляет аккаунт с VPS (для logout)."""
    _require_fw()
    fp_token = _require_fp(x_token)
    fw.delete_account(fp_token)
    return {"ok": True}


# ── Admin endpoints ────────────────────────────────────────────────────────────

@app.post("/admin/upload")
async def upload_update(
    file: UploadFile = File(...),
    version: str = Form("1.0.0"),
    changelog: str = Form(""),
    x_admin_token: Optional[str] = Header(None)
):
    require_admin(x_admin_token)
    if not file.filename.endswith(".zip"):
        raise HTTPException(400, "Only .zip files accepted")

    tmp = UPDATES_DIR / "upload_tmp.zip"
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)

    checksum = sha256_file(tmp)
    size = tmp.stat().st_size
    tmp.rename(CURRENT_ZIP)

    meta = {"version": version, "changelog": changelog,
            "uploaded_at": time.time(), "size": size, "sha256": checksum}
    save_meta(meta)
    return {"ok": True, "version": version, "size": size, "sha256": checksum}

@app.get("/admin/status")
def admin_status(x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    meta = load_meta()
    tokens = list(TOKENS_DIR.glob("*.json"))
    return {
        "current_version": meta["version"],
        "has_file": CURRENT_ZIP.exists(),
        "active_tokens": len(tokens),
        "meta": meta,
    }

@app.delete("/admin/delete")
def delete_update(x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    if CURRENT_ZIP.exists():
        CURRENT_ZIP.unlink()
    save_meta({"version": "0.0.0", "changelog": "", "uploaded_at": 0, "size": 0, "sha256": ""})
    return {"ok": True}

# ── Plugins helpers ────────────────────────────────────────────────────────────

# Расширенные данные плагина (скриншоты, длинное описание, отзывы) живут
# в отдельной поддиректории plugins/<id>/. Это отделено от основных
# plugins/<id>.zip / plugins/<id>.json ради обратной совместимости — старый
# клиент, который не знает о карточке деталей, получит те же поля что раньше.
def _plugin_paths(plugin_id: str) -> tuple[Path, Path]:
    return PLUGINS_DIR / f"{plugin_id}.zip", PLUGINS_DIR / f"{plugin_id}.json"


def _plugin_dir(plugin_id: str, create: bool = False) -> Path:
    """Путь до директории с расширенными данными плагина. По умолчанию только
    возвращает путь (без побочных эффектов), create=True — вызывается из
    write-paths и создаёт директорию."""
    d = PLUGINS_DIR / plugin_id
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _plugin_details_file(plugin_id: str, create: bool = False) -> Path:
    """JSON с long_description, screenshots[], обновляется через админку."""
    return _plugin_dir(plugin_id, create=create) / "details.json"


def _plugin_reviews_file(plugin_id: str, create: bool = False) -> Path:
    return _plugin_dir(plugin_id, create=create) / "reviews.json"


def _plugin_downloads_file(plugin_id: str, create: bool = False) -> Path:
    return _plugin_dir(plugin_id, create=create) / "downloads.json"


def _plugin_icon_file(plugin_id: str) -> Optional[Path]:
    d = _plugin_dir(plugin_id)
    if not d.exists():
        return None
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = d / f"icon.{ext}"
        if p.exists():
            return p
    return None


def _plugin_screenshot_file(plugin_id: str, n: int) -> Optional[Path]:
    d = _plugin_dir(plugin_id)
    if not d.exists():
        return None
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = d / f"s{n}.{ext}"
        if p.exists():
            return p
    return None


# Защита от гонок при read-modify-write на reviews.json / downloads.json.
# FastAPI гоняет sync-хендлеры в thread pool — два POST /reviews на один и тот
# же плагин могут стереть данные друг друга. Лочим отдельно per plugin_id,
# чтобы разные плагины не блокировали друг друга.
_plugin_locks_mu = threading.Lock()
_plugin_locks: dict[str, threading.Lock] = {}


def _plugin_lock(plugin_id: str) -> threading.Lock:
    with _plugin_locks_mu:
        lk = _plugin_locks.get(plugin_id)
        if lk is None:
            lk = threading.Lock()
            _plugin_locks[plugin_id] = lk
        return lk


def _load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def _save_json(path: Path, data):
    """Атомарная запись: tmp + rename, чтобы не оставить полу-файл при падении."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def _token_hash(token: str) -> str:
    """Короткий детерминированный хеш токена — используется как псевдо-user-id
    в отзывах (так можно проверять «один юзер = один отзыв» без хранения
    самого токена в плейнтексте)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def _record_download(plugin_id: str, token: str):
    """Отметка что token скачал плагин. Используется для проверки права
    оставлять отзыв."""
    if not token:
        return
    th = _token_hash(token)
    path = _plugin_downloads_file(plugin_id, create=True)
    with _plugin_lock(plugin_id):
        data = _load_json(path, {})
        if not isinstance(data, dict):
            data = {}
        if th not in data:
            data[th] = {"first_at": time.time()}
        data[th]["last_at"] = time.time()
        _save_json(path, data)


def _has_downloaded(plugin_id: str, token: str) -> bool:
    th = _token_hash(token)
    data = _load_json(_plugin_downloads_file(plugin_id), {})
    return isinstance(data, dict) and th in data


def _reviews_summary(plugin_id: str) -> dict:
    """Сводка: кол-во отзывов, средний рейтинг, распределение по звёздам."""
    reviews = _load_json(_plugin_reviews_file(plugin_id), [])
    if not isinstance(reviews, list):
        reviews = []
    visible = [r for r in reviews if r.get("approved", True)]
    dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    total = 0.0
    for r in visible:
        rr = int(r.get("rating") or 0)
        if 1 <= rr <= 5:
            dist[rr] = dist.get(rr, 0) + 1
            total += rr
    count = sum(dist.values())
    avg = round(total / count, 2) if count else 0.0
    return {"count": count, "avg": avg, "dist": dist}

def _read_plugin_json(zip_bytes: bytes) -> dict:
    """Извлекает plugin.json из переданного zip-архива."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    except zipfile.BadZipFile:
        raise HTTPException(400, "Файл не является корректным zip-архивом")
    with zf:
        try:
            raw = zf.read("plugin.json")
        except KeyError:
            raise HTTPException(400, "В архиве нет plugin.json в корне")
        try:
            return json.loads(raw)
        except Exception as e:
            raise HTTPException(400, f"plugin.json не парсится: {e}")

def _validate_plugin_meta(meta: dict) -> dict:
    pid = str(meta.get("id", "")).strip()
    if not PLUGIN_ID_RE.match(pid):
        raise HTTPException(400,
            "id должен быть [a-z0-9_-], 2..64 символа")
    if not meta.get("name"):
        raise HTTPException(400, "name обязателен")
    if not meta.get("version"):
        raise HTTPException(400, "version обязателен")
    return {
        "id": pid,
        "name": str(meta["name"]),
        "version": str(meta["version"]),
        "description": str(meta.get("description", "")),
        "author": str(meta.get("author", "")),
        "hooks": list(meta.get("hooks") or []),
        "config_schema": list(meta.get("config_schema") or []),
    }

def list_plugins() -> list[dict]:
    out = []
    for json_path in sorted(PLUGINS_DIR.glob("*.json")):
        try:
            meta = json.loads(json_path.read_text())
        except Exception:
            continue
        pid = meta.get("id")
        if pid:
            meta["has_icon"] = _plugin_icon_file(pid) is not None
            summary = _reviews_summary(pid)
            meta["reviews_count"] = summary["count"]
            meta["rating"] = summary["avg"]
        out.append(meta)
    return out

# ── Plugin endpoints ───────────────────────────────────────────────────────────

@app.post("/admin/plugins/upload")
async def admin_plugin_upload(
    file: UploadFile = File(...),
    x_admin_token: Optional[str] = Header(None),
):
    """Загрузка плагина. Метаданные читаются из plugin.json внутри zip."""
    require_admin(x_admin_token)
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(400, "Нужен .zip файл")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Пустой файл")

    plugin_json = _read_plugin_json(raw)
    meta = _validate_plugin_meta(plugin_json)

    zip_path, json_path = _plugin_paths(meta["id"])
    zip_path.write_bytes(raw)
    meta["size"] = len(raw)
    meta["sha256"] = hashlib.sha256(raw).hexdigest()
    meta["uploaded_at"] = time.time()
    json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return {"ok": True, "plugin": meta}

@app.get("/admin/plugins")
def admin_plugin_list(x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    return {"plugins": list_plugins()}

@app.delete("/admin/plugins/{plugin_id}")
def admin_plugin_delete(plugin_id: str,
                        x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    zip_path, json_path = _plugin_paths(plugin_id)
    zip_path.unlink(missing_ok=True)
    json_path.unlink(missing_ok=True)
    # Удаляем и расширенные данные (иконка, скриншоты, reviews, downloads).
    pdir = PLUGINS_DIR / plugin_id
    if pdir.exists() and pdir.is_dir():
        shutil.rmtree(pdir, ignore_errors=True)
    return {"ok": True}


@app.post("/admin/plugins/{plugin_id}/details")
async def admin_plugin_set_details(
    plugin_id: str,
    long_description: str = Form(""),
    x_admin_token: Optional[str] = Header(None),
):
    require_admin(x_admin_token)
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    _, json_path = _plugin_paths(plugin_id)
    if not json_path.exists():
        raise HTTPException(404, "Плагин не найден")
    path = _plugin_details_file(plugin_id, create=True)
    with _plugin_lock(plugin_id):
        details = _load_json(path, {})
        if not isinstance(details, dict):
            details = {}
        details["long_description"] = (long_description or "").strip()[:20000]
        details["updated_at"] = time.time()
        _save_json(path, details)
    return {"ok": True}


def _write_image(target_dir: Path, base_name: str, upload: UploadFile, raw: bytes) -> Path:
    """Сохраняет изображение, очищая старые версии того же base_name.*."""
    target_dir.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "jpg", "jpeg", "webp"):
        old = target_dir / f"{base_name}.{ext}"
        if old.exists():
            old.unlink()
    suffix = "png"
    fn = (upload.filename or "").lower()
    for ext in ("png", "jpg", "jpeg", "webp"):
        if fn.endswith("." + ext):
            suffix = ext
            break
    path = target_dir / f"{base_name}.{suffix}"
    path.write_bytes(raw)
    return path


@app.post("/admin/plugins/{plugin_id}/icon")
async def admin_plugin_upload_icon(
    plugin_id: str,
    file: UploadFile = File(...),
    x_admin_token: Optional[str] = Header(None),
):
    require_admin(x_admin_token)
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    raw = await file.read()
    if not raw or len(raw) > 2_000_000:
        raise HTTPException(400, "Картинка пустая или больше 2 МБ")
    _write_image(_plugin_dir(plugin_id, create=True), "icon", file, raw)
    return {"ok": True}


@app.delete("/admin/plugins/{plugin_id}/icon")
def admin_plugin_delete_icon(plugin_id: str,
                             x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    icon = _plugin_icon_file(plugin_id)
    if icon:
        icon.unlink()
    return {"ok": True}


@app.post("/admin/plugins/{plugin_id}/screenshots")
async def admin_plugin_upload_screenshot(
    plugin_id: str,
    slot: int = Form(...),
    file: UploadFile = File(...),
    x_admin_token: Optional[str] = Header(None),
):
    require_admin(x_admin_token)
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    if slot < 1 or slot > 20:
        raise HTTPException(400, "Слот 1..20")
    raw = await file.read()
    if not raw or len(raw) > 5_000_000:
        raise HTTPException(400, "Картинка пустая или больше 5 МБ")
    _write_image(_plugin_dir(plugin_id, create=True), f"s{slot}", file, raw)
    return {"ok": True, "slot": slot}


@app.delete("/admin/plugins/{plugin_id}/screenshots/{slot}")
def admin_plugin_delete_screenshot(plugin_id: str, slot: int,
                                   x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    shot = _plugin_screenshot_file(plugin_id, slot)
    if shot:
        shot.unlink()
    return {"ok": True}


@app.get("/admin/plugins/{plugin_id}/details")
def admin_plugin_get_details(plugin_id: str,
                             x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    details = _load_json(_plugin_details_file(plugin_id), {})
    if not isinstance(details, dict):
        details = {}
    screenshots = [n for n in range(1, 21) if _plugin_screenshot_file(plugin_id, n)]
    return {
        "long_description": details.get("long_description", ""),
        "screenshots": screenshots,
        "has_icon": _plugin_icon_file(plugin_id) is not None,
    }


@app.get("/admin/plugins/{plugin_id}/reviews")
def admin_plugin_list_reviews(plugin_id: str,
                              x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    reviews = _load_json(_plugin_reviews_file(plugin_id), [])
    return {"reviews": reviews}


@app.delete("/admin/plugins/{plugin_id}/reviews/{review_id}")
def admin_plugin_delete_review(plugin_id: str, review_id: str,
                               x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    path = _plugin_reviews_file(plugin_id)
    with _plugin_lock(plugin_id):
        reviews = _load_json(path, [])
        if not isinstance(reviews, list):
            reviews = []
        new = [r for r in reviews if r.get("id") != review_id]
        _save_json(path, new)
    return {"ok": True}

@app.get("/plugins")
def public_plugin_list(x_token: Optional[str] = Header(None)):
    valid, data = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, data.get("error", "Invalid access token"))
    return {"plugins": list_plugins()}

@app.get("/plugins/{plugin_id}/meta")
def public_plugin_meta(plugin_id: str,
                       x_token: Optional[str] = Header(None)):
    valid, data = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, data.get("error", "Invalid access token"))
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    _, json_path = _plugin_paths(plugin_id)
    if not json_path.exists():
        raise HTTPException(404, "Плагин не найден")
    return json.loads(json_path.read_text())

@app.get("/plugins/{plugin_id}/download")
def public_plugin_download(plugin_id: str,
                           x_token: Optional[str] = Header(None)):
    valid, data = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, data.get("error", "Invalid access token"))
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    zip_path, _ = _plugin_paths(plugin_id)
    if not zip_path.exists():
        raise HTTPException(404, "Плагин не найден")
    _record_download(plugin_id, x_token or "")
    return FileResponse(zip_path, media_type="application/zip",
                        filename=f"{plugin_id}.zip")


@app.get("/plugins/{plugin_id}/details")
def public_plugin_details(plugin_id: str,
                          x_token: Optional[str] = Header(None)):
    """Полная карточка плагина: базовая мета + long_description + скриншоты +
    сводка по отзывам. Используется витриной в приложении."""
    valid, data = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, data.get("error", "Invalid access token"))
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    _, json_path = _plugin_paths(plugin_id)
    if not json_path.exists():
        raise HTTPException(404, "Плагин не найден")
    meta = json.loads(json_path.read_text())
    details = _load_json(_plugin_details_file(plugin_id), {})
    if not isinstance(details, dict):
        details = {}
    meta["long_description"] = details.get("long_description", "")
    # Скриншоты: авто-индексируем по файлам s1.*, s2.*, ...
    screenshots = []
    for n in range(1, 21):
        if _plugin_screenshot_file(plugin_id, n):
            screenshots.append(n)
    meta["screenshots"] = screenshots
    meta["has_icon"] = _plugin_icon_file(plugin_id) is not None
    summary = _reviews_summary(plugin_id)
    meta["reviews_count"] = summary["count"]
    meta["rating"] = summary["avg"]
    meta["rating_dist"] = summary["dist"]
    meta["downloaded"] = _has_downloaded(plugin_id, x_token or "")
    return meta


@app.get("/plugins/{plugin_id}/icon")
def public_plugin_icon(plugin_id: str,
                       x_token: Optional[str] = Header(None)):
    valid, _d = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, "Invalid access token")
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    icon = _plugin_icon_file(plugin_id)
    if not icon:
        raise HTTPException(404, "Иконка не задана")
    return FileResponse(icon)


@app.get("/plugins/{plugin_id}/screenshots/{n}")
def public_plugin_screenshot(plugin_id: str, n: int,
                             x_token: Optional[str] = Header(None)):
    valid, _d = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, "Invalid access token")
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    if n < 1 or n > 20:
        raise HTTPException(400, "Невалидный номер")
    shot = _plugin_screenshot_file(plugin_id, n)
    if not shot:
        raise HTTPException(404, "Скриншот не найден")
    return FileResponse(shot)


@app.get("/plugins/{plugin_id}/reviews")
def public_plugin_reviews(plugin_id: str,
                          x_token: Optional[str] = Header(None)):
    valid, _d = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, "Invalid access token")
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    reviews = _load_json(_plugin_reviews_file(plugin_id), [])
    if not isinstance(reviews, list):
        reviews = []
    visible = [r for r in reviews if r.get("approved", True)]
    # Сортируем от новых к старым. Кроме токена ничего чувствительного наружу
    # не отдаём: юзер видит только author (отображаемое имя) + rating + text.
    visible.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    my_hash = _token_hash(x_token or "") if x_token else ""
    out = []
    for r in visible:
        out.append({
            "id": r.get("id"),
            "author": r.get("author") or "Аноним",
            "rating": int(r.get("rating") or 0),
            "text": r.get("text", ""),
            "created_at": r.get("created_at", 0),
            "mine": my_hash and r.get("token_hash") == my_hash,
        })
    summary = _reviews_summary(plugin_id)
    return {
        "reviews": out,
        "count": summary["count"],
        "avg": summary["avg"],
        "dist": summary["dist"],
        "can_review": _has_downloaded(plugin_id, x_token or ""),
    }


class ReviewPayload(BaseModel):
    author: Optional[str] = None
    rating: int
    text: Optional[str] = None


@app.post("/plugins/{plugin_id}/reviews")
def public_plugin_post_review(plugin_id: str,
                              body: ReviewPayload,
                              x_token: Optional[str] = Header(None)):
    valid, _d = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, "Invalid access token")
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    _, json_path = _plugin_paths(plugin_id)
    if not json_path.exists():
        raise HTTPException(404, "Плагин не найден")
    if not _has_downloaded(plugin_id, x_token or ""):
        raise HTTPException(403, "Отзыв можно оставить только после установки плагина")
    rating = int(body.rating or 0)
    if rating < 1 or rating > 5:
        raise HTTPException(400, "Рейтинг должен быть 1–5")
    text = (body.text or "").strip()[:2000]
    author = (body.author or "").strip()[:40] or "Аноним"

    th = _token_hash(x_token or "")
    path = _plugin_reviews_file(plugin_id, create=True)
    with _plugin_lock(plugin_id):
        reviews = _load_json(path, [])
        if not isinstance(reviews, list):
            reviews = []
        # Upsert — один токен = один отзыв; повторная отправка обновляет.
        now = time.time()
        existing = next((r for r in reviews if r.get("token_hash") == th), None)
        if existing:
            existing.update({
                "author": author, "rating": rating, "text": text,
                "updated_at": now, "approved": existing.get("approved", True),
            })
            rid = existing.get("id")
        else:
            rid = hashlib.sha256(f"{th}-{now}".encode()).hexdigest()[:16]
            reviews.append({
                "id": rid,
                "token_hash": th,
                "author": author,
                "rating": rating,
                "text": text,
                "created_at": now,
                "approved": True,
            })
        _save_json(path, reviews)
    return {"ok": True, "id": rid}


@app.delete("/plugins/{plugin_id}/reviews/mine")
def public_plugin_delete_my_review(plugin_id: str,
                                   x_token: Optional[str] = Header(None)):
    valid, _d = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, "Invalid access token")
    if not PLUGIN_ID_RE.match(plugin_id):
        raise HTTPException(400, "Невалидный id")
    th = _token_hash(x_token or "")
    path = _plugin_reviews_file(plugin_id)
    with _plugin_lock(plugin_id):
        reviews = _load_json(path, [])
        if not isinstance(reviews, list):
            reviews = []
        new = [r for r in reviews if r.get("token_hash") != th]
        if len(new) == len(reviews):
            raise HTTPException(404, "Отзыв не найден")
        _save_json(path, new)
    return {"ok": True}

@app.get("/admin", response_class=HTMLResponse)
def admin_panel():
    meta = load_meta()
    size_kb = round(meta.get("size", 0) / 1024, 1)
    uploaded = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta.get("uploaded_at", 0))) \
               if meta.get("uploaded_at") else "—"
    tokens_count = len(list(TOKENS_DIR.glob("*.json")))

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FunPay Bot — Update Admin</title>
<style>
  :root {{--bg:#0d1117;--card:#161b22;--border:#30363d;--accent:#00e5ff;--green:#00e676;--red:#ff3d71;--text:#e6edf3;--dim:#8b949e}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}}
  .card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:32px;width:100%;max-width:560px}}
  h1{{font-size:20px;font-weight:700;margin-bottom:4px;color:var(--accent)}}
  .sub{{font-size:12px;color:var(--dim);margin-bottom:28px}}
  .meta{{background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:24px;font-size:13px;line-height:1.8}}
  .meta b{{color:var(--accent)}}
  label{{font-size:12px;color:var(--dim);display:block;margin-bottom:5px;margin-top:14px}}
  input,textarea{{width:100%;background:#0d1117;border:1px solid var(--border);color:var(--text);padding:9px 12px;border-radius:7px;font-family:inherit;font-size:13px;outline:none}}
  input:focus,textarea:focus{{border-color:var(--accent)}}
  textarea{{height:72px;resize:vertical}}
  .btn{{display:inline-block;padding:10px 20px;border:none;border-radius:7px;font-size:13px;font-weight:600;cursor:pointer;margin-top:16px;transition:.15s}}
  .btn-primary{{background:var(--accent);color:#050d12}}
  .btn-danger{{background:var(--red);color:#fff;margin-left:8px}}
  .btn-sm{{padding:5px 10px;font-size:11px;margin-top:0}}
  .btn:hover{{opacity:.85}}
  .tabs{{display:flex;gap:6px;margin-bottom:20px;border-bottom:1px solid var(--border)}}
  .tab{{padding:10px 16px;font-size:13px;color:var(--dim);cursor:pointer;border-bottom:2px solid transparent;user-select:none}}
  .tab.active{{color:var(--accent);border-bottom-color:var(--accent)}}
  .pane{{display:none}}
  .pane.active{{display:block}}
  .plugin-row{{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;background:#0d1117;border:1px solid var(--border);border-radius:7px;margin-bottom:6px;font-size:13px}}
  .plugin-row .pid{{font-weight:600;color:var(--text)}}
  .plugin-row .pmeta{{color:var(--dim);font-size:11px;margin-top:2px}}
  #result,#result2,#result3{{margin-top:16px;padding:10px 14px;border-radius:7px;font-size:13px;display:none}}
  .ok{{background:#00e67622;border:1px solid var(--green);color:var(--green)}}
  .err{{background:#ff3d7122;border:1px solid var(--red);color:var(--red)}}
</style>
</head>
<body>
<div class="card">
  <h1>⚡ FunPay Bot — Admin Panel</h1>
  <div class="sub">Управление обновлениями</div>

  <div id="auth-section">
    <label>Admin Token</label>
    <input type="password" id="admin-token" placeholder="Введите admin token..." />
    <button class="btn btn-primary" onclick="authCheck()">Войти</button>
    <div id="result"></div>
  </div>

  <div id="main-section" style="display:none">
    <div class="tabs">
      <div class="tab active" data-tab="updates" onclick="switchTab('updates')">Обновления</div>
      <div class="tab" data-tab="plugins" onclick="switchTab('plugins')">Плагины</div>
    </div>

    <div class="pane active" id="pane-updates">
      <div class="meta">
        <div><b>Текущая версия:</b> <span id="m-ver">{meta['version']}</span></div>
        <div><b>Размер:</b> <span id="m-size">{size_kb} KB</span></div>
        <div><b>Загружено:</b> {uploaded}</div>
        <div><b>Активных токенов:</b> {tokens_count}</div>
        <div style="margin-top:6px;color:var(--dim);font-size:11px"><b>Changelog:</b> {meta.get('changelog','—')}</div>
      </div>

      <label>Файл обновления (.zip)</label>
      <input type="file" id="update-file" accept=".zip" />
      <label>Новая версия</label>
      <input type="text" id="new-version" placeholder="например: 1.2.0" />
      <label>Changelog</label>
      <textarea id="changelog" placeholder="Что изменилось..."></textarea>
      <div>
        <button class="btn btn-primary" onclick="uploadUpdate()">⬆ Загрузить обновление</button>
        <button class="btn btn-danger" onclick="deleteUpdate()">🗑 Удалить</button>
      </div>
      <div id="result2"></div>
    </div>

    <div class="pane" id="pane-plugins">
      <div style="font-size:12px;color:var(--dim);margin-bottom:10px">
        Загрузи zip с <code>plugin.json</code> в корне — метаданные читаются автоматически.
      </div>
      <div id="plugin-list" style="margin-bottom:18px"></div>
      <label>Файл плагина (.zip)</label>
      <input type="file" id="plugin-file" accept=".zip" />
      <div>
        <button class="btn btn-primary" onclick="uploadPlugin()">⬆ Загрузить плагин</button>
      </div>
      <div id="result3"></div>

      <div id="edit-plugin-wrap" style="display:none;margin-top:22px;padding-top:18px;border-top:1px solid var(--border)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <div style="font-size:14px;font-weight:600;color:var(--accent)">
            Детали плагина: <span id="edit-plugin-id">—</span>
          </div>
          <button class="btn btn-sm" style="background:#0d1117;color:var(--text);border:1px solid var(--border)" onclick="closeEdit()">Закрыть</button>
        </div>

        <label>Подробное описание (markdown)</label>
        <textarea id="edit-plugin-desc" style="height:120px" placeholder="Для чего нужен плагин, примеры использования, ограничения..."></textarea>
        <button class="btn btn-primary btn-sm" onclick="saveDescription()">💾 Сохранить описание</button>

        <label>Иконка (png/jpg/webp, до 2 МБ)</label>
        <input type="file" id="edit-icon-file" accept="image/*" onchange="uploadIcon(event)" />

        <label>Скриншоты работы</label>
        <div style="font-size:11px;color:var(--dim);margin-bottom:4px" id="edit-shot-list">Скриншотов нет</div>
        <div style="display:flex;gap:6px;align-items:center">
          <label style="margin:0;width:60px">Слот:</label>
          <input type="number" id="edit-shot-slot" min="1" max="20" value="1" style="width:70px" />
          <input type="file" id="edit-shot-file" accept="image/*" onchange="uploadScreenshot(event)" style="flex:1" />
          <button class="btn btn-danger btn-sm" onclick="deleteScreenshot()">Удалить слот</button>
        </div>

        <label style="margin-top:14px">Отзывы</label>
        <div id="edit-plugin-reviews" style="max-height:220px;overflow-y:auto"></div>

        <div id="edit-plugin-result" style="margin-top:10px;font-size:12px;min-height:16px"></div>
      </div>
    </div>
  </div>
</div>
<script>
let _token = '';
async function authCheck() {{
  _token = document.getElementById('admin-token').value.trim();
  const r = await fetch('/admin/status', {{headers:{{'x-admin-token': _token}}}});
  if (r.ok) {{
    document.getElementById('auth-section').style.display = 'none';
    document.getElementById('main-section').style.display = 'block';
    refreshPlugins();
  }} else {{ showResult('result', 'Неверный токен', false); }}
}}
function switchTab(name) {{
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.pane').forEach(p => p.classList.toggle('active', p.id === 'pane-' + name));
  if (name === 'plugins') refreshPlugins();
}}
async function refreshPlugins() {{
  const r = await fetch('/admin/plugins', {{headers:{{'x-admin-token':_token}}}});
  const wrap = document.getElementById('plugin-list');
  if (!r.ok) {{ wrap.innerHTML = '<div class="err" style="display:block">Ошибка загрузки</div>'; return; }}
  const d = await r.json();
  if (!d.plugins.length) {{ wrap.innerHTML = '<div style="color:var(--dim);font-size:12px;padding:8px 0">Пока нет загруженных плагинов</div>'; return; }}
  wrap.innerHTML = d.plugins.map(p => `
    <div class="plugin-row">
      <div>
        <div class="pid">${{p.name}} <span style="color:var(--dim);font-weight:400">v${{p.version}}</span>
          ${{p.reviews_count ? `<span style="color:var(--dim);font-size:11px"> ★${{p.rating||0}} (${{p.reviews_count}})</span>` : ''}}
        </div>
        <div class="pmeta">${{p.id}} · ${{p.author||'—'}} · ${{Math.round((p.size||0)/1024)}} KB${{p.has_icon?' · иконка':''}}</div>
        ${{p.description ? `<div class="pmeta" style="color:var(--text);margin-top:4px">${{p.description}}</div>` : ''}}
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-sm" style="background:var(--bg3,#0d1117);color:var(--text);border:1px solid var(--border)" onclick="editPlugin('${{p.id}}')">Детали</button>
        <button class="btn btn-danger btn-sm" onclick="deletePlugin('${{p.id}}')">Удалить</button>
      </div>
    </div>
  `).join('');
}}

async function editPlugin(pid) {{
  _editingPlugin = pid;
  document.getElementById('edit-plugin-id').textContent = pid;
  document.getElementById('edit-plugin-wrap').style.display = 'block';
  document.getElementById('edit-plugin-desc').value = '';
  document.getElementById('edit-shot-slot').value = '1';
  document.getElementById('edit-plugin-result').textContent = '';
  try {{
    const r = await fetch('/admin/plugins/' + encodeURIComponent(pid) + '/details',
      {{headers:{{'x-admin-token':_token}}}});
    if (r.ok) {{
      const d = await r.json();
      document.getElementById('edit-plugin-desc').value = d.long_description || '';
      document.getElementById('edit-shot-list').textContent =
        d.screenshots.length ? 'Скриншоты: ' + d.screenshots.join(', ') : 'Скриншотов нет';
    }}
  }} catch(_) {{}}
  await loadPluginReviewsAdmin(pid);
}}

function closeEdit() {{
  document.getElementById('edit-plugin-wrap').style.display = 'none';
  _editingPlugin = null;
}}
let _editingPlugin = null;

async function saveDescription() {{
  if (!_editingPlugin) return;
  const fd = new FormData();
  fd.append('long_description', document.getElementById('edit-plugin-desc').value);
  const r = await fetch('/admin/plugins/' + encodeURIComponent(_editingPlugin) + '/details',
    {{method:'POST',headers:{{'x-admin-token':_token}},body:fd}});
  const el = document.getElementById('edit-plugin-result');
  el.textContent = r.ok ? 'Описание сохранено' : 'Ошибка';
  el.style.color = r.ok ? 'var(--green)' : 'var(--red)';
}}

async function uploadIcon(ev) {{
  const f = ev.target.files[0]; if (!f || !_editingPlugin) return;
  const fd = new FormData(); fd.append('file', f);
  const r = await fetch('/admin/plugins/' + encodeURIComponent(_editingPlugin) + '/icon',
    {{method:'POST',headers:{{'x-admin-token':_token}},body:fd}});
  document.getElementById('edit-plugin-result').textContent = r.ok ? 'Иконка загружена' : 'Ошибка загрузки иконки';
  refreshPlugins();
}}

async function uploadScreenshot(ev) {{
  const f = ev.target.files[0]; if (!f || !_editingPlugin) return;
  const slot = parseInt(document.getElementById('edit-shot-slot').value || '1', 10);
  const fd = new FormData(); fd.append('file', f); fd.append('slot', String(slot));
  const r = await fetch('/admin/plugins/' + encodeURIComponent(_editingPlugin) + '/screenshots',
    {{method:'POST',headers:{{'x-admin-token':_token}},body:fd}});
  document.getElementById('edit-plugin-result').textContent = r.ok ? `Скриншот #${{slot}} загружен` : 'Ошибка';
}}

async function deleteScreenshot() {{
  if (!_editingPlugin) return;
  const slot = parseInt(document.getElementById('edit-shot-slot').value || '1', 10);
  const r = await fetch('/admin/plugins/' + encodeURIComponent(_editingPlugin) + '/screenshots/' + slot,
    {{method:'DELETE',headers:{{'x-admin-token':_token}}}});
  document.getElementById('edit-plugin-result').textContent = r.ok ? `Скриншот #${{slot}} удалён` : 'Ошибка';
}}

async function loadPluginReviewsAdmin(pid) {{
  const wrap = document.getElementById('edit-plugin-reviews');
  const r = await fetch('/admin/plugins/' + encodeURIComponent(pid) + '/reviews',
    {{headers:{{'x-admin-token':_token}}}});
  if (!r.ok) {{ wrap.innerHTML = '<div class="err" style="display:block">Не удалось загрузить</div>'; return; }}
  const d = await r.json();
  const list = d.reviews || [];
  if (!list.length) {{ wrap.innerHTML = '<div style="color:var(--dim);font-size:11px">Отзывов пока нет</div>'; return; }}
  wrap.innerHTML = list.map(rv => `
    <div class="plugin-row" style="margin-bottom:4px">
      <div>
        <div class="pid">${{'★'.repeat(Math.max(0,Math.min(5,rv.rating||0)))}}${{'☆'.repeat(5-Math.max(0,Math.min(5,rv.rating||0)))}} <span style="color:var(--dim);font-weight:400">${{_esc(rv.author||'Аноним')}}</span></div>
        <div class="pmeta" style="color:var(--text);margin-top:2px;white-space:pre-wrap">${{_esc(rv.text||'')}}</div>
      </div>
      <button class="btn btn-danger btn-sm" onclick="deleteReview('${{_jsAttr(rv.id||'')}}')">×</button>
    </div>
  `).join('');
}}

function _esc(s) {{
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}
function _jsAttr(s) {{
  return String(s == null ? '' : s).replace(/\\\\/g,'\\\\\\\\').replace(/'/g,'\\\\x27');
}}

async function deleteReview(rid) {{
  if (!_editingPlugin) return;
  if (!confirm('Удалить отзыв?')) return;
  const r = await fetch('/admin/plugins/' + encodeURIComponent(_editingPlugin) + '/reviews/' + encodeURIComponent(rid),
    {{method:'DELETE',headers:{{'x-admin-token':_token}}}});
  if (r.ok) loadPluginReviewsAdmin(_editingPlugin);
}}
async function uploadPlugin() {{
  const file = document.getElementById('plugin-file').files[0];
  if (!file) {{ showResult('result3', 'Выберите zip файл', false); return; }}
  const fd = new FormData();
  fd.append('file', file);
  showResult('result3', 'Загружаю...', true);
  const r = await fetch('/admin/plugins/upload', {{method:'POST',headers:{{'x-admin-token':_token}},body:fd}});
  const d = await r.json();
  if (r.ok) {{
    showResult('result3', `Загружен ${{d.plugin.name}} v${{d.plugin.version}}`, true);
    document.getElementById('plugin-file').value = '';
    refreshPlugins();
  }} else {{ showResult('result3', d.detail||'Ошибка', false); }}
}}
async function deletePlugin(id) {{
  if (!confirm('Удалить плагин ' + id + '?')) return;
  const r = await fetch('/admin/plugins/' + encodeURIComponent(id), {{
    method:'DELETE', headers:{{'x-admin-token':_token}}
  }});
  if (r.ok) refreshPlugins();
}}
async function uploadUpdate() {{
  const file = document.getElementById('update-file').files[0];
  if (!file) {{ showResult('result2', 'Выберите файл', false); return; }}
  const ver = document.getElementById('new-version').value.trim() || '1.0.0';
  const cl  = document.getElementById('changelog').value.trim();
  const fd = new FormData();
  fd.append('file', file);
  fd.append('version', ver);
  fd.append('changelog', cl);
  showResult('result2', 'Загружаю...', true);
  const r = await fetch('/admin/upload', {{method:'POST',headers:{{'x-admin-token':_token}},body:fd}});
  const d = await r.json();
  if (r.ok) {{ showResult('result2', `Загружено v${{d.version}}`, true); }}
  else {{ showResult('result2', d.detail||'Ошибка', false); }}
}}
async function deleteUpdate() {{
  if (!confirm('Удалить?')) return;
  const r = await fetch('/admin/delete', {{method:'DELETE',headers:{{'x-admin-token':_token}}}});
  const d = await r.json();
  showResult('result2', d.ok?'Удалено':'Ошибка', d.ok);
}}
function showResult(id, msg, ok) {{
  const el = document.getElementById(id);
  el.textContent = msg; el.className = ok?'ok':'err'; el.style.display='block';
}}
document.getElementById('admin-token').addEventListener('keydown', e => {{ if(e.key==='Enter') authCheck(); }});
</script>
</body>
</html>"""

@app.on_event("startup")
def _on_startup():
    """Стартовая инициализация: поднимаем keepalive-поток и восстанавливаем
    воркеры всех активных аккаунтов после рестарта."""
    if FW_AVAILABLE:
        try:
            fw.start_keepalive_thread()
            fw.restore_workers()
            print(f"[startup] funpay_worker запущен, активных воркеров: {len(fw.list_active())}")
        except Exception as e:
            print(f"[startup] funpay_worker init failed: {e}")


if __name__ == "__main__":
    print(f"[UpdateServer] Starting on port {PORT}")
    print(f"[UpdateServer] Admin panel: http://0.0.0.0:{PORT}/admin")
    print(f"[UpdateServer] ADMIN_TOKEN: {ADMIN_TOKEN}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
