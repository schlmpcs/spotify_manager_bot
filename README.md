# Spotify Manager Automatization

An AI assistant that runs on your **manager's personal Telegram account** (via
Telegram Business / Chat Automation). It:

- **Answers customers from your account**, in your style, grounded in the real
  payment database (never invents prices or due dates).
- **Texts overdue customers first** — because people ignore the bot and only pay
  after the manager messages them.
- Runs on **free LLMs via OpenRouter**, with an automatic fallback chain — if one
  free model is rate-limited (429), it tries the next — so there's no API cost.

It is a *separate* bot from the main payment bot. It only **reads** the payment
bot's PostgreSQL database for grounding.

---

## How it works

```
customer ──► manager's account ──► (Telegram Business) ──► this bot
                                                              │
                              grounding ◄── PostgreSQL (payment bot, read-only)
                                                              │
                                  draft ◄── OpenRouter (free-model fallback chain)
                                                              │
   reply / nudge ◄── sent AS the manager via business_connection_id
```

- **Incoming messages** → looked up in the payment DB → LLM drafts a reply →
  auto-sent as the manager (or queued for one-tap approval).
- **Sensitive topics** ("I already paid", refunds, complaints) are **always**
  escalated to you for approval instead of auto-answered.
- **Daily job** finds overdue customers and sends a personalised reminder as the
  manager.

### ⚠️ The proactive limitation (read this)

Telegram only lets a personal account message users it **already has a chat
with** — you cannot cold-DM a stranger from the manager account (anti-spam).
So a nudge that can't be delivered automatically falls back to a **draft** you
see in the bot. Customers who've messaged your manager before are reachable
directly; the rest you'll see as drafts.

---

## Setup

### 1. Create the bot
- Talk to [@BotFather](https://t.me/BotFather) → **new bot** → copy the token.
- (BotFather → your bot → **Bot Settings → Business Mode → Enable**.)

### 2. Get a free OpenRouter key
- Sign up at [openrouter.ai](https://openrouter.ai) → **Keys** → create a key.
- Free models (`...:free`) need no credit; they're just rate-limited, which the
  fallback chain in `LLM_MODELS` handles by rotating to the next model on a 429.

### 3. Configure
```bash
cp .env.example .env   # fill BOT_TOKEN, OWNER_IDS, OPENROUTER_API_KEY, DB creds
```
Point `DB_*` at the **same PostgreSQL** the main payment bot uses. `LLM_MODELS`
already lists a sensible free chain — reorder or extend it as you like.

### 4. Run
```bash
pip install -r requirements.txt
python main.py
```
or with Docker:
```bash
docker compose up -d --build
```

### 5. Connect it to your manager account
On the manager's phone: **Settings → Telegram Business → Chat-bots** (or
**Chat Automation**) → add your bot → choose which chats → enable **"Reply to
messages"**. The bot will DM you a confirmation.

---

## Commands (manager only)

| Command | Description |
|---|---|
| `/start` | Intro + how to connect |
| `/status` | Connection state, auto-reply mode, style |
| `/style` | Describe how you talk to customers (the bot mimics it) |
| `/auto on\|off` | Auto-send replies, or queue drafts for approval |
| `/nudge` | Run the overdue-customer outreach right now |

## Key settings (`.env`)

| Var | Meaning |
|---|---|
| `AUTO_REPLY` | `true` = auto-send replies; `false` = approve each |
| `PROACTIVE_MODE` | `auto` (send as manager), `approve` (one-tap), `off` |
| `PROACTIVE_OVERDUE_DAYS` | Start nudging this many days overdue |
| `PROACTIVE_COOLDOWN_DAYS` | Don't re-nudge within N days |
| `OPENROUTER_API_KEY` | Your OpenRouter key (free tier is fine) |
| `LLM_MODELS` | Ordered free-model chain; next is tried on a 429 |

---

## Safety model

- Replies are **grounded** in the payment DB — the model is told to use only the
  injected amounts/dates and never invent reqs.
- Money-sensitive messages are **escalated**, not auto-answered.
- In `approve` mode nothing goes out without your tap.
- The bot has **read-only** intent against the payment DB (it never writes).
