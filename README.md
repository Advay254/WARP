---
title: Evolution API
emoji: 📱
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# Evolution API — Multi-Instance WhatsApp Gateway

Self-hosted Evolution API v2 with a secure FastAPI proxy layer, multi-instance management, configurable webhooks, phone-number pairing, and a full management dashboard.

## Environment Variables

| Secret | Required | Description |
|---|---|---|
| `EVOLUTION_API_KEY` | ✅ | Master API key for Evolution API |
| `ADMIN_KEY` | ✅ | Extra key required for instance create/delete |
| `DASHBOARD_PASSWORD` | ✅ | Web dashboard password |
| `DASHBOARD_USERNAME` | No | Default: `admin` |
| `JWT_SECRET` | ✅ | Signs session cookies |
| `WEBHOOK_SECRET` | ✅ | HMAC secret for validating webhook payloads |
| `SESSION_SECRET` | ✅ | AES-256 key for encrypting session state |
| `DATABASE_PROVIDER` | No | `postgresql` (recommended) |
| `DATABASE_CONNECTION_URI` | ✅ | Supabase/PostgreSQL connection string |
| `CACHE_REDIS_ENABLED` | No | `true` |
| `CACHE_REDIS_URI` | ✅ | Upstash Redis URL |
| `SERVER_URL` | ✅ | Your public HF Space URL |
| `WARMUP_MODE` | No | `true` for new numbers (extra-safe rate limits) |
| `MAX_TURNS` | No | Max bot turns before handoff. Default `20` |
| `MSG_MIN_DELAY_MS` | No | Min delay between outbound msgs. Default `1500` |
| `MSG_MAX_DELAY_MS` | No | Max delay between outbound msgs. Default `4000` |
