"""
Lead Pipeline — Web Interface
Google Auth (optional), Usage Tracking, LemonSqueezy Billing
"""

import os
import csv
import json
import time
import re
import hmac
import hashlib
import threading
import secrets
from collections import deque
import requests
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps

from flask import Flask, jsonify, request, send_file, send_from_directory, redirect, url_for, session, g
from dotenv import load_dotenv

from ad_check import check_ad_presence

load_dotenv()

app = Flask(__name__, static_folder="static")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

LOG_FILE = DATA_DIR / "pipeline.log"

# ── Config ───────────────────────────────────────────────────────

AUTH_ENABLED = os.getenv("AUTH_ENABLED", "false").lower() == "true"

_flask_secret = os.getenv("FLASK_SECRET", "")
if AUTH_ENABLED and not _flask_secret:
    raise RuntimeError(
        "FLASK_SECRET must be set when AUTH_ENABLED=true. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
app.secret_key = _flask_secret or secrets.token_hex(32)

# ── Database (only needed if auth enabled) ───────────────────────

if AUTH_ENABLED:
    from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
    from flask_sqlalchemy import SQLAlchemy
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests

    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DATA_DIR / 'leadpipeline.db'}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db = SQLAlchemy(app)

# ── Tier Config ──────────────────────────────────────────────────

TIERS = {
    "free": {
        "credits_per_month": 100,       # 100/mo × 12 = 1,200 lifetime
        "lifetime_cap": 1200,           # Total credits ever granted
        "apollo_credits": 0,            # No Apollo email credits on free
        "apollo_byok": False,           # Cannot use own key on free
        "max_categories": 1,
        "max_per_category": 10,
    },
    "starter": {
        "credits_per_month": 2500,
        "lifetime_cap": None,
        "apollo_credits": 200,          # One-time bonus
        "apollo_byok": True,
        "max_categories": 3,
        "max_per_category": 20,
    },
    "pro": {
        "credits_per_month": 4000,
        "lifetime_cap": None,
        "apollo_credits": 200,
        "apollo_byok": True,
        "max_categories": 5,
        "max_per_category": 25,
    },
    "agency": {
        "credits_per_month": 6000,
        "lifetime_cap": None,
        "apollo_credits": 200,
        "apollo_byok": True,
        "max_categories": 10,
        "max_per_category": 30,
    },
}

# LemonSqueezy product ID → tier mapping
# Fill these in from your LemonSqueezy dashboard
LEMONSQUEEZY_PRODUCT_TIERS = {
    os.getenv("LS_STARTER_PRODUCT_ID", ""): "starter",
    os.getenv("LS_PRO_PRODUCT_ID", ""): "pro",
    os.getenv("LS_AGENCY_PRODUCT_ID", ""): "agency",
}
LEMONSQUEEZY_WEBHOOK_SECRET = os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET", "")
APOLLO_OWNER_KEY = os.getenv("APOLLO_OWNER_KEY", "")  # Your Apollo key for free credits pool

# ── Models (auth only) ───────────────────────────────────────────

if AUTH_ENABLED:
    class User(UserMixin, db.Model):
        __tablename__ = "users"
        id = db.Column(db.Integer, primary_key=True)
        google_id = db.Column(db.String(64), unique=True, nullable=False)
        email = db.Column(db.String(255), nullable=False)
        name = db.Column(db.String(255), default="")
        picture = db.Column(db.String(512), default="")
        tier = db.Column(db.String(20), default="free")
        lemonsqueezy_customer_id = db.Column(db.String(64), default="")
        lemonsqueezy_subscription_id = db.Column(db.String(64), default="")
        subscription_status = db.Column(db.String(20), default="none")  # none, active, cancelled, past_due
        created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
        last_login = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    class Usage(db.Model):
        __tablename__ = "usage"
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
        action = db.Column(db.String(50), nullable=False)
        businesses_count = db.Column(db.Integer, default=0)
        categories_count = db.Column(db.Integer, default=0)
        timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
        user = db.relationship("User", backref="usage_records")

# ── Auth Setup (auth only) ───────────────────────────────────────

if AUTH_ENABLED:
    login_manager = LoginManager()
    login_manager.init_app(app)
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    @login_manager.unauthorized_handler
    def unauthorized():
        if request.path.startswith("/api/"):
            return jsonify({"error": "Login required", "redirect": "/login"}), 401
        return redirect("/login")

# ── Decorator: auth-aware ────────────────────────────────────────

def require_auth(f):
    """Wraps a route to require auth if AUTH_ENABLED, otherwise pass through."""
    if not AUTH_ENABLED:
        return f
    return login_required(f)

def get_current_user_id():
    """Returns user ID if auth enabled, otherwise 0 (dev mode)."""
    if AUTH_ENABLED:
        return current_user.id
    return 0

def get_current_user_tier():
    """Returns user tier if auth enabled, otherwise starter."""
    if AUTH_ENABLED:
        return current_user.tier
    return os.getenv("DEFAULT_TIER", "free")

# ── CSRF (double-submit cookie) ──────────────────────────────────
# Browsers cannot set the X-CSRF-Token header on cross-origin requests without
# CORS preflight, and this app does not enable CORS. An attacker's site also
# cannot read the csrf_token cookie from our domain. So requiring the header to
# match the cookie blocks forged form submissions from other origins.

CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_EXEMPT_PATHS = {
    "/api/webhooks/lemonsqueezy",  # HMAC-verified
    "/auth/google",                 # Google OAuth POST, no cookie yet on first login
}


def _issue_csrf_token() -> str:
    return secrets.token_urlsafe(32)


@app.before_request
def _csrf_protect():
    if not AUTH_ENABLED:
        return None
    if request.method in CSRF_SAFE_METHODS:
        return None
    if request.path in CSRF_EXEMPT_PATHS:
        return None
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    header_token = request.headers.get(CSRF_HEADER_NAME, "")
    if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
        log(f"CSRF: rejected {request.method} {request.path}")
        return jsonify({"error": "CSRF validation failed"}), 403
    return None


@app.after_request
def _csrf_set_cookie(response):
    if not AUTH_ENABLED:
        return response
    if not request.cookies.get(CSRF_COOKIE_NAME):
        response.set_cookie(
            CSRF_COOKIE_NAME,
            _issue_csrf_token(),
            secure=request.is_secure,
            httponly=False,  # must be readable by JS for double-submit
            samesite="Lax",
            max_age=60 * 60 * 24 * 7,
        )
    return response

# ── Helpers ──────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_tier_limits(tier_name: str) -> dict:
    return TIERS.get(tier_name, TIERS["free"])


