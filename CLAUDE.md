# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Stack

Django 5+ REST API (DRF) with Channels (WebSockets), Celery + Redis, Firebase auth, and a multi-database SQLite/Postgres split. Frontend lives in a separate repo and is symlinked at `frontend/` (ignored by git).

## Common commands

```bash
# Local dev (runs daphne + celery worker + beat together via honcho)
honcho start -f Procfile.processes

# Or just the web tier
daphne -b 0.0.0.0 -p 8000 middleman_api.asgi:application
python manage.py runserver  # WSGI only — won't serve websockets

# Migrations: ALWAYS use migrate_all (see Multi-database section)
python manage.py migrate_all

# Tests
python manage.py test                       # all apps
python manage.py test agreement             # one app
python manage.py test users.tests.AuthenticationTests.test_authentication_success_new_user

# Celery (when running pieces separately)
celery -A middleman_api worker -l info
celery -A middleman_api beat -l info

# Debug helper for routing rules
python check_routers.py
```

Requires Redis for production-grade Channels (`REDIS_URL`) and Celery; without it Channels falls back to `InMemoryChannelLayer` and Celery defaults to `redis://localhost:6379/0`.

## Architecture

### Multi-database with app-scoped routers

Four databases — `default`, `wallet_db`, `wager_db`, `agreement_db` — each backing a single app. `middleman_api/db_routers.py` enforces this: each router both pins its own app to its DB AND blocks foreign apps from migrating into it. The `users`/`auth`/`admin`/`sessions` apps live in `default`.

Consequences for any code that touches data:

- **Never run `migrate` directly** — use `python manage.py migrate_all` (in `middleman_api/management/commands/`). It iterates every DB so each router's `allow_migrate` runs.
- **Cross-app ForeignKeys must use `db_constraint=False`** (see `agreement.Agreement` → `users.User`). DB-level integrity isn't possible across DBs; relations resolve via Django ORM only.
- **Tests must declare `databases = '__all__'`** (or a subset like `{'default', 'agreement_db', 'wallet_db'}`). Tests without this will silently skip data setup on non-default DBs.
- **Wallet writes use `transaction.atomic(using="wallet_db")`** explicitly, and notifications fire via `transaction.on_commit(..., using="wallet_db")`. Mirror this pattern for any new wallet-touching code.
- Production overrides each DB via `DATABASE_URL`, `WALLET_DATABASE_URL`, `WAGER_DATABASE_URL`, `AGREEMENT_DATABASE_URL` (see `settings.py`).

### Auth

`users.authentication.FirebaseAuthentication` is the only DRF auth class — it verifies Firebase ID tokens and lazily provisions `users.User` (custom `AUTH_USER_MODEL`) on first sight. Default permission is `IsAuthenticated`. There is no Django session login flow for the API. Firebase Admin is initialized in `settings.py` from `FIREBASE_CREDENTIALS_PATH`, which can be a local file or an HTTPS URL (e.g. R2).

### WebSockets (Channels)

`middleman_api/asgi.py` mounts `agreement.routing` and `wager.routing` under the `websocket` protocol. Consumers (`agreement/consumers.py`, `wager/consumers.py`) authenticate by Firebase token passed in the WS handshake, verify per-room access, and broadcast via Channels groups. Use `daphne` (or honcho) — `runserver` won't serve them.

### Money flow (escrow)

`agreement/services.py` (`AgreementService`) is the single source of truth for funding/releasing escrow. Key invariants:

- `ESCROW_FEE_RATE` (default `0.035`) and `Agreement.fee_payer` (`me`/`other`/`split`) determine `(buyer_fee, seller_fee)` via `_resolve_fee_payer` + `_calculate_fees`. `me`/`other` are resolved against `creator_role` to absolute `buyer`/`seller`.
- Buyer pays `offer_amount + buyer_fee` at funding; seller receives `offer_amount − seller_fee` at release.
- `Agreement.buyer_debited_amount` / `buyer_debited_currency` are persisted at debit time so refunds (cancel/expiry) reverse the exact amount, even after rate changes.
- `PLATFORM_FEE_WALLET_USER_ID` (in `settings.py`, default `None`) optionally credits an internal wallet with the fee. When `None`, fees are only logged.
- All wallet mutations route through `wallet.services.WalletEngine` for a single audit trail; do not write `Wallet.balance` directly elsewhere.

### Periodic & background work

`agreement.tasks.process_agreement_expiries` runs every 60s via Celery beat (`CELERY_BEAT_SCHEDULE` in `settings.py`). It does three passes per tick: 24h/1h reminders, mark-expired + 1h grace window, then cancel + refund after grace. Refunds use the persisted `buyer_debited_*` fields. Adding a new periodic task: register it in `CELERY_BEAT_SCHEDULE` and put the implementation in `<app>/tasks.py` so `app.autodiscover_tasks()` picks it up.

### Multi-currency

Amounts on `Agreement` and `Transaction` are stored as `amount`, `amount_usd`, `amount_ngn`. Conversions go through `middleman_api/utils.py` (`convert_currency`, `get_converted_amounts`), which reads the `rates` app's `Rate` model. There's a hardcoded USD→NGN fallback of `1500.00` if no `Rate` row exists for USD — only for that pair.

### External integrations

- **Paystack** (NGN deposits, withdrawals, virtual accounts) — webhooks at `wallet.views.PaystackWebhookView`. `WITHDRAWAL_COMMISSION_FEE` (300 NGN flat) is collected internally; only one outbound transfer is made per withdrawal. Failed transfers refund via `WalletEngine._reverse_withdrawal`.
- **NOWPayments** (crypto) — webhook at `wallet.views.NOWPaymentsWebhookView`, signature verified with `NOWPAYMENTS_IPN_SECRET`.
- **Intercom** (disputes) — `agreement.intercom.IntercomClient`, webhook at `agreement.views.IntercomWebhookView`.
- **MetaMap** (identity verification) — webhook at `users.views.MetaMapWebhookView`, signed with `METAMAP_WEBHOOK_SECRET`.
- **Resend** (transactional email) via django-anymail.
- **FCM** (push) via `fcm-django`; helpers in `users/notifications.py`.

### Storage

When `USE_R2=True`, both static and media use Cloudflare R2 via `middleman_api/storage_backends.py` (S3-compatible) under custom domain `cdn.midman.app`. Otherwise filesystem (`static-root/`, `media-root/`).

### Response shape

All DRF errors flow through `middleman_api.exceptions.custom_exception_handler` and are normalized to `{ "status": "error", "code": ..., "message": ..., "data"?: ... }`. Successful responses use `middleman_api.utils.StandardResponse` for the same envelope shape — prefer it over raw `Response` for new endpoints.

## Conventions worth knowing

- ID generation: agreements/offers/messages use short prefixed random IDs (`agr_xxxxxxxx`, `off_xxxxxxxx`, `msg_xxxxxxxx`) via `agreement.models.generate_short_id`. Wager IDs follow the same pattern.
- The test runner disables `SECURE_SSL_REDIRECT` / secure-cookie flags automatically when `'test' in sys.argv` (see top of `settings.py`). Don't reintroduce these as hard-coded values.
- `RequestLoggingMiddleware` logs every request/response — keep noisy `print` calls out of it.
