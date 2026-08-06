"""
scrape_places_apify.py — Apify Google Maps + Contact Details lead discovery.

Drop-in replacement for scrape_places.py. SAME CLI + SAME DB contract:
    python scrape_places_apify.py --campaign-id 1 --db-path leads.db [--max-places 50]

Reads target_geography (zips) + target_business_types from the campaign, runs the
Apify actor `lukaskrivka/google-maps-with-contact-details` (Google Maps scrape +
website email/social scrape), and writes businesses to PipelineContacts.

Email logic:
    - Apify returns an email  -> store email + socials, status = 'enriched' (skips waterfall)
    - No email                -> status = 'new'  (flows into the existing waterfall enrichment)

Auth: APIFY_TOKEN env var, or secrets/apify.json {"token": "..."}.
"""
import argparse, json, os, re, sys, time, urllib.request, urllib.error

# placeholder / non-deliverable emails the scraper sometimes returns from parked or template sites
JUNK_EMAIL = re.compile(
    r"(filler@|no-?reply|do-?not-?reply|example\.(com|org|net)|godaddy|wixpress|"
    r"sentry\.io|sentry-|@sentry|your@|youremail|user@|email@example|name@|"
    r"domain\.com|yourdomain|test@|info@godaddy|@wix\.com|@squarespace|cloudflare|"
    r"\.(png|jpg|jpeg|gif|webp)$)", re.I)

def clean_emails(emails):
    return [e for e in (emails or []) if e and "@" in e and not JUNK_EMAIL.search(e)]

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
from pipeline_db import PipelineDB

ACTOR = "lukaskrivka~google-maps-with-contact-details"
APIFY_BASE = "https://api.apify.com/v2"

def _token():
    t = os.environ.get("APIFY_TOKEN", "").strip()
    if t:
        return t
    # Optional: a git-ignored local secrets file at <repo>/secrets/apify.json. NEVER commit a real token.
    secrets_path = os.path.join(os.path.dirname(_SCRIPT_DIR), "secrets", "apify.json")
    try:
        return json.load(open(secrets_path, encoding="utf-8-sig")).get("token", "").strip()
    except Exception:
        return ""

APIFY_TOKEN = _token()

STATE_ABBR = {"florida": "FL", "georgia": "GA", "alabama": "AL", "tennessee": "TN",
              "south carolina": "SC", "north carolina": "NC", "new york": "NY", "california": "CA",
              "texas": "TX", "illinois": "IL"}

