# Hell X Checker — Telegram Bot (Razorpay)

Railway-ready Telegram bot for Razorpay CC checking.

## Bot commands

| Command | Action |
|---|---|
| `/start` | Info + usage |
| `/check cc\|mm\|yy\|cvv` | Single card check inline |
| `/stats` | Live stats for current run |
| `/stop` | Halt current batch |
| Send `.txt` file | Bulk check — results + approved sent as files |
| Paste cards directly | Same as file for multi-line, inline for single |

## Card format

```
4111111111111111|12|2026|123
5500005555555559|01|2025|999
```

---

## Railway deploy

1. Push repo to GitHub
2. Railway → New Project → Deploy from GitHub
3. Set env vars:

| Var | Required | Value |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | Bot token from @BotFather |
| `RZP_PAGE_URL` | ✅ | `https://pages.razorpay.com/yourslug` |
| `PROXY_URL` | ❌ | `http://user:pass@host:port` |
| `ALLOWED_IDS` | ❌ | Comma-sep Telegram user IDs (blank = open) |
| `AMOUNT_INR` | ❌ | Charge amount in INR (default: 1) |

4. Deploy — Railway runs the worker process, no port needed.

---

## Result categories

- ✅ **CHARGED** — actual charge went through
- 🟢 **LIVE** — valid card, insufficient balance / limit
- 🔵 **CCN** — card valid, CVV wrong
- ❌ **DEAD** — declined
- ⚠️ **ERROR** — network / parse issue
