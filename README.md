# Telethon Sender + Manager Bot

Проект состоит из двух процессов с общей SQLite-базой:

- `sender-userbot` на `Telethon` работает от имени пользовательского Telegram-аккаунта, подписывается на паблики, группы и каналы, рассылает случайное активное сообщение по глобальному расписанию и уведомляет владельцев о входящих личных сообщениях.
- `manager-bot` на `aiogram` управляет подписками, сообщениями, расписанием, whitelist и языком интерфейса через Telegram-бота.

## Возможности

- Добавление целей по `@username`, public `t.me` link или invite link.
- Поддержка forum-супергрупп: можно добавить ссылку на конкретный топик, например `https://t.me/groupname/123` или `https://t.me/c/1234567890/123`.
- Очередь попыток join со статусами `pending`, `joined`, `approval_pending`, `retry`, `error`.
- Пул из нескольких сообщений со случайным выбором одного активного текста на каждую рассылку.
- Глобальный интервал и настраиваемый случайный джиттер.
- Whitelist пользователей для доступа к `manager-bot`.
- Русская локализация менеджера по умолчанию и переключение языка в меню.
- Уведомления владельцам о личных сообщениях userbot-аккаунту.
- Docker Compose для развертывания обоих процессов на сервере.

## Переменные окружения

Скопируйте [.env.example](/G:/projects/tg-spam-agent/.env.example) в `.env` и заполните:

- `MANAGER_BOT_TOKEN` - токен бота-менеджера.
- `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` - значения с [my.telegram.org](https://my.telegram.org).
- `OWNER_IDS` - список Telegram user id через запятую.
- `DATABASE_PATH` - путь к SQLite-файлу.
- `TELETHON_SESSION_PATH` - путь для сохранения session userbot.
- `LOG_LEVEL` - уровень логирования.
- `SCHEDULER_POLL_SECONDS` - как часто sender проверяет очередь join и расписание.
- `DEFAULT_INTERVAL_MINUTES`, `DEFAULT_JITTER_MINUTES` - дефолтные значения при первом старте.

## Локальный запуск

Установить зависимости:

```bash
pip install -e .[dev]
```

Один раз создать Telethon session:

```bash
tg-spam-agent init-userbot-session
```

Запустить менеджера:

```bash
tg-spam-agent run-manager
```

Запустить sender-userbot:

```bash
tg-spam-agent run-sender
```

## Docker

Сначала авторизуйте userbot внутри контейнера и сохраните session в volume:

```bash
docker compose run --rm sender-userbot tg-spam-agent init-userbot-session
```

Потом поднимите оба сервиса:

```bash
docker compose up -d --build
```

Оба контейнера используют общий volume `tg_spam_agent_data`, где лежат SQLite-база, Telethon `.session` и runtime state.

## Управление через manager-bot

- `/start` или `/help` - главное меню.
- `Подписки` - добавить новую цель или конкретный топик forum-группы, повторить join, отключить, включить или удалить цель.
- `Сообщения` - добавить текст, отключить, включить или удалить сообщение.
- `Расписание` - поменять интервал, джиттер и общее состояние рассылки.
- `Whitelist` - добавить или удалить Telegram user id.
- `Статус` - сводка по сессии, целям, сообщениям и последним ошибкам доставки.
- `Язык` - переключить интерфейс менеджера между русским и английским. По умолчанию используется русский.

## Тесты

```bash
pytest
```
