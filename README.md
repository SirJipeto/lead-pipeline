# LeadPipeline

A self-hosted tool for finding local business leads. Search Google Places by category and city, enrich each business from its website, check whether it advertises, and export the results to CSV or JSON.

## Features

- **Business search:** find businesses by category and city with the Google Places API, or look up a known list of companies by name
- **Website enrichment:** collect emails, phone numbers, and contact pages with Firecrawl
- **Site quality scoring:** rate each website from `terrible` to `decent`
- **Ad presence checks:** detect Google Ads activity through the Ads Transparency Center, plus Meta Pixel and Google Ads tags on the homepage
- **Apollo enrichment (optional):** add company details and decision-maker contacts
- **Export:** download your leads as CSV or JSON

## Requirements

- Python 3.10 or later
- A [Google Places API](https://console.cloud.google.com/) key with **Places API (New)** enabled
- A [Firecrawl](https://firecrawl.dev) API key
- An [Apollo](https://apollo.io) API key (optional)

These services have their own pricing and free tiers, so check their current rates before running large searches.

## Getting Started

### Ubuntu

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/SirJipeto/lead-pipeline.git
cd lead-pipeline
./setup.sh
```

Add your API keys to `.env`, then start the app:

```bash
./start.sh
```

Open http://localhost:5000.

### Windows

```bat
git clone https://github.com/SirJipeto/lead-pipeline.git
cd lead-pipeline
pip install -r requirements.txt
copy .env.example .env
```

Add your API keys to `.env`, then run `Start.bat` and open http://localhost:5000.

### Docker

```bash
cp .env.example .env
docker compose up -d --build
```

## Configuration

Settings are read from `.env`. See [`.env.example`](.env.example) for the full list.

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_PLACES_API_KEY` | Yes | Google Places API key |
| `FIRECRAWL_API_KEY` | Yes | Firecrawl API key |
| `FIRECRAWL_RPM` | No | Firecrawl requests per minute (default: `28`) |
| `APOLLO_OWNER_KEY` | No | Apollo API key. You can also add one in the app |
| `TARGET_CITY` | No | Default search city (default: `Las Vegas, NV`) |
| `DEFAULT_TIER` | No | Usage limits when login is off: `free`, `starter`, `pro`, or `agency` |
| `AUTH_ENABLED` | No | Set to `true` to require Google login (default: `false`) |
| `FLASK_SECRET` | With login | Session secret |
| `GOOGLE_CLIENT_ID` | With login | Google OAuth client ID |
| `LEMONSQUEEZY_WEBHOOK_SECRET` | With login | LemonSqueezy webhook signing secret |

`start.sh` also accepts `HOST` and `PORT`.

## Security

Login is off by default. While it is off, anyone who can reach the app can view your leads and use your API keys.

`start.sh` listens on `127.0.0.1` for this reason. Set `AUTH_ENABLED=true` before exposing the app to a network or the internet with `HOST=0.0.0.0`. `Start.bat` and Docker listen on all network interfaces, so only use them on a network you trust.

## Project Structure

```
├── app.py              # Flask app and API
├── ad_check.py         # Ad presence checks
├── static/             # App interface
├── landing/            # Marketing page
├── setup.sh            # Ubuntu install
├── start.sh            # Ubuntu start
├── Start.bat           # Windows start
├── Dockerfile
└── docker-compose.yaml
```

Your leads, logs, and credit balances are stored in `data/`, which is excluded from git.

## License

[MIT](LICENSE)
