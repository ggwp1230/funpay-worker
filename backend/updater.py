"""
Модуль авто-обновления.
Проверяет наличие обновлений на VPS, скачивает и применяет их.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional, Callable

import requests

APP_VERSION_FILE = Path(__file__).parent / "version.json"
BACKUP_DIR       = Path(__file__).parent / "backup"


def get_local_version() -> str:
    try:
        return json.loads(APP_VERSION_FILE.read_text()).get("version", "0.0.0")
    except Exception:
        return "0.0.0"


def save_local_version(version: str):
    APP_VERSION_FILE.write_text(json.dumps({"version": version}))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def version_gt(a: str, b: str) -> bool:
    """Возвращает True если версия a > b."""
    def parse(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except Exception:
            return (0,)
    return parse(a) > parse(b)


class Updater:
    def __init__(self, server_url: str, access_token: str,
                 on_log: Optional[Callable] = None):
        self.server_url   = server_url.rstrip("/")
        self.access_token = access_token
        self.on_log       = on_log or print
        self.session      = requests.Session()
        self.session.headers["X-Token"] = access_token

    def _log(self, msg: str):
        self.on_log(msg)

    def ping(self) -> tuple[bool, str]:
        """Проверка доступности сервера."""
        try:
            r = self.session.get(f"{self.server_url}/ping", timeout=5)
            return r.status_code == 200, ""
        except Exception as e:
            return False, str(e)

    def check_update(self) -> tuple[bool, dict]:
        """
        Возвращает (has_update: bool, meta: dict).
        meta содержит version, changelog, size, sha256.
        """
        try:
            r = self.session.get(
                f"{self.server_url}/version",
                headers={"X-Token": self.access_token},
                timeout=10
            )
            if r.status_code == 403:
                return False, {"error": "Неверный токен доступа"}
            if r.status_code != 200:
                return False, {"error": f"HTTP {r.status_code}"}
            meta = r.json()
            remote_ver = meta.get("version", "0.0.0")
            local_ver  = get_local_version()
            has_update = version_gt(remote_ver, local_ver)
            meta["local_version"]  = local_ver
            meta["remote_version"] = remote_ver
            return has_update, meta
        except Exception as e:
            return False, {"error": str(e)}

    def download_and_apply(self,
                           progress_cb: Optional[Callable[[str, int, int], None]] = None
                           ) -> tuple[bool, str]:
        """
        Скачивает архив с сервера, проверяет хэш, применяет обновление.
        Возвращает (success, message).
        progress_cb(stage, done, total) — колбэк прогресса.
        stage: 'downloading' | 'backing_up' | 'extracting'.
        """
        has_update, meta = self.check_update()
        if meta.get("error"):
            return False, meta["error"]
        if not has_update:
            return False, f"Обновлений нет (текущая: {meta.get('local_version')})"

        remote_ver = meta["remote_version"]
        expected_hash = meta.get("sha256", "")
        self._log(f"Скачиваю обновление v{remote_ver}...")

        try:
            r = self.session.get(
                f"{self.server_url}/download",
                headers={"X-Token": self.access_token},
                stream=True, timeout=60
            )
            if r.status_code == 403:
                return False, "Неверный токен доступа"
            if r.status_code != 200:
                return False, f"Ошибка скачивания: HTTP {r.status_code}"

            total = int(r.headers.get("content-length", 0))
            downloaded = 0

            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                for chunk in r.iter_content(65536):
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total:
                        progress_cb("downloading", downloaded, total)

        except Exception as e:
            return False, f"Ошибка скачивания: {e}"

        # Проверяем хэш
        if expected_hash:
            try:
                actual_hash = sha256_file(tmp_path)
            except Exception as e:
                tmp_path.unlink(missing_ok=True)
                return False, f"Не удалось проверить хэш: {e}"
            if actual_hash != expected_hash:
                tmp_path.unlink(missing_ok=True)
                return False, "Ошибка целостности файла (SHA256 не совпадает)"
            self._log("Хэш файла проверен")

        # Применяем обновление
        try:
            ok, msg = self._apply_update(tmp_path, remote_ver, progress_cb=progress_cb)
        except Exception as e:
            ok, msg = False, f"Ошибка при применении обновления: {e}"
        finally:
            tmp_path.unlink(missing_ok=True)
        return ok, msg

    def _apply_update(self, zip_path: Path, new_version: str,
                      progress_cb: Optional[Callable[[str, int, int], None]] = None
                      ) -> tuple[bool, str]:
        """Распаковывает архив поверх текущих файлов (кроме config/ и FunPayAPI/)."""
        app_root = Path(__file__).parent.parent  # project root

        # В прод-сборке Electron-фронт лежит в app.asar.unpacked/electron/...
        # рядом с backend/. В dev — обычный layout с папкой electron/ в корне.
        asar_unpacked = app_root / "app.asar.unpacked"
        prod_layout = asar_unpacked.exists() and asar_unpacked.is_dir()

        def resolve_path(rel_path: str) -> Path:
            """Куда писать/читать файл относительно реальной структуры на диске."""
            rel = rel_path.replace("\\", "/")
            if prod_layout and rel.startswith("electron/"):
                return asar_unpacked / rel
            return app_root / rel

        # Папки, которые НЕ трогаем при обновлении
        PROTECTED = {
            "backend/config",
            "backend/logs",
            "backend/backup",
            "backend/FunPayAPI",
            "backend/version.json",
            "backend/plugins_data",
        }

        def is_protected(rel_path: str) -> bool:
            rel = rel_path.replace("\\", "/")
            return any(rel.startswith(p) for p in PROTECTED)

        # ── Бэкап только тех файлов, которые будут перезаписаны ─────────────────
        # Раньше зипали ВЕСЬ app_root (electron/dist/node_modules/...) что
        # подвисало на минуту+. Теперь бэкапим только реально затрагиваемые файлы.
        try:
            BACKUP_DIR.mkdir(exist_ok=True)
            backup_file = BACKUP_DIR / f"backup_{int(time.time())}.zip"
            self._log("Создаю резервную копию...")

            with zipfile.ZipFile(zip_path, "r") as zf:
                members = [m for m in zf.namelist()
                           if not m.endswith("/") and not is_protected(m)]

            existing = [m for m in members if resolve_path(m).is_file()]
            total_b = len(existing) or 1
            if progress_cb:
                progress_cb("backing_up", 0, total_b)

            with zipfile.ZipFile(backup_file, "w", zipfile.ZIP_DEFLATED) as bz:
                for i, member in enumerate(existing, 1):
                    src_path = resolve_path(member)
                    try:
                        bz.write(src_path, member)
                    except Exception as e:
                        self._log(f"Не смог забэкапить {member}: {e}")
                    if progress_cb:
                        progress_cb("backing_up", i, total_b)
        except Exception as e:
            self._log(f"Предупреждение: не удалось создать бэкап: {e}")

        # ── Распаковываем ──────────────────────────────────────────────────────
        try:
            self._log("Применяю обновление...")
            with zipfile.ZipFile(zip_path, "r") as zf:
                members = [m for m in zf.namelist() if not is_protected(m)]
                total_e = len(members) or 1
                if progress_cb:
                    progress_cb("extracting", 0, total_e)

                for i, member in enumerate(members, 1):
                    dest = resolve_path(member)
                    if member.endswith("/"):
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    if progress_cb:
                        progress_cb("extracting", i, total_e)

            save_local_version(new_version)
            self._log(f"Обновление v{new_version} применено успешно!")
            return True, f"Обновлено до v{new_version}. Перезапустите приложение."

        except PermissionError as e:
            return False, (f"Файл заблокирован (закройте приложение и попробуйте "
                           f"снова): {e}")
        except Exception as e:
            return False, f"Ошибка при применении обновления: {e}"
