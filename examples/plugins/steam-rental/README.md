# steam-rental

Пример плагина для FP Nexus, демонстрирующий хуки `on_order_paid` и `on_timer`.

## Что делает

При оплате заказа, в названии которого есть одно из ключевых слов (по умолчанию `steam`), плагин:

1. Берёт из пула первый свободный аккаунт.
2. Отправляет покупателю в чат логин/пароль и время до возврата.
3. Запоминает аренду в `ctx.storage`.
4. Через `duration_hours` часов помечает аккаунт как свободный и пишет покупателю что аренда закончилась.

## Структура

```
plugin.json   — метаданные + config_schema (форма настроек авто-генерится в UI)
main.py       — класс Plugin(ctx) с методами on_order_paid и on_timer
```

## Сборка и заливка на VPS

Чтобы превратить эту папку в `.zip` для загрузки в админку:

```bash
cd examples/plugins/steam-rental
zip -r ../steam-rental.zip plugin.json main.py
```

или одной командой из корня репо:

```bash
python3 -c "
import zipfile, pathlib
src = pathlib.Path('examples/plugins/steam-rental')
with zipfile.ZipFile('steam-rental.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for f in src.iterdir():
        if f.is_file() and not f.name.endswith(('.md', '.pyc')):
            z.write(f, f.name)
print('OK', pathlib.Path('steam-rental.zip').stat().st_size, 'bytes')
"
```

Потом залить через `/admin` → вкладка «Плагины» → «Загрузить .zip».

После этого у клиентов в приложении на странице «Плагины» в секции «Магазин» появится «Steam Rental», который можно установить, заполнить настройки и включить.

## Настройка

Все поля авто-генерируются из `config_schema` в `plugin.json`. Доступны:

| ключ | тип | смысл |
|---|---|---|
| `accounts` | textarea | Пул аккаунтов в формате `login:password` по одной паре в строке |
| `keywords` | text | Список ключевых слов через запятую — если ни одно не встречается в названии заказа, плагин его игнорирует |
| `duration_hours` | number | Сколько часов покупатель пользуется аккаунтом |
| `give_message` | textarea | Шаблон сообщения при выдаче. Плейсхолдеры: `{login}`, `{password}`, `{until}`, `{hours}`, `{order_id}`, `{buyer}` |
| `return_message` | textarea | Что отправить когда время вышло |
| `no_accounts_message` | textarea | Что отправить если все аккаунты заняты |

## Ограничения MVP

- **Активные таймеры не переживают рестарт бэкенда.** Если бот перезапустился во время чьей-то аренды, плагин не вернёт аккаунт в пул автоматически — придётся вручную удалить запись из `backend/plugins_data/steam-rental/storage.json`. Это починится в будущей версии (persistent timers).
- **Нет sandbox.** Плагин это обычный Python-модуль с полным доступом к процессу. Не загружай на VPS чужие плагины не глядя в код.
- **Нет фильтра по категории.** Текущая логика смотрит только на текст в названии заказа. Если хочешь по `category_id` — допиши условие в `on_order_paid`.

## Как тестировать

1. Установи плагин (через UI или вручную распакуй в `backend/plugins_data/steam-rental/`).
2. Включи его и заполни `accounts`, например:
   ```
   testuser1:hunter2
   testuser2:correcthorsebatterystaple
   ```
3. Поставь `duration_hours = 1` (или временно меньше — но min=1).
4. На FunPay создай тестовый заказ с `steam` в названии и оплати его.
5. В логах должно появиться `Выдан аккаунт testuser1 ...`, в чате покупателя — логин/пароль.
6. Через час придёт сообщение про возврат, аккаунт снова свободен.

Для отладки можешь временно изменить таймер на короткий — поправь в `main.py`:
```python
self.ctx.schedule(30, "return", {"login": login})  # 30 секунд вместо часов
```
