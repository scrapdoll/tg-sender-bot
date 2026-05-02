# Telegram Multi-Tenant Sender SaaS

Telegram-only SaaS for managing per-customer Telethon sender userbots through one aiogram manager bot.

The product is multi-tenant:

- Each Telegram user gets a tenant on `/start`.
- Each tenant stores its own targets, message templates, schedule, inbound events, delivery logs, subscription, and encrypted Telethon `StringSession`.
- Platform admins are configured by `PLATFORM_ADMIN_IDS` and can edit the paid plan inside the manager bot.
- Customers pay with Telegram Stars. The active plan defaults to 500 Stars per 30 days, 100 targets, 10 templates, and a 30 minute minimum interval.
- Expired or inactive subscriptions keep data visible but pause joins and broadcasts.

## Runtime

Two processes are still used:

- `manager-bot`: customer UI, tenant creation, billing, plan admin settings, userbot onboarding.
- `sender-userbot`: worker loop that selects tenants with active subscriptions and connected sessions, then runs joins and broadcasts tenant by tenant.

Production storage is PostgreSQL. Supabase works through `DATABASE_URL`.

## Environment

Copy `.env.example` to `.env` and set:

```env
MANAGER_BOT_TOKEN=
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
PLATFORM_ADMIN_IDS=123456789
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/tg_spam_agent
SESSION_ENCRYPTION_KEY=change-me-to-a-long-random-secret
LOG_LEVEL=INFO
SCHEDULER_POLL_SECONDS=15
DEFAULT_INTERVAL_MINUTES=60
DEFAULT_JITTER_MINUTES=10
SENDER_DEBUG_ERRORS_TO_CHAT=false
SENDER_DEBUG_ERROR_COOLDOWN_SECONDS=300
```

Do not put plan price or limits in env. Platform admins edit them in the manager bot Admin menu.

## Local Run

Install dependencies:

```bash
pip install -e .[dev]
```

Run migrations against PostgreSQL/Supabase:

```bash
alembic upgrade head
```

Run the manager:

```bash
tg-spam-agent run-manager
```

Run the sender worker:

```bash
tg-spam-agent run-sender
```

Legacy file-based session setup is deprecated:

```bash
tg-spam-agent init-userbot-session
```

Use the manager bot Account menu instead. It walks the customer through phone, code, and optional 2FA password, then stores only an encrypted Telethon `StringSession`.

## Docker

Local compose starts PostgreSQL plus both app processes:

```bash
docker compose up -d --build
```

For Supabase production, point `DATABASE_URL` at the Supabase PostgreSQL connection string and run:

```bash
alembic upgrade head
```

## Manager Bot Menus

- `Account`: connect, view, or disconnect the tenant userbot session.
- `Subscription`: show current plan, subscription status, and Stars invoice.
- `Targets`: add, retry, enable, disable, or delete tenant targets.
- `Messages`: manage tenant message templates.
- `Schedule`: manage interval, jitter, sender toggle, and paid-message Stars allowance.
- `Users`: show inbound private users for the tenant userbot.
- `Tenant members`: add/remove additional manager users for the tenant.
- `Admin`: platform-admin-only plan settings: Stars price, max targets, max templates, min interval, active/inactive plan.

## Tests

```bash
pytest
```
