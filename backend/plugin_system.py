"""
FP Nexus — Plugin System
Загрузка пользовательских плагинов из backend/plugins_data/<id>/ и
управление их жизненным циклом (install/uninstall, enable/disable, hooks).

Плагин — папка с двумя файлами:

    plugin.json   — метаданные (id, name, version, hooks, config_schema)
    main.py       — модуль с классом Plugin, который инстанциируется с ctx

Плагины распространяются с VPS как .zip и устанавливаются клиентом через
скачивание с /plugins/<id>/download. Этот модуль ничего про сеть не знает —
он просто принимает уже скачанные байты или путь к zip.
"""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import re
import sys
import threading
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("FPNexus.plugins")

PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


# ── Storage / Config / Context ────────────────────────────────────────────────

class PluginStorage:
    """JSON-хранилище ключ-значение, скоупленное на конкретный плагин."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("Не удалось прочитать %s — стираю", path)
                self._data = {}

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self._path)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._flush()

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
            self._flush()

    def all(self) -> dict:
        with self._lock:
            return dict(self._data)


class PluginConfig:
    """Read-only view над settings.plugins.config[<id>]."""

    def __init__(self, get_root_config: Callable[[], dict], plugin_id: str):
        self._get_root = get_root_config
        self._pid = plugin_id

    def _slice(self) -> dict:
        cfg = self._get_root() or {}
        return ((cfg.get("plugins") or {}).get("config") or {}).get(self._pid) or {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._slice().get(key, default)

    def all(self) -> dict:
        return self._slice()


@dataclass
class PluginContext:
    """То, что мы передаём плагину при инициализации (ctx)."""
    plugin_id: str
    storage: PluginStorage
    config: PluginConfig
    send_message: Callable[..., Any]
    log_info: Callable[[str], None]
    log_error: Callable[[str], None]
    schedule: Callable[[float, str, Optional[dict]], str]
    cancel_timer: Callable[[str], None]


# ── Loaded plugin record ──────────────────────────────────────────────────────

@dataclass
class LoadedPlugin:
    meta: dict
    instance: Any = None
    enabled: bool = False
    error: Optional[str] = None
    storage_path: Path = field(default=None)  # type: ignore


# ── Plugin Manager ────────────────────────────────────────────────────────────

class PluginManager:
    """
    Управляет жизненным циклом всех плагинов клиента.

    Не делает HTTP-запросов сам — для скачивания плагина используй install_zip.
    """

    def __init__(self,
                 plugins_dir: Path,
                 get_config: Callable[[], dict],
                 save_config: Callable[[dict], None],
                 send_message_fn: Callable[..., Any],
                 log_event: Callable[[str, str, str], None]):
        self.plugins_dir = Path(plugins_dir)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self._get_config = get_config
        self._save_config = save_config
        self._send_message_fn = send_message_fn
        self._log_event = log_event  # signature: (level, category, message)

        self._lock = threading.RLock()
        self._plugins: Dict[str, LoadedPlugin] = {}
        # active threading.Timer-ы, ключ — timer_id, значение — (timer, plugin_id)
        self._timers: Dict[str, tuple] = {}

    # ── Persistence helpers ───────────────────────────────────────────────────

    def _enabled_set(self) -> set[str]:
        cfg = self._get_config() or {}
        return set((cfg.get("plugins") or {}).get("enabled") or [])

    def _set_enabled(self, plugin_id: str, enabled: bool) -> None:
        cfg = self._get_config() or {}
        plugins_cfg = dict(cfg.get("plugins") or {})
        enabled_list = list(plugins_cfg.get("enabled") or [])
        if enabled and plugin_id not in enabled_list:
            enabled_list.append(plugin_id)
        elif not enabled and plugin_id in enabled_list:
            enabled_list.remove(plugin_id)
        plugins_cfg["enabled"] = enabled_list
        cfg["plugins"] = plugins_cfg
        self._save_config(cfg)

    def _set_plugin_config(self, plugin_id: str, values: dict) -> None:
        cfg = self._get_config() or {}
        plugins_cfg = dict(cfg.get("plugins") or {})
        configs = dict(plugins_cfg.get("config") or {})
        configs[plugin_id] = values
        plugins_cfg["config"] = configs
        cfg["plugins"] = plugins_cfg
        self._save_config(cfg)

    def _drop_plugin_config(self, plugin_id: str) -> None:
        cfg = self._get_config() or {}
        plugins_cfg = dict(cfg.get("plugins") or {})
        configs = dict(plugins_cfg.get("config") or {})
        configs.pop(plugin_id, None)
        plugins_cfg["config"] = configs
        enabled_list = [p for p in (plugins_cfg.get("enabled") or [])
                        if p != plugin_id]
        plugins_cfg["enabled"] = enabled_list
        cfg["plugins"] = plugins_cfg
        self._save_config(cfg)

    # ── Discovery ─────────────────────────────────────────────────────────────

    def list_installed(self) -> List[dict]:
        """Возвращает метаданные всех установленных плагинов + их состояние."""
        with self._lock:
            out = []
            enabled = self._enabled_set()
            for pid in sorted(self._iter_dirs()):
                meta = self._read_meta(pid) or {"id": pid, "name": pid,
                                                "version": "?"}
                rec = self._plugins.get(pid)
                out.append({
                    **meta,
                    "enabled": pid in enabled,
                    "loaded": bool(rec and rec.instance),
                    "error": rec.error if rec else None,
                })
            return out

    def _iter_dirs(self) -> List[str]:
        if not self.plugins_dir.exists():
            return []
        return [p.name for p in self.plugins_dir.iterdir()
                if p.is_dir() and PLUGIN_ID_RE.match(p.name)
                and (p / "plugin.json").exists()]

    def _read_meta(self, plugin_id: str) -> Optional[dict]:
        f = self.plugins_dir / plugin_id / "plugin.json"
        if not f.exists():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Невалидный plugin.json у %s", plugin_id)
            return None

    # ── Install / Uninstall ───────────────────────────────────────────────────

    def install_zip(self, zip_bytes: bytes) -> dict:
        """Установка плагина из bytes zip-архива. Возвращает meta."""
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
        except zipfile.BadZipFile as e:
            raise ValueError(f"Не zip-архив: {e}")

        with zf:
            try:
                meta_raw = zf.read("plugin.json")
            except KeyError:
                raise ValueError("В архиве нет plugin.json")
            try:
                meta = json.loads(meta_raw)
            except Exception as e:
                raise ValueError(f"plugin.json не парсится: {e}")

            pid = str(meta.get("id", "")).strip()
            if not PLUGIN_ID_RE.match(pid):
                raise ValueError("Невалидный id плагина")

            target = self.plugins_dir / pid
            with self._lock:
                # Если плагин был загружен — выгружаем
                self._unload_locked(pid)
                # Стираем старую папку
                if target.exists():
                    _rmtree(target)
                target.mkdir(parents=True, exist_ok=True)
                # Распаковываем — все файлы внутри одного zip считаем безопасными
                # (id плагина и так провалидирован, path traversal внутри —
                # отдельная история, проверим имена)
                for name in zf.namelist():
                    if name.endswith("/"):
                        continue
                    safe = name.replace("\\", "/")
                    if safe.startswith("/") or ".." in safe.split("/"):
                        raise ValueError(f"Опасное имя файла в архиве: {name}")
                    dest = target / safe
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(name))

        self._log_event("info", "plugins",
                        f"Плагин {pid} v{meta.get('version', '?')} установлен")
        return meta

    def uninstall(self, plugin_id: str) -> None:
        if not PLUGIN_ID_RE.match(plugin_id):
            raise ValueError("Невалидный id")
        with self._lock:
            self._unload_locked(plugin_id)
            self._drop_plugin_config(plugin_id)
            target = self.plugins_dir / plugin_id
            if target.exists():
                _rmtree(target)
        self._log_event("info", "plugins", f"Плагин {plugin_id} удалён")

    # ── Load / Unload ─────────────────────────────────────────────────────────

    def load_all_enabled(self) -> None:
        """Грузит все плагины, помеченные как enabled в config."""
        for pid in self._enabled_set():
            try:
                self.load(pid)
            except Exception as e:
                logger.exception("Не смог загрузить плагин %s", pid)
                self._log_event("error", "plugins",
                                f"Плагин {pid} не загрузился: {e}")

    def load(self, plugin_id: str) -> LoadedPlugin:
        if not PLUGIN_ID_RE.match(plugin_id):
            raise ValueError("Невалидный id")

        with self._lock:
            self._unload_locked(plugin_id)

            meta = self._read_meta(plugin_id)
            if meta is None:
                raise FileNotFoundError(
                    f"Плагин {plugin_id} не установлен или сломан plugin.json")

            rec = LoadedPlugin(meta=meta, enabled=False)
            rec.storage_path = self.plugins_dir / plugin_id / "storage.json"

            entry = (self.plugins_dir / plugin_id / "main.py")
            if not entry.exists():
                rec.error = "main.py не найден"
                self._plugins[plugin_id] = rec
                raise FileNotFoundError("main.py отсутствует")

            try:
                module = self._import_main(plugin_id, entry)
                if not hasattr(module, "Plugin"):
                    raise AttributeError(
                        "В main.py должен быть класс Plugin(ctx)")
                ctx = self._make_ctx(plugin_id, rec.storage_path)
                rec.instance = module.Plugin(ctx)  # type: ignore
                rec.enabled = True
                self._plugins[plugin_id] = rec
                self._log_event("info", "plugins",
                                f"Плагин {plugin_id} v{meta.get('version','?')} загружен")
                return rec
            except Exception as e:
                rec.error = f"{type(e).__name__}: {e}"
                self._plugins[plugin_id] = rec
                logger.exception("Plugin load failed: %s", plugin_id)
                raise

    def _unload_locked(self, plugin_id: str) -> None:
        rec = self._plugins.pop(plugin_id, None)
        # Кенселим все таймеры этого плагина
        to_cancel = [tid for tid, (_, pid) in self._timers.items()
                     if pid == plugin_id]
        for tid in to_cancel:
            timer, _ = self._timers.pop(tid, (None, None))
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
        if rec and rec.instance is not None:
            on_unload = getattr(rec.instance, "on_unload", None)
            if callable(on_unload):
                try:
                    on_unload()
                except Exception:
                    logger.exception("on_unload error in %s", plugin_id)
        # Чистим импортированный модуль
        mod_name = self._module_name(plugin_id)
        sys.modules.pop(mod_name, None)

    def reload(self, plugin_id: str) -> LoadedPlugin:
        return self.load(plugin_id)

    def reload_all(self) -> None:
        for pid in list(self._plugins.keys()):
            try:
                self.reload(pid)
            except Exception:
                logger.exception("reload failed: %s", pid)

    @staticmethod
    def _module_name(plugin_id: str) -> str:
        # подчёркивания вместо тире, плюс префикс — чтобы не схлопнулось с
        # реальными модулями
        return "_fpnexus_plugin_" + plugin_id.replace("-", "_")

    def _import_main(self, plugin_id: str, entry: Path):
        mod_name = self._module_name(plugin_id)
        # Полный re-import: убираем закэшированную версию
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(mod_name, str(entry))
        if spec is None or spec.loader is None:
            raise ImportError("spec_from_file_location вернул None")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module

    # ── Enable / Disable ──────────────────────────────────────────────────────

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        if not PLUGIN_ID_RE.match(plugin_id):
            raise ValueError("Невалидный id")
        if not (self.plugins_dir / plugin_id / "plugin.json").exists():
            raise FileNotFoundError("Плагин не установлен")
        self._set_enabled(plugin_id, enabled)
        with self._lock:
            if enabled:
                try:
                    self.load(plugin_id)
                except Exception as e:
                    self._log_event("error", "plugins",
                                    f"Включил {plugin_id}, но загрузить не вышло: {e}")
                    raise
            else:
                self._unload_locked(plugin_id)
                self._log_event("info", "plugins",
                                f"Плагин {plugin_id} отключён")

    # ── Configuration ─────────────────────────────────────────────────────────

    def set_config(self, plugin_id: str, values: dict) -> None:
        if not PLUGIN_ID_RE.match(plugin_id):
            raise ValueError("Невалидный id")
        self._set_plugin_config(plugin_id, values)
        # Дёрнем on_config_changed если плагин её реализует
        with self._lock:
            rec = self._plugins.get(plugin_id)
            if rec and rec.instance is not None:
                cb = getattr(rec.instance, "on_config_changed", None)
                if callable(cb):
                    try:
                        cb(values)
                    except Exception:
                        logger.exception("on_config_changed error in %s",
                                         plugin_id)

    def get_config(self, plugin_id: str) -> dict:
        cfg = self._get_config() or {}
        return ((cfg.get("plugins") or {}).get("config") or {}).get(plugin_id) or {}

    # ── Context construction ──────────────────────────────────────────────────

    def _make_ctx(self, plugin_id: str, storage_path: Path) -> PluginContext:
        storage = PluginStorage(storage_path)
        config = PluginConfig(self._get_config, plugin_id)

        def _send(chat_id: int, text: str, chat_name: Optional[str] = None,
                  interlocutor_id: Optional[int] = None):
            return self._send_message_fn(chat_id=chat_id, text=text,
                                         chat_name=chat_name,
                                         interlocutor_id=interlocutor_id,
                                         _from_plugin=plugin_id)

        def _info(msg: str):
            self._log_event("info", f"plugin:{plugin_id}", msg)

        def _error(msg: str):
            self._log_event("error", f"plugin:{plugin_id}", msg)

        def _schedule(seconds: float, name: str,
                      data: Optional[dict] = None) -> str:
            return self._schedule_timer(plugin_id, seconds, name, data or {})

        def _cancel(timer_id: str) -> None:
            self._cancel_timer(timer_id)

        return PluginContext(
            plugin_id=plugin_id,
            storage=storage,
            config=config,
            send_message=_send,
            log_info=_info,
            log_error=_error,
            schedule=_schedule,
            cancel_timer=_cancel,
        )

    # ── Timers ────────────────────────────────────────────────────────────────

    def _schedule_timer(self, plugin_id: str, seconds: float,
                        name: str, data: dict) -> str:
        tid = uuid.uuid4().hex[:12]

        def _fire():
            self._timers.pop(tid, None)
            self._dispatch_timer(plugin_id, name, data)

        timer = threading.Timer(max(0.0, float(seconds)), _fire)
        timer.daemon = True
        with self._lock:
            self._timers[tid] = (timer, plugin_id)
        timer.start()
        return tid

    def _cancel_timer(self, timer_id: str) -> None:
        with self._lock:
            entry = self._timers.pop(timer_id, None)
        if entry:
            timer, _ = entry
            try:
                timer.cancel()
            except Exception:
                pass

    def _dispatch_timer(self, plugin_id: str, name: str, data: dict) -> None:
        rec = self._plugins.get(plugin_id)
        if not rec or rec.instance is None:
            return
        cb = getattr(rec.instance, "on_timer", None)
        if callable(cb):
            try:
                cb(name, data)
            except Exception:
                logger.exception("on_timer error in %s", plugin_id)
                self._log_event("error", f"plugin:{plugin_id}",
                                f"on_timer упал: {traceback.format_exc(limit=2)}")

    # ── Hook dispatch ─────────────────────────────────────────────────────────

    def dispatch_message(self, msg: Any) -> List[str]:
        """Вызывает on_message у всех загруженных плагинов.
        Возвращает список ответов, которые плагины захотели отправить
        (на случай если автор главного цикла сам захочет ими распорядиться)."""
        replies: List[str] = []
        for pid, rec in list(self._plugins.items()):
            if rec.instance is None:
                continue
            cb = getattr(rec.instance, "on_message", None)
            if not callable(cb):
                continue
            try:
                ret = cb(msg)
                if isinstance(ret, str) and ret.strip():
                    replies.append(ret)
            except Exception:
                logger.exception("on_message error in %s", pid)
                self._log_event("error", f"plugin:{pid}",
                                f"on_message упал: {traceback.format_exc(limit=2)}")
        return replies

    def dispatch_order_paid(self, order: Any) -> None:
        for pid, rec in list(self._plugins.items()):
            if rec.instance is None:
                continue
            cb = getattr(rec.instance, "on_order_paid", None)
            if not callable(cb):
                continue
            try:
                cb(order)
            except Exception:
                logger.exception("on_order_paid error in %s", pid)
                self._log_event("error", f"plugin:{pid}",
                                f"on_order_paid упал: {traceback.format_exc(limit=2)}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rmtree(path: Path) -> None:
    """Удаляет дерево, переживает залоченные файлы (best-effort)."""
    import shutil
    if not path.exists():
        return
    shutil.rmtree(path, ignore_errors=True)
