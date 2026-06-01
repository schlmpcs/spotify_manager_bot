# CLAUDE.md — Spotify Manager Automatization Bot

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

A **separate** Telegram bot from the main payment bot (`../spotify_family_automatization-fresh-start`). It runs on the **manager's personal Telegram account** via **Telegram Business / Chat Automation** and:

- **Answers customers as the manager**, in the manager's style, grounded in the real payment database.
- **Texts overdue customers first** (proactive nudges) — because customers ignore the bot and only pay after the manager messages them.
- Talks to any **OpenAI-compatible LLM API** (OpenAI / OpenRouter / local Ollama) via an ordered fallback chain that rotates to the next model on a 429 — provider chosen purely by `.env`.

It only **reads** the main payment bot's PostgreSQL DB for grounding — it never writes to it.

---

## Tech Stack

| Layer | Tech |
|---|---|
| Language | Python 3.11+ (Dockerfile pins 3.11-slim) |
| Bot framework | Aiogram 3.x (FSM via MemoryStorage) |
| Telegram feature | Business connection (`business_connection` / `business_message` updates; replies via `business_connection_id`) |
| LLM | Any OpenAI-compatible API (OpenAI / OpenRouter / Ollama) set by `LLM_BASE_URL`; ordered chain in `LLM_MODELS`, rotates on 429 |
| Own state | SQLite via **aiosqlite** (`data/manager.db`) |
| Grounding source | **asyncpg**, read-only into the payment bot's Postgres |
| Scheduling | APScheduler 3.x (`AsyncIOScheduler`) |
| Config | pydantic-settings (`.env`) |
| Deployment | Docker + docker-compose (single bot container) |

---

## File Structure

```
├── main.py                  # Entry: init stores, build bot, register routers, start polling
├── config.py                # All env vars via pydantic BaseSettings (Settings + `settings` singleton)
├── bot/
│   ├── handlers/
│   │   ├── business.py      # @router.business_connection + @router.business_message
│   │   └── owner.py         # Owner commands (/start /status /style /auto /nudge) + draft-approval callbacks
│   ├── services/
│   │   ├── llm.py           # Async OpenAI-compatible client — chat(messages) -> str | None (model fallback chain)
│   │   ├── dispatch.py      # deliver() (send AS manager) + send_or_approve() (auto vs draft)
│   │   └── outreach.py      # run_outreach(bot) — daily overdue-customer nudges
│   ├── db/
│   │   ├── local.py         # SQLite: connections, message history, owner_settings, drafts, outreach_log
│   │   └── payments.py      # asyncpg pool; get_customer_status() / get_overdue_customers()
│   ├── utils/
│   │   ├── prompts.py       # System prompt, grounded context block, escalation triggers
│   │   ├── states.py        # OwnerStates (setting_style, editing_draft)
│   │   └── keyboards.py     # draft_kb() — Send / Edit / Skip inline buttons
│   └── scheduler.py         # start_scheduler(bot) — daily outreach cron job
├── Dockerfile
├── docker-compose.yml       # single bot container
└── README.md
```

---

## Architecture

### Message flow (incoming)
1. Customer messages the manager → arrives as a `business_message` update in `bot/handlers/business.py`.
2. The manager's own outgoing messages also arrive here — they are recorded as `assistant` context and **not** replied to (guard: `message.from_user.id == owner_id`).
3. Customer text is stored, then `payments.get_customer_status(customer_id)` builds the grounding.
4. `prompts.system_prompt(status, style)` + last ~10 messages → `llm.chat(...)`.
5. `dispatch.send_or_approve(...)` either auto-sends as the manager or posts a draft to the manager for approval.

### Sending AS the manager
All customer-facing sends go through `dispatch.deliver()`, which calls `bot.send_message(chat_id, text, business_connection_id=conn_id)` (and a typing action first). **Telegram only allows messaging users the manager account already has a chat with** — undeliverable sends fall back to a draft (`send_or_approve` catches `TelegramBadRequest` / `TelegramForbiddenError`).

### Proactive outreach
`outreach.run_outreach(bot)` (daily via APScheduler, or `/nudge`): pulls overdue customers from Postgres, dedupes per user, skips those contacted within `PROACTIVE_COOLDOWN_DAYS`, drafts a nudge via the LLM, and sends/approves under the first connected owner identity. `log_outreach()` records the cooldown.

### Grounding (never invent facts)
`bot/db/payments.py` mirrors the main bot's `get_overdue_users` logic exactly: `COALESCE((latest payment next_payment_date), groups.next_payment_date)` with `ug.is_phantom = FALSE`. Region from `display_id` (`'1…'` → RU `₽`, else KZ `₸`); amounts from `KZ_GROUP_PRICE` / `RU_GROUP_PRICE`. The model is instructed to use **only** the injected context block.

