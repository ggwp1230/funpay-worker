"""
steam-rental — пример плагина для FP Nexus.

Демонстрирует:
  • on_order_paid — реакция на оплаченный заказ
  • on_timer       — отложенный возврат аккаунта в пул
  • ctx.storage    — JSON-хранилище для активных аренд и состояния пула
  • ctx.config     — пользовательские настройки (config_schema в plugin.json)
  • ctx.send_message — отправка сообщения в чат FunPay
  • ctx.schedule   — постановка таймера

Логика:
  1. При оплате заказа смотрим, подходит ли он по ключевым словам в названии.
  2. Берём из пула первый аккаунт, которого нет в active_rentals.
  3. Шлём покупателю логин/пароль.
  4. Записываем аренду в storage.active_rentals[login] = {order_id, chat_id, ...}.
  5. Через duration_hours срабатывает таймер: чистим запись и пишем покупателю.

Состояние НЕ хранится в process memory — всё через ctx.storage, поэтому
переживает перезапуск бэкенда (но активные таймеры на момент рестарта
не восстанавливаются — это особенность MVP, см. README).
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx
        ctx.log_info("steam-rental загружен")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _parse_accounts(self) -> list[tuple[str, str]]:
        raw = self.ctx.config.get("accounts", "") or ""
        out: list[tuple[str, str]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            login, _, password = line.partition(":")
            login = login.strip()
            password = password.strip()
            if login and password:
                out.append((login, password))
        return out

    def _keywords(self) -> list[str]:
        raw = self.ctx.config.get("keywords", "steam") or "steam"
        return [k.strip().lower() for k in raw.split(",") if k.strip()]

    def _duration_hours(self) -> int:
        v = self.ctx.config.get("duration_hours", 6)
        try:
            v = int(v)
        except Exception:
            v = 6
        return max(1, min(168, v))

    def _active_rentals(self) -> dict:
        return self.ctx.storage.get("active_rentals", {}) or {}

    def _save_active_rentals(self, data: dict) -> None:
        self.ctx.storage.set("active_rentals", data)

    def _pick_free_account(self) -> Optional[tuple[str, str]]:
        active = self._active_rentals()
        for login, password in self._parse_accounts():
            if login not in active:
                return login, password
        return None

    def _format(self, template: str, **values) -> str:
        try:
            return template.format(**values)
        except Exception:
            # Если в шаблоне опечатка типа {логин} — отдадим сырой текст,
            # чтобы плагин не падал на пользователе.
            return template

    # ── Hooks ─────────────────────────────────────────────────────────────────

    def on_order_paid(self, order) -> None:
        title = (getattr(order, "title", "") or "").lower()
        order_id = str(getattr(order, "id", "?"))
        buyer = getattr(order, "buyer_username", "?") or "?"
        chat_id = (getattr(order, "chat_id", None)
                   or getattr(order, "buyer_id", None))

        keywords = self._keywords()
        if keywords and not any(k in title for k in keywords):
            return  # не наш заказ — молча пропускаем

        if not chat_id:
            self.ctx.log_error(
                f"Не нашёл chat_id в заказе #{order_id} — пропускаю выдачу")
            return

        account = self._pick_free_account()
        if account is None:
            self.ctx.log_info(
                f"Заказ #{order_id} от {buyer}: все аккаунты заняты")
            self.ctx.send_message(
                chat_id=chat_id,
                text=self.ctx.config.get(
                    "no_accounts_message",
                    "Все аккаунты сейчас заняты — продавец скоро напишет вручную."),
            )
            return

        login, password = account
        hours = self._duration_hours()
        until_ts = time.time() + hours * 3600
        until_str = datetime.fromtimestamp(until_ts).strftime("%d.%m %H:%M")

        msg = self._format(
            self.ctx.config.get("give_message",
                                "Логин: {login}\nПароль: {password}"),
            login=login,
            password=password,
            until=until_str,
            hours=hours,
            order_id=order_id,
            buyer=buyer,
        )
        ok = self.ctx.send_message(chat_id=chat_id, text=msg)
        if not ok:
            self.ctx.log_error(
                f"Не смог отправить логин/пароль для заказа #{order_id}")
            return

        active = self._active_rentals()
        active[login] = {
            "order_id": order_id,
            "buyer": buyer,
            "chat_id": chat_id,
            "started_at": time.time(),
            "until_ts": until_ts,
            "hours": hours,
        }
        self._save_active_rentals(active)
        self.ctx.log_info(
            f"Выдан аккаунт {login} покупателю {buyer} (заказ #{order_id}) на {hours}ч")

        # Таймер на возврат
        self.ctx.schedule(hours * 3600, "return", {"login": login})

    def on_timer(self, name: str, data: dict) -> None:
        if name != "return":
            return
        login = (data or {}).get("login")
        if not login:
            return

        active = self._active_rentals()
        rental = active.pop(login, None)
        if rental is None:
            # Уже вернули вручную или повторное срабатывание — ничего не делаем.
            return
        self._save_active_rentals(active)

        chat_id = rental.get("chat_id")
        if chat_id:
            self.ctx.send_message(
                chat_id=chat_id,
                text=self.ctx.config.get(
                    "return_message",
                    "Время аренды Steam-аккаунта подошло к концу. Спасибо!"),
            )
        self.ctx.log_info(
            f"Аккаунт {login} вернулся в пул "
            f"(был у {rental.get('buyer','?')}, заказ #{rental.get('order_id','?')})")

    def on_unload(self) -> None:
        # Таймеры PluginManager отменит сам. Тут только лог.
        self.ctx.log_info("steam-rental выгружен")

    def on_config_changed(self, values: dict) -> None:
        self.ctx.log_info("steam-rental: настройки обновлены")
