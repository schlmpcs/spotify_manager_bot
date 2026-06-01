# Spotify Manager Automatization

An AI assistant that runs on your **manager's personal Telegram account** (via
Telegram Business / Chat Automation). It:

- **Answers customers from your account**, in your style, grounded in the real
  payment database (never invents prices or due dates).
- **Texts overdue customers first** — because people ignore the bot and only pay
  after the manager messages them.
- Runs on **any OpenAI-compatible LLM** — OpenAI (`gpt-4o-mini`), OpenRouter's free
  models, or a local Ollama model — chosen by `.env`, with a fallback chain that
  rotates to the next model on a rate-limit (429).

It is a *separate* bot from the main payment bot. It only **reads** the payment
bot's PostgreSQL database for grounding.

---

## How it works

```
customer ──► manager's account ──► (Telegram Business) ──► this bot
                                                              │
                              grounding ◄── PostgreSQL (payment bot, read-only)
                                                              │
                                  draft ◄── LLM (OpenAI / OpenRouter / Ollama)
                                                              │
   reply / nudge ◄── sent AS the manager via business_connection_id
```

- **Incoming messages** → looked up in the payment DB → LLM drafts a reply →
  auto-sent as the manager (or queued for one-tap approval).
- **Sensitive topics** ("I already paid", refunds, complaints) are **always**
  escalated to you for approval instead of auto-answered.
- **Daily job** (noon Almaty) walks overdue customers through a 2-step reminder,
  one step per day: **day 1** "не оплачено — будете продлевать?", **day 2**
  "отключать вас от подписки?". If they still haven't paid after both, on **day 3**
  it stops messaging them and sends *you* a summary of who to deal with by hand.
  These two nudge texts are fixed (you own the wording), not LLM-generated.

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

### 2. Pick an LLM provider
Set `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODELS` in `.env` (examples inline there):
- **OpenAI** — reliable, cheap with `gpt-4o-mini`; key from [platform.openai.com](https://platform.openai.com).
- **OpenRouter** — free models (rate-limited); key from [openrouter.ai/keys](https://openrouter.ai/keys).
- **Ollama** — free & private, runs on your own GPU (see [Local model](#local-model-ollama)).

### 3. Configure
```bash
cp .env.example .env   # fill BOT_TOKEN, OWNER_IDS, LLM_API_KEY, DB creds
```
Point `DB_*` at the **same PostgreSQL** the main payment bot uses.

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
| `PROACTIVE_COOLDOWN_DAYS` | Legacy; the staged sequence now advances one step per day |
| `LLM_BASE_URL` | OpenAI-compatible endpoint (OpenAI / OpenRouter / Ollama) |
| `LLM_API_KEY` | Key for that provider (`ollama` for local) |
| `LLM_MODELS` | Ordered model chain; next is tried on a 429 |

---

## Local model (Ollama)

To run **free & private** on your own GPU (e.g. an RTX 5060 8 GB) instead of a
paid API. The bot must be able to reach the machine that hosts Ollama.

```bash
# on the GPU box — install https://ollama.com, then:
ollama pull qwen2.5:7b-instruct      # ~4.7 GB Q4, fits 8 GB VRAM, strong Russian
ollama serve                          # exposes http://localhost:11434
```
`qwen2.5:7b-instruct` is the sweet spot for 8 GB. Tighter alternatives if you
want more headroom: `llama3.1:8b`, `gemma2:9b`.

Then in `.env`:
```bash
LLM_BASE_URL=http://host.docker.internal:11434/v1   # Ollama's OpenAI-compatible API
LLM_API_KEY=ollama                                   # any non-empty placeholder
LLM_MODELS=["qwen2.5:7b-instruct"]
```

> **Where it runs matters.** A typical VPS has no GPU. To use the 5060 you must
> either run the whole bot **on the GPU machine**, or keep the bot on the VPS and
> expose Ollama to it over a private link (e.g. Tailscale / WireGuard) — never
> expose `11434` to the public internet. For a GPU-less VPS, `gpt-4o-mini` is the
> simplest reliable choice.

---

## Safety model

- Replies are **grounded** in the payment DB — the model is told to use only the
  injected amounts/dates and never invent reqs.
- Money-sensitive messages are **escalated**, not auto-answered.
- In `approve` mode nothing goes out without your tap.
- The bot has **read-only** intent against the payment DB (it never writes).