### Safety / escalation
`prompts.needs_escalation(text)` matches sensitive triggers (`уже оплат`, refunds, complaints, "already paid", "not working", …). Escalated messages are **always** drafted for manager approval, regardless of `AUTO_REPLY`. The model is told never to confirm payment, promise refunds, or output reqs/amounts not in context.

---

## Key Settings (`.env` → `config.py`)

| Var | Meaning |
|---|---|
| `BOT_TOKEN` | This automation bot (a **new** @BotFather bot, Business Mode enabled) |
| `OWNER_IDS` | Manager Telegram user id(s) — owner commands are filtered to these |
| `LLM_BASE_URL` | OpenAI-compatible endpoint (OpenAI / OpenRouter / Ollama) |
| `LLM_API_KEY` | Key for that provider (`ollama` placeholder for local) |
| `LLM_MODELS` | Ordered model chain (JSON list); next is tried on a 429 |
| `DB_*` | The **same** Postgres as the payment bot (read-only grounding) |
| `LOCAL_DB_PATH` | SQLite file for this bot's own state |
| `AUTO_REPLY` | `true` = auto-send replies; `false` = draft every reply |
| `PROACTIVE_MODE` | `auto` (send as manager) / `approve` (one-tap) / `off` |
| `PROACTIVE_OVERDUE_DAYS`, `PROACTIVE_COOLDOWN_DAYS`, `PROACTIVE_HOUR` | Outreach tuning |
| `KZ_GROUP_PRICE`, `RU_GROUP_PRICE` | Amounts stated to customers |

Per-owner overrides (style prompt, auto-reply) live in the SQLite `owner_settings` table and take precedence over the global `.env` defaults.

---

## Owner Commands

| Command | Description |
|---|---|
| `/start` | Intro + how to connect the bot to the account |
| `/status` | Connection state, auto-reply mode, proactive mode, style |
| `/style` | FSM: capture the manager's writing style (stored per-owner) |
| `/auto on\|off` | Toggle auto-send vs draft approval |
| `/nudge` | Run overdue-customer outreach immediately (forces even if `PROACTIVE_MODE=off`) |

Draft approval is via inline buttons (`d:send:<id>` / `d:edit:<id>` / `d:skip:<id>`) handled in `owner.py`.

---

## Conventions & Constraints

- **Strictly async.** Never use `requests`, `time.sleep()`, or sync `psycopg2`. Use `aiohttp`, `asyncio.sleep()`, `asyncpg`, `aiosqlite`.
- **Read-only on the payment DB.** This bot must never `INSERT`/`UPDATE`/`DELETE` against the payment bot's Postgres.
- **All grounding goes through `bot/db/payments.py`** and must mirror the main bot's payment-status logic — keep the `COALESCE(... , groups.next_payment_date)` + `is_phantom = FALSE` shape in sync if the main bot changes.
- **Customer-facing sends only via `dispatch.deliver` / `send_or_approve`** — never call `bot.send_message(..., business_connection_id=...)` directly from handlers, so escalation/fallback stays centralised.
- **The model must not invent payment facts.** Any new info the customer can ask about must be added to the context block in `prompts.build_context_block`, not left to the model.
- **New FSM states** → `bot/utils/states.py`; **new keyboards** → `bot/utils/keyboards.py`; **new env vars** → `config.py` + `.env.example`.
- **Owner-only handlers** are gated by `router.message.filter(F.from_user.id.in_(settings.owner_ids))` in `owner.py`; callback handlers re-check `settings.owner_ids` manually.
- **Aiogram version drift:** `BusinessConnection` moved `can_reply` → `rights.can_reply` (≥3.15). `business.py` reads both defensively — preserve that.
- **Git commits: no co-authorship.** Do NOT add `Co-Authored-By` or "Generated with Claude Code" trailers (same rule as the main repo).

---

## Running

**Local:**
```bash
cp .env.example .env          # fill BOT_TOKEN, OWNER_IDS, LLM_API_KEY, DB_*
pip install -r requirements.txt
python main.py
```

**Docker:**
```bash
docker compose up -d --build
```

**Connect to the manager account:** Settings → Telegram Business → Chat-bots (or Chat Automation) → add this bot → choose chats → enable "Reply to messages". Enable Business Mode for the bot in @BotFather first.

---

## Important Notes

- **The proactive hard limit:** a personal account cannot cold-DM a user it has no prior chat with (Telegram anti-spam). Undeliverable nudges become drafts — this is expected, not a bug.
- **One LLM failure ≠ a bad message:** if `llm.chat` returns `None`, nothing is sent to the customer; the manager is pinged to reply manually.
- **`business_message` is bidirectional** — both customer and manager messages arrive; always guard on `from_user.id` before replying.
- **Inspiration:** the business-connection wiring shape is adapted from `github.com/rlxrd/assist_ai_bot`, but this project adds grounding, proactive outreach, escalation, local LLM, and integration with the payment DB.