def _post(url, body, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def _get(url, timeout=120):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def humanize_type(t):
    return (t or "").replace("_", " ").strip()

def run_actor(search_strings, max_places, poll_timeout=2400):
    """Start the actor async, poll to completion (or timeout), return (items, status)."""
    body = {
        "searchStringsArray": search_strings,
        "maxCrawledPlacesPerSearch": max_places,
        "language": "en",
        "maxReviews": 0,
        "maxImages": 0,
        "scrapeContacts": True,
        "skipClosedPlaces": True,
    }
    start = _post(f"{APIFY_BASE}/acts/{ACTOR}/runs?token={APIFY_TOKEN}&timeout=3000&memory=4096", body)["data"]
    run_id, dataset_id, status = start["id"], start["defaultDatasetId"], start["status"]
    deadline = time.time() + poll_timeout
    while status in ("READY", "RUNNING") and time.time() < deadline:
        time.sleep(15)
        try:
            status = _get(f"{APIFY_BASE}/actor-runs/{run_id}?token={APIFY_TOKEN}")["data"]["status"]
        except Exception as e:
            sys.stderr.write(f"  poll error: {e}\n")
    items = _get(f"{APIFY_BASE}/datasets/{dataset_id}/items?token={APIFY_TOKEN}&clean=true")
    return items, status

def norm_state(s):
    if not s:
        return None
    return STATE_ABBR.get(s.strip().lower(), s.strip()[:2].upper() if len(s.strip()) > 2 else s.strip())

def item_to_contact(it, campaign_id, scrape_id):
    emails = clean_emails(it.get("emails"))
    email = emails[0] if emails else None
    fb = (it.get("facebooks") or [None])[0]
    ig = (it.get("instagrams") or [None])[0]
    li = (it.get("linkedIns") or [None])[0]
    phone = it.get("phone") or it.get("phoneUnformatted") or (it.get("phones") or [None])[0]
    c = {
        "campaign_id": campaign_id,
        "google_place_id": it.get("placeId"),
        "business_name": it.get("title") or "Unknown",
        "primary_type": it.get("categoryName"),
        "address": it.get("address"),
        "city": it.get("city"),
        "state": norm_state(it.get("state")),
        "zip_code": it.get("postalCode"),
        "neighborhood": it.get("neighborhood"),
        "phone": phone,
        "website": it.get("website"),
        "google_rating": it.get("totalScore"),
        "review_count": it.get("reviewsCount"),
        "social_facebook": fb,
        "social_instagram": ig,
        "social_linkedin": li,
        "has_social_presence": 1 if (fb or ig or li) else 0,
        "status": "new",
        "source_type": "scrape",
        "source_scrape_id": scrape_id,
    }
    if email:
        c["email"] = email
        c["status"] = "enriched"   # Apify already found the email; skip the waterfall
    return c

def run_scrape(db, campaign_id, max_places, dry_run=False, max_searches=0):
    campaign = db.get_campaign(campaign_id=campaign_id)
    if not campaign:
        print(f"ERROR: Campaign {campaign_id} not found"); return
    if campaign["status"] != "active":
        print(f"ERROR: Campaign '{campaign['name']}' is {campaign['status']}, not active"); return

    zip_codes = json.loads(campaign.get("target_geography") or "[]")
    business_types = json.loads(campaign.get("target_business_types") or "[]")
    if not zip_codes:
        print("ERROR: No target_geography configured for this campaign"); return

    # build search strings: one per (type, zip); fallback to broad if no types
    searches = []
    if business_types:
        for z in zip_codes:
            for t in business_types:
                searches.append(f"{humanize_type(t)} in {z}")
    else:
        searches = [f"businesses in {z}" for z in zip_codes]

    if max_searches and len(searches) > max_searches:
        print(f"(capping {len(searches)} searches -> {max_searches} for this run)")
        searches = searches[:max_searches]

    scrape_id = db.start_scrape(campaign_id=campaign_id, zip_codes=",".join(zip_codes),
                                place_types=json.dumps(business_types) if business_types else None)
    print(f"Apify run: {len(searches)} searches x up to {max_places} places each")

    items, status = run_actor(searches, max_places)
    print(f"Apify run status: {status}; dataset items: {len(items)}")
    db.log_api_call("apify_gmaps_contacts", "google-maps-with-contact-details", len(items))

    total_found = new_leads = duplicates = with_email = 0
    for it in items:
        total_found += 1
        gid = it.get("placeId")
        if it.get("permanentlyClosed") or it.get("temporarilyClosed"):
            continue
        if gid and db.contact_exists(google_place_id=gid):
            duplicates += 1; continue
        contact = item_to_contact(it, campaign_id, scrape_id)
        if dry_run:
            new_leads += 1
            if contact.get("email"): with_email += 1
            print(f"  [DRY] {contact['status']:8} | {contact['business_name'][:30]:30} | "
                  f"{contact.get('city')},{contact.get('state')} {contact.get('zip_code')} | "
                  f"email={contact.get('email') or '-'} | ph={contact.get('phone') or '-'}")
            continue
        try:
            db.insert_contact(**contact)
            new_leads += 1
            if contact.get("email"):
                with_email += 1
        except Exception as e:
            sys.stderr.write(f"  WARN insert failed for {contact.get('business_name')}: {e}\n")
            duplicates += 1

    if not dry_run:
        db.finish_scrape(scrape_id, total_found, new_leads, duplicates, len(searches),
                         "completed" if status == "SUCCEEDED" else "partial")
    print(f"\n=== Apify Scrape Complete ===")
    print(f"Campaign: {campaign['name']}  | Zips: {', '.join(zip_codes)}")
    print(f"Dataset items: {total_found} | New leads: {new_leads} | Dupes: {duplicates}")
    print(f"With email (-> enriched): {with_email} | Without email (-> new, waterfall): {new_leads - with_email}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Apify Google Maps lead discovery for a campaign")
    ap.add_argument("--campaign-id", type=int, required=True)
    ap.add_argument("--db-path", type=str, default=None)
    ap.add_argument("--max-places", type=int, default=int(os.environ.get("APIFY_MAX_PLACES", "50")))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-searches", type=int, default=0, help="cap number of search strings (testing)")
    args = ap.parse_args()
    if not APIFY_TOKEN:
        print("ERROR: Set APIFY_TOKEN env var or secrets/apify.json"); sys.exit(1)
    db = PipelineDB(args.db_path)
    run_scrape(db, args.campaign_id, args.max_places, dry_run=args.dry_run, max_searches=args.max_searches)