# ── Credit System ────────────────────────────────────────────────

def _get_credits_file(user_id: int) -> Path:
    user_dir = DATA_DIR / str(user_id)
    user_dir.mkdir(exist_ok=True)
    return user_dir / "credits.json"


def _default_credits(tier_name: str) -> dict:
    """Initialize credits for a new user."""
    tier = TIERS.get(tier_name, TIERS["free"])
    now = datetime.now(timezone.utc).isoformat()
    return {
        "credit_balance": tier["credits_per_month"],
        "apollo_credit_balance": tier["apollo_credits"],
        "credits_granted_total": tier["credits_per_month"],
        "last_grant_date": now,
        "created_at": now,
    }


def _maybe_grant_monthly(credits: dict, tier_name: str) -> dict:
    """Grant monthly credits if a new month has started."""
    tier = TIERS.get(tier_name, TIERS["free"])
    last_grant = datetime.fromisoformat(credits["last_grant_date"])
    now = datetime.now(timezone.utc)

    if now.year > last_grant.year or (now.year == last_grant.year and now.month > last_grant.month):
        grant_amount = tier["credits_per_month"]

        # For free tier, check lifetime cap
        if tier.get("lifetime_cap"):
            remaining_cap = tier["lifetime_cap"] - credits.get("credits_granted_total", 0)
            grant_amount = min(grant_amount, max(remaining_cap, 0))

        if grant_amount > 0:
            credits["credit_balance"] = credits.get("credit_balance", 0) + grant_amount
            credits["credits_granted_total"] = credits.get("credits_granted_total", 0) + grant_amount
            credits["last_grant_date"] = now.isoformat()
            log(f"Monthly credit grant: +{grant_amount} credits (tier={tier_name})")

    return credits


def load_credits(user_id: int) -> dict:
    """Load user credits, creating defaults and granting monthly drip if needed."""
    path = _get_credits_file(user_id)
    tier = get_current_user_tier()
    if path.exists():
        credits = json.loads(path.read_text(encoding="utf-8"))
        credits = _maybe_grant_monthly(credits, tier)
        save_credits(user_id, credits)
        return credits
    # First time — initialize
    credits = _default_credits(tier)
    save_credits(user_id, credits)
    return credits


def save_credits(user_id: int, credits: dict):
    path = _get_credits_file(user_id)
    path.write_text(json.dumps(credits, indent=2), encoding="utf-8")


def check_credits(user_id: int, amount: int, credit_type: str = "credit") -> tuple[bool, str]:
    """Check if user has enough credits. Does NOT deduct."""
    credits = load_credits(user_id)
    field = "apollo_credit_balance" if credit_type == "apollo" else "credit_balance"
    balance = credits.get(field, 0)
    if balance < amount:
        if credit_type == "apollo":
            return False, f"Not enough Apollo credits ({balance} remaining). Add your own Apollo API key or upgrade your plan."
        return False, f"Not enough credits ({balance} remaining). Upgrade your plan for more."
    return True, ""


def deduct_credits(user_id: int, amount: int, credit_type: str = "credit") -> tuple[bool, str]:
    """Deduct credits. Returns (success, error_message)."""
    credits = load_credits(user_id)
    field = "apollo_credit_balance" if credit_type == "apollo" else "credit_balance"
    if credits[field] < amount:
        if credit_type == "apollo":
            return False, f"Not enough Apollo credits ({credits[field]} remaining)."
        return False, f"Not enough credits ({credits[field]} remaining). Upgrade your plan for more."
    credits[field] -= amount
    save_credits(user_id, credits)
    return True, ""


def refund_credits(user_id: int, amount: int, credit_type: str = "credit"):
    """Refund credits (e.g., if a pipeline step fails)."""
    credits = load_credits(user_id)
    field = "apollo_credit_balance" if credit_type == "apollo" else "credit_balance"
    credits[field] = credits.get(field, 0) + amount
    save_credits(user_id, credits)


def track_usage(user_id: int, action: str, businesses: int = 0, credits_used: int = 0):
    """Track usage for analytics (only writes to DB if auth enabled)."""
    if not AUTH_ENABLED:
        return
    record = Usage(
        user_id=user_id, action=action,
        businesses_count=businesses, categories_count=credits_used,
    )
    db.session.add(record)
    db.session.commit()


def get_user_businesses_file(user_id: int) -> Path:
    user_dir = DATA_DIR / str(user_id)
    user_dir.mkdir(exist_ok=True)
    return user_dir / "businesses.json"


