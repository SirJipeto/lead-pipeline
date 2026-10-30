"""
Free advertising-presence checks for leads.

Two signals, no API keys, no cost:
  1. Google Ads Transparency Center — undocumented public RPC used by
     adstransparency.google.com. Searching by the lead's domain returns the
     advertiser's creatives if they run Google Ads. Empty response = no ads.
     The request/response use numbered protobuf-style fields, so this may
     break without notice if Google changes the endpoint.
  2. Homepage tag sniff — fetch the lead's site and look for the Facebook
     Pixel and the Google Ads conversion/remarketing tag. A pixel means the
     business is set up for (or has run) Meta ads; it is not proof of
     currently active campaigns. Tags injected via Tag Manager at runtime
     are not visible in raw HTML, so misses are possible.
"""

import json
import re
import time

import requests

GATS_URL = "https://adstransparency.google.com/anji/_/rpc/SearchService/SearchCreatives?authuser="
US_REGION = 2840  # Google region code for United States

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

META_PIXEL_RE = re.compile(
    r"fbq\s*\(\s*['\"]init|connect\.facebook\.net/[^\"']*fbevents\.js", re.I)
GOOGLE_ADS_TAG_RE = re.compile(
    r"\bAW-\d{9,11}\b|googleads\.g\.doubleclick\.net/pagead/viewthroughconversion"
    r"|google_conversion_id", re.I)


CORP_SUFFIXES = {
    "llc", "inc", "ltd", "llp", "lp", "corp", "co", "company", "companies",
    "group", "holdings", "enterprise", "enterprises", "services",
    "international", "limited", "incorporated", "operating", "domestic",
}
STOP_TOKENS = {"the", "at", "of", "and", "a", "an", "in"}
# Generic industry words — a person's name won't contain these, and an
# overlap on one of them alone shouldn't count as a business-name match.
BIZ_WORDS = {
    "spa", "wellness", "salon", "studio", "center", "centers", "clinic",
    "health", "medical", "med", "skin", "beauty", "massage", "sauna",
    "fitness", "market", "media", "marketing", "digital", "agency",
    "hotel", "hotels", "resort", "resorts", "casino", "sales",
}


def _norm_tokens(s: str) -> list[str]:
    s = re.sub(r"[^a-z0-9\s]", " ", (s or "").lower())
    return [t for t in s.split()
            if t not in CORP_SUFFIXES and t not in STOP_TOKENS and len(t) > 1]


def classify_advertiser(advertiser: str, business_name: str,
                        domain: str = "", city: str = "") -> str:
    """Heuristic: is the verified Google advertiser the business itself,
    an individual (likely the owner), or an unrelated org (agency,
    franchisor, or corporate parent)?"""
    if not advertiser:
        return ""
    city_toks = set(_norm_tokens(city))
    adv = [t for t in _norm_tokens(advertiser) if t not in city_toks]
    biz = set(_norm_tokens(business_name)) - city_toks
    dom_squash = re.sub(r"[^a-z0-9]", "",
                        re.sub(r"\.[a-z]{2,6}$", "", domain or "").lower())
    distinctive = [t for t in adv if t not in BIZ_WORDS]
    if any(t in biz or (dom_squash and t in dom_squash) for t in distinctive):
        return "self-managed"
    adv_squash = "".join(t for t in adv if t not in BIZ_WORDS)
    for hay in (dom_squash, "".join(_norm_tokens(business_name))):
        if hay and adv_squash and (adv_squash in hay or hay in adv_squash):
            return "self-managed"
    raw = re.sub(r"[^a-zA-Z\s]", " ", advertiser).split()
    if (2 <= len(raw) <= 4
            and not any(w.lower() in CORP_SUFFIXES | BIZ_WORDS for w in raw)):
        return "individual"
    return "agency/parent"


