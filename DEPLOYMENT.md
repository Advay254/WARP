# Evolution API Gateway — Deployment Guide

## Repo Structure

```
evolution-hf-space/
├── Dockerfile               ← Runs Evolution API + FastAPI proxy via supervisord
├── supervisord.conf         ← Process manager config
├── README.md
├── .env.example             ← All env vars documented
├── DEPLOYMENT.md
└── app/
    ├── main.py              ← Entrypoint, waits for Evolution API to start
    ├── server.py            ← All routes: proxy + webhook receiver + dashboard
    ├── auth.py              ← Bearer + Admin key + session cookie auth
    ├── safety.py            ← Rate limiting, anti-ban, jitter, loop prevention
    ├── webhook_manager.py   ← Per-instance webhook CRUD + event routing
    ├── requirements.txt
    └── templates/
        ├── login.html
        └── dashboard.html   ← Full management UI
```

---

## Step 1 — External Services

### Supabase (free tier)
1. Create project at [supabase.com](https://supabase.com)
2. Go to **Settings → Database → Connection Pooling**
3. Copy the **Transaction** pooler URI (port 6543)
4. Set as `DATABASE_CONNECTION_URI`

### Upstash Redis (free tier)
1. Create database at [upstash.com](https://upstash.com)
2. Copy the **Redis URL**
3. Set as `CACHE_REDIS_URI`

---

## Step 2 — Create the HF Space

1. [huggingface.co/new-space](https://huggingface.co/new-space)
2. **SDK → Docker**
3. Visibility → **Private**
4. Hardware → **CPU basic** (free tier)

---

## Step 3 — Push Code

```bash
git clone https://huggingface.co/spaces/YOUR_USERNAME/evolution-api
cd evolution-api
cp -r /path/to/evolution-hf-space/* .
git add . && git commit -m "Initial Evolution API deployment" && git push
```

---

## Step 4 — Set Secrets

Space → **Settings → Variables and Secrets**:

| Key | Type | Notes |
|---|---|---|
| `EVOLUTION_API_KEY` | **Secret** | Strong random string |
| `ADMIN_KEY` | **Secret** | Separate key for instance create/delete |
| `DASHBOARD_PASSWORD` | **Secret** | Your login password |
| `JWT_SECRET` | **Secret** | `openssl rand -hex 32` |
| `WEBHOOK_SECRET` | **Secret** | `openssl rand -hex 32` |
| `SESSION_SECRET` | **Secret** | `openssl rand -hex 32` |
| `SERVER_URL` | Variable | Your Space public URL |
| `DATABASE_CONNECTION_URI` | **Secret** | Supabase pooler URI |
| `CACHE_REDIS_URI` | **Secret** | Upstash Redis URL |
| `WARMUP_MODE` | Variable | `true` for new numbers |

---

## Step 5 — First Boot

Build takes ~5 minutes (installs Evolution API from GitHub).
Both processes start via supervisord:
- Evolution API on internal port 8080
- FastAPI proxy on public port 7860

Watch logs for:
```
evolution-api  | Server running on port 8080
fastapi-proxy  | Evolution API is ready ✓
fastapi-proxy  | Uvicorn running on 0.0.0.0:7860
```

---

## Step 6 — Add Your First Phone Number

1. Go to `https://YOUR-SPACE.hf.space/dashboard`
2. Click **+ Add Number**
3. Enter instance name (e.g. `business-1`) and your phone number
4. Click **Create Instance**
5. Click **🔗 Connect** on the new instance card
6. Enter the same phone number
7. Click **Get Pairing Code**
8. On your phone: WhatsApp → Linked Devices → Link a Device → Link with phone number
9. Enter the displayed code

---

## Step 7 — Configure Webhooks for n8n

1. Click **🔔 Webhooks** on your instance card
2. Add a webhook:
   - **Label**: `n8n main`
   - **URL**: `https://YOUR-N8N.hf.space/webhook/whatsapp`
   - **Events**: Select `MESSAGES_UPSERT` (incoming messages) or leave blank for all
3. Click **Add Webhook**

In n8n, create a **Webhook** node that listens on `/webhook/whatsapp`.
The payload will contain:
```json
{
  "instance": "business-1",
  "event": "MESSAGES_UPSERT",
  "data": {
    "key": { "remoteJid": "5511999@s.whatsapp.net", "fromMe": false, "id": "..." },
    "message": { "conversation": "Hello!" },
    "pushName": "John"
  }
}
```

---

## Sending Messages via n8n

After processing with LLM, send reply back via HTTP Request node in n8n:

```
POST https://YOUR-SPACE.hf.space/api/message/safe/{instanceName}
Headers:
  apikey: YOUR_EVOLUTION_API_KEY
Body:
{
  "number": "5511999999999",
  "text": "Hello! This is the bot reply."
}
```

The `/api/message/safe/` endpoint automatically applies:
- Rate limiting
- Typing indicator simulation
- Random 1.5–4s delay with extra time for message length
- Anti-broadcast detection
- Bot message ID registration (prevents reply loops)

---

## Full Voice Pipeline (n8n flow)

```
WhatsApp voice note
  ↓ Evolution API → webhook → n8n
  ↓ n8n → Whisper Space (transcribe audio)
  ↓ n8n → LLM Space (generate response)
  ↓ n8n → Kokoro Space (text → speech) [optional]
  ↓ n8n → Evolution API safe send → WhatsApp reply
```

---

## Anti-Ban Summary

| Measure | Default |
|---|---|
| Max messages/min | 20 (10 in warmup) |
| Burst limit | 10 msgs in 60s |
| Burst cooldown | 5 minutes |
| Min send delay | 1,500ms |
| Max send delay | 4,000ms + typing time |
| Typing presence | Sent before every message |
| Reply loop prevention | Bot message IDs tracked |
| Self-message guard | Sender JID checked |
| Broadcast detection | Same body to 3+ recipients |
| Max conversation turns | 20 (configurable) |
| Warmup mode | 2× delays, 0.5× limits |