def load_businesses(user_id: int) -> list[dict]:
    path = get_user_businesses_file(user_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_businesses(user_id: int, businesses: list[dict]):
    path = get_user_businesses_file(user_id)
    # Write to a temp file then replace, so a failed write can't truncate the data
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(businesses, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ── Pipeline Status (per-user) ───────────────────────────────────

_pipeline_lock = threading.Lock()
_pipeline_status = {}

def get_pipeline_status(user_id: int) -> dict:
    with _pipeline_lock:
        return _pipeline_status.get(user_id, {"running": False, "step": "", "progress": "", "error": ""})

def set_pipeline_status(user_id: int, status: dict):
    with _pipeline_lock:
        _pipeline_status[user_id] = status


# ── Google Places API ────────────────────────────────────────────

SEARCH_QUERIES = [
    "Accounting firm in {city}", "Advertising agency in {city}", "Architecture firm in {city}",
    "Auto repair shop in {city}", "Car dealership in {city}", "Chiropractor in {city}",
    "Cleaning service in {city}", "Concrete contractor in {city}", "Dental clinic in {city}",
    "Digital marketing agency in {city}", "Electrician in {city}", "Fence contractor in {city}",
    "Financial planner in {city}", "General contractor in {city}", "HVAC contractor in {city}",
    "Hair salon in {city}", "Handyman service in {city}", "IT consulting firm in {city}",
    "Insurance agency in {city}", "Landscaping company in {city}", "Law firm in {city}",
    "Locksmith in {city}", "Mortgage broker in {city}", "Moving company in {city}",
    "Painting contractor in {city}", "Pest control in {city}", "Plumber in {city}",
    "Pool service in {city}", "Property management company in {city}", "Real estate agency in {city}",
    "Roofing contractor in {city}", "Spa and wellness center in {city}", "Tree service in {city}",
    "Veterinary clinic in {city}", "Water damage restoration in {city}", "Window cleaning in {city}"
]


def _dedup(businesses: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for b in businesses:
        key = re.sub(r"[^a-z0-9]", "", b["name"].lower())
        addr = re.sub(r"[^a-z0-9]", "", b.get("address", "").lower())[:20]
        dedup_key = f"{key}|{addr}"
        if dedup_key not in seen:
            seen.add(dedup_key)
            unique.append(b)
    return unique


def _cache_key(category: str, city: str) -> str:
    """Generate a filesystem-safe cache key from category+city."""
    slug = re.sub(r"[^a-z0-9]+", "_", f"{category}_{city}".lower()).strip("_")
    return slug


def _load_reservoir(user_id: int, cache_key: str) -> dict:
    """Load the search reservoir for a category+city combo."""
    path = DATA_DIR / str(user_id) / "search_cache" / f"{cache_key}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"businesses": [], "served_ids": [], "passes_completed": 0, "center": None}


def _save_reservoir(user_id: int, cache_key: str, reservoir: dict):
    """Persist the search reservoir."""
    cache_dir = DATA_DIR / str(user_id) / "search_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{cache_key}.json"
    path.write_text(json.dumps(reservoir, indent=2), encoding="utf-8")


# Max geographic passes per tier (Pass 1 is always free relevance search)
TIER_MAX_PASSES = {"free": 1, "starter": 3, "pro": 5, "agency": 7}

# Geographic offsets (~5 miles ≈ 0.07 degrees) for search diversity
GEO_OFFSETS = [
    (0.07, 0),          # North
    (-0.07, 0),         # South
    (0, 0.07),          # East
    (0, -0.07),         # West
    (0.05, 0.05),       # NE
    (0.05, -0.05),      # NW
    (-0.05, 0.05),      # SE
    (-0.05, -0.05),     # SW
]


def scrape_google_places(categories: list[str], max_per: int, city: str,
                         user_id: int = 0, tier: str = "free",
                         single_shot: bool = False) -> list[dict]:
    """Search Google Places.

    single_shot mode is for looking up companies you already know by name:
    one API call per query, no pagination, no geographic expansion. The
    default discovery mode casts a wide net and costs up to 7 calls per query.
    """
    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_PLACES_API_KEY not set in .env")

    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,places.formattedAddress,"
            "places.nationalPhoneNumber,places.websiteUri,"
            "places.rating,places.userRatingCount,places.googleMapsUri,"
            "places.businessStatus,places.types,places.id,places.location,"
            "places.regularOpeningHours"
        ),
    }

    max_passes = 1 if single_shot else TIER_MAX_PASSES.get(tier, 1)

    def _do_search(query, extra_body=None):
        """Execute a single paginated search, return list of places."""
        results = []
        is_distance = extra_body and extra_body.get("rankPreference") == "DISTANCE"
        max_pages = 1 if (is_distance or single_shot) else 3
        page_token = None
        
        for _ in range(max_pages):
            body = {"textQuery": query, "maxResultCount": 20}
            if extra_body:
                body.update(extra_body)
            if page_token:
                body["pageToken"] = page_token
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=30)
                if resp.status_code == 429:
                    time.sleep(5)
                    continue
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log(f"  Error: {e}")
                break

            for place in data.get("places", []):
                name = place.get("displayName", {}).get("text", "")
                if not name:
                    continue
                loc = place.get("location", {})
                oh = place.get("regularOpeningHours", {})
                results.append({
                    "name": name, "address": place.get("formattedAddress", ""),
                    "phone": place.get("nationalPhoneNumber", ""),
                    "website": place.get("websiteUri", ""),
                    "rating": place.get("rating", 0),
                    "review_count": place.get("userRatingCount", 0),
                    "maps_url": place.get("googleMapsUri", ""),
                    "status": place.get("businessStatus", ""),
                    "hours": " | ".join(oh.get("weekdayDescriptions", [])),
                    "open_now": oh.get("openNow", ""),
                    "types": ", ".join(place.get("types", [])),
                    "place_id": place.get("id", ""),
                    "lat": loc.get("latitude", 0),
                    "lng": loc.get("longitude", 0),
                    "emails": "", "site_phones": "",
                    "has_contact_page": False, "site_quality": "",
                    "enrichment_source": "google_places",
                    "selected_for_apollo": False,
                })

            page_token = data.get("nextPageToken")
            if not page_token:
                break
            time.sleep(2)
        return results

    all_businesses = []
    for query_tpl in categories:
        query = query_tpl.format(city=city)
        ck = _cache_key(query, city)
        reservoir = _load_reservoir(user_id, ck)
        log(f"Searching: {query}")

        # Step 1: Serve un-served businesses from reservoir
        served_set = set(reservoir.get("served_ids", []))
        cached = reservoir.get("businesses", [])
        unserved = [b for b in cached if b.get("place_id") and b["place_id"] not in served_set]

        if unserved:
            to_serve = unserved[:max_per]
            all_businesses.extend(to_serve)
            # Mark as served
            for b in to_serve:
                served_set.add(b["place_id"])
            reservoir["served_ids"] = list(served_set)
            _save_reservoir(user_id, ck, reservoir)
            log(f"  From cache: {len(to_serve)} businesses ({len(unserved) - len(to_serve)} still cached)")
            continue  # Served from cache, no API calls needed

        # Step 2: Cache is depleted — make API calls to refill
        log(f"  Cache empty — searching Google Places...")
        passes_done = reservoir.get("passes_completed", 0)

        # Pass 1: Default relevance search (if not done yet)
        if passes_done == 0:
            results = _do_search(query)
            cached.extend(results)
            cached = _dedup(cached)
            passes_done = 1
            log(f"  Pass 1 (relevance): {len(results)} found")

            # Extract center coords
            lats = [b["lat"] for b in cached if b.get("lat")]
            lngs = [b["lng"] for b in cached if b.get("lng")]
            if lats:
                reservoir["center"] = {
                    "lat": sum(lats) / len(lats),
                    "lng": sum(lngs) / len(lngs)
                }

        # Pass 2+: Geographic offset passes (limited by tier)
        center = reservoir.get("center")
        if center and passes_done < max_passes:
            geo_start = passes_done - 1  # Which offset to start from
            geo_passes_remaining = max_passes - passes_done
            offsets_to_try = GEO_OFFSETS[geo_start:geo_start + geo_passes_remaining]

            for i, (dlat, dlng) in enumerate(offsets_to_try, passes_done + 1):
                extra = {
                    "locationBias": {
                        "circle": {
                            "center": {
                                "latitude": center["lat"] + dlat,
                                "longitude": center["lng"] + dlng
                            },
                            "radius": 8000.0
                        }
                    },
                    "rankPreference": "DISTANCE",
                }
                before = len(cached)
                results = _do_search(query, extra)
                cached.extend(results)
                cached = _dedup(cached)
                new_count = len(cached) - before
                passes_done += 1
                if new_count > 0:
                    log(f"  Pass {passes_done} (area search): +{new_count} new")
                time.sleep(1)

        # Save updated reservoir
        reservoir["businesses"] = cached
        reservoir["passes_completed"] = passes_done
        reservoir["served_ids"] = list(served_set)
        _save_reservoir(user_id, ck, reservoir)

        # Serve from newly fetched results
        unserved = [b for b in cached if b.get("place_id") and b["place_id"] not in served_set]
        to_serve = unserved[:max_per]
        all_businesses.extend(to_serve)
        for b in to_serve:
            served_set.add(b["place_id"])
        reservoir["served_ids"] = list(served_set)
        _save_reservoir(user_id, ck, reservoir)

        total_cached = len(cached)
        total_served = len(served_set)
        log(f"  Served {len(to_serve)} new | Cache: {total_cached} total, {total_cached - total_served} remaining")

    all_businesses = _dedup(all_businesses)
    # Strip internal lat/lng before returning
    for b in all_businesses:
        b.pop("lat", None)
        b.pop("lng", None)
    log(f"Total after dedup: {len(all_businesses)}")
    return all_businesses