def extract_domain(url: str) -> str:
    domain = re.sub(r"^https?://", "", url or "")
    domain = domain.split("/")[0].split("?")[0]
    return re.sub(r"^www\.", "", domain).lower()


def check_google_ads(domain: str, session: requests.Session) -> dict:
    """Ask the Ads Transparency Center for creatives verified to this domain.

    Google rate-limits this endpoint by 302-redirecting to its /sorry CAPTCHA
    page; the block lifts after a pause, so back off and retry before giving up.
    """
    payload = {"2": 10, "3": {"12": {"1": domain, "2": True}},
               "7": {"1": 1, "2": 0, "3": US_REGION}}
    creatives = None
    backoff = 20
    for attempt in range(4):
        try:
            resp = session.post(
                GATS_URL,
                data={"f.req": json.dumps(payload)},
                headers={"User-Agent": UA,
                         "Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
                allow_redirects=False,
            )
            if resp.status_code in (302, 429, 503):  # rate limited
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            creatives = resp.json().get("1", [])
            break
        except Exception:
            break
    if creatives is None:
        return {"advertises_google": "check_failed"}
    if not creatives:
        return {"advertises_google": "no"}
    advertiser = next((c.get("12") for c in creatives if c.get("12")), "")
    return {
        "advertises_google": "yes",
        "google_advertiser": advertiser,
        # We only request 10 creatives, so 10 means "10 or more"
        "google_ads_count": len(creatives),
    }


def sniff_site_tags(url: str, session: requests.Session) -> dict:
    """Fetch the homepage and look for Meta Pixel / Google Ads tags."""
    try:
        resp = session.get(url, headers={"User-Agent": UA}, timeout=15,
                           allow_redirects=True)
        html = resp.text[:2_000_000]
    except Exception:
        return {"meta_pixel": "check_failed", "google_ads_tag": "check_failed"}
    return {
        "meta_pixel": "yes" if META_PIXEL_RE.search(html) else "no",
        "google_ads_tag": "yes" if GOOGLE_ADS_TAG_RE.search(html) else "no",
    }


def check_ad_presence(businesses: list[dict], progress_cb=None,
                      recheck: bool = False) -> list[dict]:
    """Annotate businesses with advertising-presence signals in place.

    Skips businesses without a website and (unless recheck) ones already
    checked — except that failed checks are always retried. Google lookups
    are throttled and cached per domain within a run.
    """
    def _needs_check(b):
        if not b.get("website"):
            return False
        if recheck or not b.get("ads_checked"):
            return True
        return "check_failed" in (b.get("advertises_google"), b.get("meta_pixel"))

    todo = [b for b in businesses if _needs_check(b)]
    session = requests.Session()
    google_cache: dict[str, dict] = {}
    google_fails_in_a_row = 0
    for i, biz in enumerate(todo):
        if progress_cb:
            progress_cb(f"[{i + 1}/{len(todo)}] {biz['name'][:30]}")
        domain = extract_domain(biz["website"])
        if not domain:
            continue
        if domain not in google_cache:
            # Circuit breaker: once Google is clearly blocking us, stop
            # hitting it this run — failed leads are retried next run.
            if google_fails_in_a_row >= 3:
                google_cache[domain] = {"advertises_google": "check_failed"}
            else:
                google_cache[domain] = check_google_ads(domain, session)
                if google_cache[domain]["advertises_google"] == "check_failed":
                    google_fails_in_a_row += 1
                else:
                    google_fails_in_a_row = 0
                time.sleep(8)  # ~15 fast requests trip Google's limiter; stay well under
        biz.update(google_cache[domain])
        if biz.get("advertises_google") == "yes":
            biz["google_advertiser_type"] = classify_advertiser(
                biz.get("google_advertiser", ""), biz.get("name", ""),
                domain, biz.get("apollo_city") or biz.get("address", ""))
        biz.update(sniff_site_tags(biz["website"], session))
        biz["ads_checked"] = True
    return businesses
