# LeadPipeline

Find local businesses with bad websites, get their contact info, and close more deals.

## Quick Start (Development)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy env file and add your API keys
cp .env.example .env
# Edit .env — at minimum set GOOGLE_PLACES_API_KEY and FIRECRAWL_API_KEY

# 3. Run
python app.py

# 4. Open http://localhost:5000
```

Auth is disabled by default in development. Set `AUTH_ENABLED=true` in `.env` to enable Google login.

## Architecture

```
lead-pipeline/
├── app.py                  # Flask backend (API + auth + billing)
├── static/
│   ├── index.html          # Main app UI (pipeline + results)
│   └── login.html          # Google Sign-In page
├── landing/
│   └── index.html          # Marketing landing page
├── scraper.py              # CLI version (no web UI needed)
├── requirements.txt
├── .env.example
├── .gitignore
├── setup.sh
└── README.md
```

## How It Works

1. **Scrape** — Google Places API finds businesses by category and city
2. **Enrich** — Firecrawl scrapes each website for emails, phones, contact pages
3. **Review** — Site quality scoring (terrible → decent) tells you who needs help
4. **Apollo** — Optional second pass for businesses without emails (Pro/Agency tier)
5. **Export** — Download CSV for cold email outreach

## Tiers

| | Starter ($29/mo) | Pro ($79/mo) | Agency ($149/mo) |
|---|---|---|---|
| Categories/run | 1 | 3 | 5 |
| Max per category | 20 | 25 | 25 |
| Apollo enrichment | ✗ | ✓ | ✓ |
| Monthly runs | 10 | 30 | Unlimited |

## API Keys Setup

### Google Places API
1. [Google Cloud Console](https://console.cloud.google.com/) → Create project
2. Enable **Places API (New)**
3. Create API key under Credentials
4. Add billing ($200/mo free credits for new accounts)

### Firecrawl
1. Sign up at [firecrawl.dev](https://firecrawl.dev)
2. Copy API key from dashboard
3. Free tier: 500 credits/month

### Apollo.io (optional)
1. Sign up at [apollo.io](https://apollo.io)
2. Settings → Integrations → API key
3. Free tier: 1,200 exports/year

### Google OAuth (production only)
1. Google Cloud Console → Credentials → OAuth 2.0 Client ID
2. Type: Web application
3. Authorized origins: `https://yourdomain.com`
4. Redirect URIs: `https://yourdomain.com/auth/google`

### LemonSqueezy (production billing)
1. Create account at [lemonsqueezy.com](https://lemonsqueezy.com)
2. Create 3 products (Starter $29, Pro $79, Agency $149)
3. Copy variant IDs and product IDs to `.env`
4. Set webhook URL: `https://yourdomain.com/api/webhooks/lemonsqueezy`
5. Subscribe to events: `subscription_created`, `subscription_updated`, `subscription_cancelled`, `subscription_expired`

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_PLACES_API_KEY` | Yes | Google Places API key |
| `FIRECRAWL_API_KEY` | Yes | Firecrawl API key |
| `APOLLO_API_KEY` | No | Apollo.io API key |
| `AUTH_ENABLED` | No | `true` to enable login (default: `false`) |
| `GOOGLE_CLIENT_ID` | If auth | Google OAuth client ID |
| `FLASK_SECRET` | If auth | Flask session secret |
| `LEMONSQUEEZY_WEBHOOK_SECRET` | No | Webhook signature verification |
| `LEMONSQUEEZY_STORE_ID` | No | LemonSqueezy store ID |
| `LS_*_VARIANT_ID` | No | LemonSqueezy product variant IDs |
| `LS_*_PRODUCT_ID` | No | LemonSqueezy product IDs |
| `TARGET_CITY` | No | Default city (default: Las Vegas, NV) |

## Costs

A typical run costs under $1 in API calls:

| Service | 300 businesses | Free tier |
|---------|---------------|-----------|
| Google Places | $0.64 | $200/mo credit (new accounts) |
| Firecrawl | $0 | 500 credits/mo |
| Apollo | $0 | 1,200/year |

## Landing Page

The marketing landing page is in `landing/index.html`. Preview:

```bash
cd landing
python -m http.server 8080
# Open http://localhost:8080
```

## License

Proprietary. All rights reserved.