# ── Firecrawl Enrichment ─────────────────────────────────────────

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
SKIP_EMAIL_DOMAINS = {"sentry", "example.com", "wix", "squarespace", "wordpress", "godaddy", "cloudflare", "sentry.io"}


def _assess_quality(content: str, emails: set, has_contact: bool) -> str:
    content_lower = content.lower()
    score = 0
    if len(content) < 500: score -= 2
    if not emails and not has_contact: score -= 1
    if any(old in content_lower for old in ["© 201", "© 2020", "© 2021", "copyright 201"]): score -= 1
    if "under construction" in content_lower or "coming soon" in content_lower: score -= 3
    if len(content) > 3000: score += 1
    if any(m in content_lower for m in ["react", "next.js", "tailwind"]): score += 2
    if "book online" in content_lower or "schedule" in content_lower: score += 1
    if score <= -3: return "terrible"
    elif score <= -1: return "poor"
    elif score <= 1: return "average"
    return "decent"


# Firecrawl plan allows 30 req/min; stay a little under to absorb clock drift
FIRECRAWL_RPM = int(os.getenv("FIRECRAWL_RPM", "28"))


class _RateLimiter:
    """Blocks so at most `rpm` requests start in any rolling 60s window."""

    def __init__(self, rpm: int):
        self.rpm = rpm
        self.stamps = deque()

    def wait(self):
        while True:
            now = time.monotonic()
            while self.stamps and now - self.stamps[0] >= 60:
                self.stamps.popleft()
            if len(self.stamps) < self.rpm:
                break
            time.sleep(60 - (now - self.stamps[0]) + 0.1)
        self.stamps.append(time.monotonic())


def _is_rate_limit_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "429" in msg or "rate limit" in msg


def _fc_scrape(fc, limiter: "_RateLimiter", url: str, max_attempts: int = 3):
    """Rate-limited fc.scrape. Waits out 429s instead of failing the business."""
    for attempt in range(max_attempts):
        limiter.wait()
        try:
            return fc.scrape(url, formats=["markdown"])
        except Exception as e:
            if _is_rate_limit_error(e) and attempt < max_attempts - 1:
                log(f"  Firecrawl rate limited, waiting 65s (attempt {attempt + 1})")
                time.sleep(65)
                continue
            raise


def enrich_with_firecrawl(businesses: list[dict], user_id: int) -> list[dict]:
    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        raise ValueError("FIRECRAWL_API_KEY not set in .env")

    from firecrawl import FirecrawlApp
    fc = FirecrawlApp(api_key=api_key)
    limiter = _RateLimiter(FIRECRAWL_RPM)

    to_enrich = [b for b in businesses if b.get("website") and not b.get("site_quality")]
    log(f"Firecrawl: enriching {len(to_enrich)} businesses")

    for i, biz in enumerate(to_enrich):
        set_pipeline_status(user_id, {
            "running": True, "step": "enrich",
            "progress": f"[{i+1}/{len(to_enrich)}] {biz['name'][:30]}", "error": ""
        })
        url = biz["website"]
        try:
            result = _fc_scrape(fc, limiter, url)
            if not result or not getattr(result, "markdown", None):
                biz.update({"site_quality": "scrape_failed", "enrichment_source": "firecrawl_failed"})
                continue
            content = result.markdown
            # Extract links from markdown since SDK v2 doesn't return them separately
            link_matches = re.findall(r'\[.*?\]\((https?://[^\)]+)\)', content)
            all_text = content
            emails = set(EMAIL_RE.findall(content))
            emails = {e for e in emails if not any(s in e.lower() for s in SKIP_EMAIL_DOMAINS)}
            contact_urls = [l for l in link_matches if any(kw in l.lower() for kw in ["/contact", "/contact-us"])]
            has_contact = bool(contact_urls)
            if contact_urls and not emails:
                try:
                    cc = _fc_scrape(fc, limiter, contact_urls[0])
                    if cc and getattr(cc, "markdown", None):
                        emails.update(EMAIL_RE.findall(cc.markdown))
                        all_text += " " + cc.markdown
                except Exception: pass
            site_phones = set(PHONE_RE.findall(all_text))
            quality = _assess_quality(all_text, emails, has_contact)
            biz.update({
                "emails": "; ".join(sorted(emails)) if emails else "",
                "site_phones": "; ".join(sorted(site_phones)) if site_phones else "",
                "has_contact_page": has_contact, "site_quality": quality,
                "enrichment_source": "google+firecrawl",
            })
        except Exception as e:
            log(f"  Firecrawl error for {biz['name']}: {e}")
            biz.update({"site_quality": "error", "enrichment_source": "firecrawl_error"})
    log("Firecrawl complete")
    return businesses


def enrich_with_apollo(businesses: list[dict], indices: list[int], api_key: str, enrich_type: str = "email") -> list[dict]:
    """Enrich businesses via Apollo API.
    
    enrich_type: "email" (1cr), "phone" (8cr), "full" (1-8cr)
    Tries organization/enrich first (free tier friendly), then people/match for contacts.
    """
    if not api_key:
        raise ValueError("Apollo API key required")
    log(f"Apollo: enriching {len(indices)} businesses (type={enrich_type})")
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    people_available = True  # skip people/match after first 403

    for idx in indices:
        if idx >= len(businesses): continue
        biz = businesses[idx]
        domain = re.sub(r"^https?://", "", biz.get("website", ""))
        domain = domain.split("/")[0]
        domain = re.sub(r"^www\.", "", domain).lower()
        if not domain: continue

        try:
            # Step 1: Try organization enrichment (free tier friendly)
            org_resp = requests.post(
                "https://api.apollo.io/api/v1/organizations/enrich",
                json={"domain": domain},
                headers=headers, timeout=15,
            )
            if org_resp.status_code == 200:
                org = org_resp.json().get("organization", {})
                if org:
                    biz["enrichment_source"] = biz.get("enrichment_source", "") + "+apollo_org"
                    biz["apollo_industry"] = org.get("industry", "")
                    biz["apollo_employees"] = org.get("employees", "")
                    biz["apollo_revenue"] = org.get("estimated_annual_revenue", "")
                    biz["apollo_founded"] = org.get("founded_year", "")
                    biz["apollo_city"] = org.get("city", "")
                    biz["apollo_state"] = org.get("state", "")
                    biz["apollo_linkedin"] = org.get("linkedin_url", "")
                    biz["apollo_phone"] = org.get("phone", "")
                    log(f"  Apollo org: enriched {biz['name']} ({org.get('industry','')}, {org.get('employees','')} emp)")

            # Step 2: For contact data, try people/match (paid tier only)
            if enrich_type in ("email", "phone", "full") and people_available:
                person_resp = requests.post(
                    "https://api.apollo.io/api/v1/people/match",
                    json={"domain": domain, "company_name": biz["name"]},
                    headers=headers, timeout=15,
                )
                if person_resp.status_code == 403:
                    log(f"  Apollo people/match: not available on this plan (403) — skipping remaining")
                    people_available = False
                elif person_resp.status_code == 401:
                    log(f"  Apollo 401: invalid API key")
                    raise ValueError("Invalid Apollo API key. Check your key in Step 3.")
                elif person_resp.status_code == 200:
                    person = person_resp.json().get("person", {})
                    email = person.get("email", "")
                    if email and person.get("email_status") in ("verified", "guessed"):
                        existing = biz.get("emails", "")
                        biz["emails"] = f"{existing}; {email}" if existing else email
                        biz["enrichment_source"] = biz.get("enrichment_source", "") + "+apollo_person"
                        biz["contact_name"] = person.get("name", "")
                        biz["contact_title"] = person.get("title", "")
                        biz["contact_email"] = email
                        if enrich_type in ("phone", "full"):
                            biz["contact_phone"] = person.get("phone", "")
                        biz["contact_linkedin"] = person.get("linkedin_url", "")
                        log(f"  Apollo person: found {email} for {biz['name']}")
                    else:
                        log(f"  Apollo person: no email for {biz['name']}")
                else:
                    log(f"  Apollo people/match {person_resp.status_code}: {person_resp.text[:200]}")

        except ValueError:
            raise
        except Exception as e:
            log(f"  Apollo error for {biz['name']}: {e}")
        time.sleep(1)

    log("Apollo complete")
    return businesses


def run_in_background(fn, user_id, *args):
    def _run():
        try:
            fn(*args)
            s = get_pipeline_status(user_id)
            if not s.get("error"):
                set_pipeline_status(user_id, {"running": False, "step": s.get("step", ""), "progress": "done", "error": ""})
        except Exception as e:
            set_pipeline_status(user_id, {"running": False, "step": "", "progress": "failed", "error": str(e)})
    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ══════════════════════════════════════════════════════════════════
# LEMONSQUEEZY WEBHOOK
# ══════════════════════════════════════════════════════════════════

@app.route("/api/webhooks/lemonsqueezy", methods=["POST"])
def lemonsqueezy_webhook():
    """
    Handle LemonSqueezy subscription webhooks.

    Events handled:
    - subscription_created: New subscription → update user tier
    - subscription_updated: Plan change / renewal → update tier
    - subscription_cancelled: Cancellation → mark as cancelled
    - subscription_expired: Past due / expired → downgrade to starter
    """
    payload = request.get_data()

    # Signature verification is mandatory — startup check guarantees the secret
    # is set when AUTH_ENABLED=true. When auth is off we still enforce it if a
    # secret is configured, and reject the request otherwise.
    if not LEMONSQUEEZY_WEBHOOK_SECRET:
        log("Webhook: rejected — LEMONSQUEEZY_WEBHOOK_SECRET not configured")
        return jsonify({"error": "Webhook not configured"}), 503

    signature = request.headers.get("X-Signature", "")
    expected = hmac.new(
        LEMONSQUEEZY_WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        log(f"Webhook: invalid signature")
        return jsonify({"error": "Invalid signature"}), 401

    data = request.json
    if not data:
        return jsonify({"error": "No payload"}), 400

    event_name = data.get("meta", {}).get("event_name", "")
    webhook_data = data.get("data", {})
    attributes = webhook_data.get("attributes", {})

    log(f"Webhook: {event_name}")

    if AUTH_ENABLED:
        # Find user by email from the subscription
        customer_email = attributes.get("user_email", "")
        if not customer_email:
            customer_email = attributes.get("customer_email", "")

        if not customer_email:
            log(f"Webhook: no email found in payload")
            return jsonify({"ok": True, "note": "no email"})

        user = User.query.filter_by(email=customer_email).first()
        if not user:
            log(f"Webhook: user not found for {customer_email}")
            return jsonify({"ok": True, "note": "user not found"})

        # Get product ID → tier mapping
        product_id = str(attributes.get("product_id", ""))
        new_tier = LEMONSQUEEZY_PRODUCT_TIERS.get(product_id, "")
        subscription_id = str(webhook_data.get("id", ""))

        if event_name == "subscription_created":
            if new_tier:
                user.tier = new_tier
            user.lemonsqueezy_subscription_id = subscription_id
            user.lemonsqueezy_customer_id = str(attributes.get("customer_id", ""))
            user.subscription_status = "active"
            log(f"Subscription created: {customer_email} → {user.tier}")

        elif event_name == "subscription_updated":
            if new_tier:
                user.tier = new_tier
            status = attributes.get("status", "")
            user.subscription_status = status
            log(f"Subscription updated: {customer_email} → {user.tier} ({status})")

        elif event_name in ("subscription_cancelled", "subscription_expired"):
            user.tier = "starter"
            user.subscription_status = "cancelled"
            log(f"Subscription ended: {customer_email} → starter")

        elif event_name == "subscription_resumed":
            if new_tier:
                user.tier = new_tier
            user.subscription_status = "active"
            log(f"Subscription resumed: {customer_email} → {user.tier}")

        db.session.commit()

    return jsonify({"ok": True})


# ── LemonSqueezy Checkout Link (helper) ──────────────────────────

@app.route("/api/checkout/<tier>")
@require_auth
def create_checkout(tier):
    """
    Generate a LemonSqueezy checkout URL for the given tier.
    LemonSqueezy handles the actual payment page.
    """
    if tier not in TIERS:
        return jsonify({"error": "Invalid tier"}), 400

    variant_id = os.getenv(f"LS_{tier.upper()}_VARIANT_ID", "")
    store_id = os.getenv("LEMONSQUEEZY_STORE_ID", "")

    if not variant_id or not store_id:
        return jsonify({"error": "LemonSqueezy not configured. Set LS_*_VARIANT_ID and LEMONSQUEEZY_STORE_ID in .env"}), 500

    # LemonSqueezy checkout URL format
    checkout_url = f"https://store.lemonsqueezy.com/checkout/buy/{variant_id}?checkout[custom][user_id]={get_current_user_id()}"

    return jsonify({"checkout_url": checkout_url})


# ══════════════════════════════════════════════════════════════════
# AUTH ROUTES (only active if AUTH_ENABLED)
# ══════════════════════════════════════════════════════════════════

if AUTH_ENABLED:
    @app.route("/login")
    def login_page():
        if current_user.is_authenticated:
            return redirect("/")
        return send_from_directory("static", "login.html")

    @app.route("/auth/google", methods=["POST"])
    def google_auth():
        data = request.json
        token = data.get("credential")
        if not token:
            return jsonify({"error": "No credential provided"}), 400
        try:
            idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
        except ValueError as e:
            return jsonify({"error": f"Invalid token: {e}"}), 401

        google_id = idinfo["sub"]
        email = idinfo.get("email", "")
        name = idinfo.get("name", "")
        picture = idinfo.get("picture", "")

        user = User.query.filter_by(google_id=google_id).first()
        if not user:
            user = User(google_id=google_id, email=email, name=name, picture=picture)
            db.session.add(user)
            log(f"New user: {email}")
        else:
            user.name = name
            user.picture = picture
            user.last_login = datetime.now(timezone.utc)
        db.session.commit()
        login_user(user, remember=True)
        return jsonify({"ok": True, "redirect": "/"})

    @app.route("/auth/logout")
    @login_required
    def logout():
        logout_user()
        return redirect("/login")


# ══════════════════════════════════════════════════════════════════
# PAGE & API ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    """Public health check — no auth required."""
    return jsonify({"status": "ok"})


@app.route("/")
@require_auth
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/me")
@require_auth
def api_me():
    uid = get_current_user_id()
    tier = get_current_user_tier()
    limits = get_tier_limits(tier)
    credits = load_credits(uid)

    resp = {
        "id": uid, "tier": tier, "limits": limits,
        "credits": {
            "balance": credits["credit_balance"],
            "apollo_balance": credits["apollo_credit_balance"],
            "credits_granted_total": credits["credits_granted_total"],
        },
    }
    if AUTH_ENABLED:
        resp.update({"email": current_user.email, "name": current_user.name, "picture": current_user.picture})
    else:
        resp.update({"email": "dev@local", "name": "Dev Mode", "picture": ""})
    return jsonify(resp)


@app.route("/api/tier")
@require_auth
def api_tier():
    uid = get_current_user_id()
    tier = get_current_user_tier()
    limits = get_tier_limits(tier)
    credits = load_credits(uid)
    return jsonify({
        "tier": tier, **limits,
        "credit_balance": credits["credit_balance"],
        "apollo_credit_balance": credits["apollo_credit_balance"],
    })


@app.route("/api/status")
@require_auth
def api_status():
    uid = get_current_user_id()
    return jsonify({
        "status": "ok",
        "pipeline": get_pipeline_status(uid),
        "business_count": len(load_businesses(uid)),
        "city": os.getenv("TARGET_CITY", "Las Vegas, NV"),
    })


@app.route("/api/categories")
@require_auth
def api_categories():
    return jsonify(SEARCH_QUERIES)


# ── Settings (Apollo BYOK) ──────────────────────────────────────

def _get_apollo_key_path(user_id: int) -> Path:
    p = DATA_DIR / str(user_id)
    p.mkdir(exist_ok=True)
    return p / ".apollo_key"

@app.route("/api/settings/apollo-key", methods=["GET"])
@require_auth
def get_apollo_key():
    uid = get_current_user_id()
    path = _get_apollo_key_path(uid)
    return jsonify({"has_key": path.exists()})

@app.route("/api/settings/apollo-key", methods=["POST"])
@require_auth
def save_apollo_key():
    uid = get_current_user_id()
    data = request.json or {}
    key = data.get("key", "").strip()
    if not key:
        return jsonify({"error": "API key required"}), 400
    path = _get_apollo_key_path(uid)
    path.write_text(key, encoding="utf-8")
    return jsonify({"ok": True})

@app.route("/api/settings/apollo-key", methods=["DELETE"])
@require_auth
def delete_apollo_key():
    uid = get_current_user_id()
    path = _get_apollo_key_path(uid)
    if path.exists():
        path.unlink()
    return jsonify({"ok": True})


@app.route("/api/scrape", methods=["POST"])
@require_auth
def api_scrape():
    uid = get_current_user_id()
    tier = get_current_user_tier()

    if get_pipeline_status(uid).get("running"):
        return jsonify({"error": "Pipeline already running"}), 400

    # Pre-check: user must have at least 1 credit
    can_afford, msg = check_credits(uid, 1)
    if not can_afford:
        return jsonify({"error": msg}), 429

    data = request.json or {}
    limits = get_tier_limits(tier)
    categories = data.get("categories", SEARCH_QUERIES)[:limits["max_categories"]]
    max_per = min(data.get("max_per", 15), limits["max_per_category"])
    city = data.get("city", os.getenv("TARGET_CITY", "Las Vegas, NV"))

    def do_scrape():
        set_pipeline_status(uid, {"running": True, "step": "scrape", "progress": "starting...", "error": ""})
        new_businesses = scrape_google_places(categories, max_per, city, user_id=uid, tier=tier)
        existing = load_businesses(uid)
        before_count = len(existing)
        # Track existing place_ids so we can auto-select only the new ones
        existing_ids = {b.get("place_id") for b in existing if b.get("place_id")}
        merged = _dedup(existing + new_businesses)
        actually_new = len(merged) - before_count
        # Auto-select new businesses for Apollo enrichment
        for b in merged:
            if b.get("place_id") and b["place_id"] not in existing_ids:
                b["selected_for_apollo"] = True
        save_businesses(uid, merged)
        # Only charge for genuinely new businesses (dedup-aware)
        if actually_new > 0:
            deduct_credits(uid, actually_new)
        log(f"Scrape complete: {len(new_businesses)} found, {actually_new} new (charged {actually_new} credits)")
        track_usage(uid, "scrape", businesses=actually_new, credits_used=actually_new)

    run_in_background(do_scrape, uid)
    return jsonify({"ok": True, "message": "Scrape started"})


@app.route("/api/scrape-list", methods=["POST"])
@require_auth
def api_scrape_list():
    """Look up a known list of companies by name, one Places call each.

    Body: {"queries": "Name One, City, ST\\nName Two, City, ST", "top_n": 1}

    Unlike /api/scrape this is not a discovery search: you already know who
    you are looking for, so there is no category cap, no pagination and no
    geographic expansion. Cost is one Places call per line.
    """
    uid = get_current_user_id()
    tier = get_current_user_tier()

    if get_pipeline_status(uid).get("running"):
        return jsonify({"error": "Pipeline already running"}), 400

    data = request.json or {}
    raw = data.get("queries") or ""
    if isinstance(raw, list):
        lines = [str(q).strip() for q in raw]
    else:
        lines = [ln.strip() for ln in raw.splitlines()]
    # Drop blanks and comment lines so a queries .txt file can be posted as-is
    queries = [ln for ln in lines if ln and not ln.startswith("#")]
    if not queries:
        return jsonify({"error": "No queries provided"}), 400

    can_afford, msg = check_credits(uid, len(queries))
    if not can_afford:
        return jsonify({"error": msg}), 429

    # scrape_google_places runs .format(city=...) on each query, which raises
    # on any literal brace in a company name. Escape them.
    categories = [q.replace("{", "{{").replace("}", "}}") for q in queries]
    top_n = max(1, min(int(data.get("top_n", 1)), 5))

    def do_scrape():
        set_pipeline_status(uid, {"running": True, "step": "scrape",
                                  "progress": f"0/{len(categories)}", "error": ""})
        new_businesses = scrape_google_places(categories, top_n, "", user_id=uid,
                                              tier=tier, single_shot=True)
        existing = load_businesses(uid)
        before_count = len(existing)
        existing_ids = {b.get("place_id") for b in existing if b.get("place_id")}
        merged = _dedup(existing + new_businesses)
        actually_new = len(merged) - before_count
        for b in merged:
            if b.get("place_id") and b["place_id"] not in existing_ids:
                b["selected_for_apollo"] = True
        save_businesses(uid, merged)
        if actually_new > 0:
            deduct_credits(uid, actually_new)
        log(f"List scrape complete: {len(queries)} queries, "
            f"{len(new_businesses)} matched, {actually_new} new")
        track_usage(uid, "scrape", businesses=actually_new, credits_used=actually_new)

    run_in_background(do_scrape, uid)
    return jsonify({"ok": True, "queries": len(queries),
                    "message": f"List scrape started ({len(queries)} lookups)"})


@app.route("/api/enrich", methods=["POST"])
@require_auth
def api_enrich():
    uid = get_current_user_id()
    if get_pipeline_status(uid).get("running"):
        return jsonify({"error": "Pipeline already running"}), 400
    businesses = load_businesses(uid)
    if not businesses:
        return jsonify({"error": "No businesses to enrich. Run scrape first."}), 400

    # Count businesses needing enrichment
    pending = [b for b in businesses if b.get("website") and not b.get("site_quality")]
    to_enrich_count = len(pending)
    if to_enrich_count == 0:
        return jsonify({"error": "All businesses already enriched."}), 400

    # Pre-check credits (1 credit per business)
    can_afford, msg = check_credits(uid, to_enrich_count)
    if not can_afford:
        return jsonify({"error": msg}), 429

    def do_enrich():
        set_pipeline_status(uid, {"running": True, "step": "enrich", "progress": "starting...", "error": ""})
        enriched = enrich_with_firecrawl(businesses, uid)
        save_businesses(uid, enriched)
        # Free ad-presence checks (Google Ads Transparency + site tag sniff) — no credits
        set_pipeline_status(uid, {"running": True, "step": "enrich", "progress": "checking ad presence...", "error": ""})
        check_ad_presence(enriched, progress_cb=lambda p: set_pipeline_status(
            uid, {"running": True, "step": "enrich", "progress": f"ads {p}", "error": ""}))
        save_businesses(uid, enriched)
        # Only charge for leads enriched in this run (not failed/errored).
        # enrich_with_firecrawl updates the dicts in `pending` in place.
        enriched_count = sum(
            1 for b in pending
            if b.get("site_quality") and b.get("site_quality") not in ("scrape_failed", "error")
            and not (b.get("site_quality") or "").startswith("error")
        )
        if enriched_count > 0:
            deduct_credits(uid, enriched_count)
        log(f"Enrichment complete: {enriched_count} businesses (charged {enriched_count} credits)")
        track_usage(uid, "enrich", businesses=enriched_count, credits_used=enriched_count)

    run_in_background(do_enrich, uid)
    return jsonify({"ok": True, "message": f"Enrichment started ({to_enrich_count} businesses)"})


@app.route("/api/rescan", methods=["POST"])
@require_auth
def api_rescan():
    """Rescan failed leads — resets error states and re-enriches."""
    uid = get_current_user_id()
    if get_pipeline_status(uid).get("running"):
        return jsonify({"error": "Pipeline already running"}), 400

    businesses = load_businesses(uid)
    if not businesses:
        return jsonify({"error": "No businesses loaded"}), 400

    data = request.json or {}
    indices = data.get("indices")

    # If no indices given, find all failed leads
    if indices is None:
        indices = [i for i, b in enumerate(businesses)
                   if b.get("site_quality") in ("scrape_failed", "error")
                   or (b.get("site_quality") or "").startswith("error")]
    else:
        indices = [i for i in indices if i < len(businesses)]

    if not indices:
        return jsonify({"error": "No failed leads to rescan"}), 400

    # Reset error states so enrich_with_firecrawl picks them up
    for i in indices:
        biz = businesses[i]
        if biz.get("site_quality") in ("scrape_failed", "error") or (biz.get("site_quality") or "").startswith("error"):
            biz["site_quality"] = ""
            biz["enrichment_source"] = ""

    def do_rescan():
        set_pipeline_status(uid, {"running": True, "step": "enrich", "progress": f"rescanning {len(indices)} leads...", "error": ""})
        enriched = enrich_with_firecrawl(businesses, uid)
        save_businesses(uid, enriched)
        track_usage(uid, "rescan", businesses=len(indices))

    run_in_background(do_rescan, uid)
    return jsonify({"ok": True, "message": f"Rescanning {len(indices)} leads"})


@app.route("/api/apollo", methods=["POST"])
@require_auth
def api_apollo():
    uid = get_current_user_id()
    tier = get_current_user_tier()

    if get_pipeline_status(uid).get("running"):
        return jsonify({"error": "Pipeline already running"}), 400

    data = request.json or {}
    indices = data.get("indices", [])
    enrich_type = data.get("enrich_type", "org")  # "org" (free) or "email"/"phone"/"full"

    if not indices:
        return jsonify({"error": "No businesses selected"}), 400
    if len(indices) > 25:
        indices = indices[:25]

    businesses = load_businesses(uid)
    if not businesses:
        return jsonify({"error": "No businesses loaded"}), 400

    # Determine API key and credit behavior based on enrich_type
    if enrich_type == "org":
        # Org search is FREE for all tiers — uses organizations/enrich
        api_key = APOLLO_OWNER_KEY
        key_path = _get_apollo_key_path(uid)
        if key_path.exists():
            api_key = key_path.read_text(encoding="utf-8").strip()
        if not api_key:
            return jsonify({"error": "Apollo not configured. Contact support."}), 500
    else:
        # People search — requires paid tier
        if tier == "free":
            return jsonify({
                "error": "Upgrade to Starter or higher to unlock decision-maker email search. Apollo org search is free for all plans.",
                "upgrade_required": True,
            }), 403

        # Check for Apollo credits or BYOK key
        user_credits = load_credits(uid)
        key_path = _get_apollo_key_path(uid)
        has_byok = key_path.exists()

        if user_credits["apollo_credit_balance"] > 0:
            # Use owner's key, will deduct Apollo credits per email found
            api_key = APOLLO_OWNER_KEY
            if not api_key:
                return jsonify({"error": "Apollo not configured. Contact support."}), 500
        elif has_byok:
            # Use their own key — no Apollo credit deduction
            api_key = key_path.read_text(encoding="utf-8").strip()
        else:
            return jsonify({
                "error": "No Apollo credits remaining. Add your own Apollo API key or sign up for free credits at apollo.io",
                "needs_apollo_key": True,
            }), 402

    use_owner_key = (api_key == APOLLO_OWNER_KEY) and enrich_type != "org"

    def do_apollo():
        set_pipeline_status(uid, {"running": True, "step": "apollo", "progress": "starting...", "error": ""})
        enriched = enrich_with_apollo(businesses, indices, api_key, enrich_type)
        save_businesses(uid, enriched)

        # For people search using owner key, deduct Apollo credits per email found
        if use_owner_key:
            emails_found = sum(
                1 for i in indices
                if i < len(enriched) and enriched[i].get("contact_email")
            )
            if emails_found > 0:
                deduct_credits(uid, emails_found, "apollo")
                log(f"Apollo: charged {emails_found} Apollo credits for emails found")

        track_usage(uid, "apollo", businesses=len(indices))

    run_in_background(do_apollo, uid)
    return jsonify({"ok": True, "message": f"Apollo {enrich_type} enrichment started ({len(indices)} businesses)"})


@app.route("/api/businesses")
@require_auth
def api_businesses():
    return jsonify(load_businesses(get_current_user_id()))


@app.route("/api/businesses", methods=["PUT"])
@require_auth
def api_update_businesses():
    data = request.json
    if not isinstance(data, list):
        return jsonify({"error": "Expected array of businesses"}), 400
    save_businesses(get_current_user_id(), data)
    return jsonify({"ok": True, "count": len(data)})


@app.route("/api/export")
@require_auth
def api_export():
    uid = get_current_user_id()
    businesses = load_businesses(uid)
    if not businesses:
        return jsonify({"error": "No data to export"}), 400
    csv_path = DATA_DIR / str(uid) / "leads.csv"
    csv_path.parent.mkdir(exist_ok=True)
    columns = [
        "name", "address", "phone", "website", "rating", "review_count",
        "hours", "status", "place_id",
        "emails", "site_phones", "site_quality", "has_contact_page",
        "advertises_google", "google_advertiser", "google_advertiser_type",
        "meta_pixel", "google_ads_tag",
        "types", "enrichment_source", "maps_url",
        "apollo_industry", "apollo_employees", "apollo_revenue", "apollo_founded",
        "apollo_linkedin", "apollo_phone",
        "contact_name", "contact_title", "contact_email", "contact_phone", "contact_linkedin",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(businesses)
    return send_file(csv_path, as_attachment=True, download_name="leads.csv")


@app.route("/api/export/json")
@require_auth
def api_export_json():
    uid = get_current_user_id()
    businesses = load_businesses(uid)
    if not businesses:
        return jsonify({"error": "No data to export"}), 400
    json_path = DATA_DIR / str(uid) / "leads.json"
    json_path.parent.mkdir(exist_ok=True)
    json_path.write_text(json.dumps(businesses, indent=2, ensure_ascii=False), encoding="utf-8")
    return send_file(json_path, as_attachment=True, download_name="leads.json")


@app.route("/api/clear", methods=["POST"])
@require_auth
def api_clear():
    path = get_user_businesses_file(get_current_user_id())
    if path.exists():
        path.unlink()
    return jsonify({"ok": True})


@app.route("/api/log")
@require_auth
def api_log():
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").strip().split("\n")
        return jsonify(lines[-100:])
    return jsonify([])


@app.route("/api/credits")
@require_auth
def api_credits():
    """Return current credit balances."""
    uid = get_current_user_id()
    credits = load_credits(uid)
    tier = get_current_user_tier()
    tier_config = get_tier_limits(tier)
    return jsonify({
        "credit_balance": credits["credit_balance"],
        "apollo_credit_balance": credits["apollo_credit_balance"],
        "credits_granted_total": credits["credits_granted_total"],
        "credits_per_month": tier_config["credits_per_month"],
        "tier": tier,
        "last_grant_date": credits.get("last_grant_date", ""),
    })


# ── Init ─────────────────────────────────────────────────────────

if AUTH_ENABLED:
    with app.app_context():
        db.create_all()
    if not LEMONSQUEEZY_WEBHOOK_SECRET:
        raise RuntimeError(
            "LEMONSQUEEZY_WEBHOOK_SECRET must be set when AUTH_ENABLED=true. "
            "Get it from the LemonSqueezy webhook settings page."
        )

if __name__ == "__main__":
    log(f"Lead Pipeline starting (auth={'on' if AUTH_ENABLED else 'off'})")
    app.run(host="0.0.0.0", port=5000, debug=False)
