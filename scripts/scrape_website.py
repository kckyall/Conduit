"""
scrape_website.py — Extract contact info from business websites.
Used by the pipeline during gap-fill to find emails, social profiles,
contact names, and years-in-business from business websites.

Usage:
    python scrape_website.py --url "https://example.com" [--verbose]

Returns JSON with extracted fields. Also callable as a module:
    from scrape_website import scrape_site
    result = scrape_site("https://example.com")
"""

import argparse
import json
import re
import sys
import requests
from urllib.parse import urljoin, urlparse

# Timeout for HTTP requests
REQUEST_TIMEOUT = 10

# Pages to check (in priority order)
TARGET_PAGES = [
    "",           # Homepage
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
    "/our-team",
    "/team",
    "/our-story",
    "/staff",
]

# Email regex (basic but effective)
EMAIL_RE = re.compile(
    r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b'
)

# Social media URL patterns
SOCIAL_PATTERNS = {
    "facebook": re.compile(r'https?://(?:www\.)?facebook\.com/[A-Za-z0-9._\-]+/?', re.I),
    "instagram": re.compile(r'https?://(?:www\.)?instagram\.com/[A-Za-z0-9._\-]+/?', re.I),
    "linkedin": re.compile(r'https?://(?:www\.)?linkedin\.com/(?:company|in)/[A-Za-z0-9._\-]+/?', re.I),
}

# "Since YYYY" or "Founded in YYYY" or "Est. YYYY" or "Established YYYY"
YEAR_PATTERNS = [
    re.compile(r'(?:since|founded(?:\s+in)?|est\.?|established)\s+(\d{4})', re.I),
    re.compile(r'(?:serving|proudly\s+serving).*?(?:since|for\s+over)\s+(\d{4}|\d+)\s*(?:years?)?', re.I),
]

# Junk email patterns to skip (prefix matches)
JUNK_EMAILS = {
    "noreply@", "no-reply@", "mailer-daemon@",
    "sentry@", "wix@", "squarespace@", "wordpress@",
    "example@", "email@", "youremail@", "info@example",
    "john@example", "jane@example", "test@", "user@domain",
    "name@", "your@", "someone@",
}

# Junk TLDs / domains that aren't real email providers
JUNK_DOMAINS = {
    "wixpress.com", "squarespace.com", "godaddy.com", "mailchimp.com",
    "sentry.io", "example.com", "domain.com", "email.com",
    "yoursite.com", "yourdomain.com", "company.com",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def is_junk_email(email):
    email_lower = email.lower()
    local_part = email_lower.split("@")[0]
    domain = email_lower.split("@")[-1]

    # Check junk prefixes
    for pattern in JUNK_EMAILS:
        if pattern in email_lower:
            return True

    # Check junk domains (exact match OR subdomain match)
    for junk_domain in JUNK_DOMAINS:
        if domain == junk_domain or domain.endswith("." + junk_domain):
            return True

    # Reject UUID-style local parts (hex strings 16+ chars, common in Wix/Sentry tracking pixels)
    if re.match(r'^[0-9a-f]{16,}$', local_part.replace("-", "")):
        return True

    # Reject generic placeholder emails
    if email_lower in ("info@website.com", "hi@mystore.com", "info@physiotherapy.com"):
        return True

    # Reject if any part looks like a filename (image/asset extensions)
    if any(ext in email_lower for ext in [".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf", ".css", ".js"]):
        return True

    # Reject if domain part looks fake (too short, no dots, or image-like)
    if "." not in domain or len(domain) < 4:
        return True

    # Reject if domain TLD is not a real email TLD
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    fake_tlds = {"png", "jpg", "jpeg", "gif", "svg", "webp", "pdf", "css", "js", "html"}
    if tld in fake_tlds:
        return True

    return False


def fetch_page(url, verbose=False):
    """Fetch a page, return text or None."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200:
            return resp.text
        if verbose:
            print(f"  [{resp.status_code}] {url}")
    except Exception as e:
        if verbose:
            print(f"  [ERR] {url}: {e}")
    return None


def extract_emails(html):
    """Extract valid emails from HTML."""
    found = set(EMAIL_RE.findall(html))
    # Filter junk
    return [e for e in found if not is_junk_email(e)]


def extract_social(html):
    """Extract social media profile URLs."""
    result = {}
    for platform, pattern in SOCIAL_PATTERNS.items():
        matches = pattern.findall(html)
        if matches:
            # Take the first match, strip trailing slash
            url = matches[0].rstrip("/")
            # Skip generic social URLs (just the domain)
            path = urlparse(url).path.strip("/")
            if path and path not in {"share", "sharer", "intent", "dialog"}:
                result[platform] = url
    return result


def extract_years_in_business(html):
    """Try to find founding year or years in business."""
    from datetime import datetime
    current_year = datetime.now().year

    for pattern in YEAR_PATTERNS:
        match = pattern.search(html)
        if match:
            val = match.group(1)
            try:
                num = int(val)
                if 1900 <= num <= current_year:
                    # It's a year
                    return current_year - num
                elif 1 <= num <= 150:
                    # It's a number of years
                    return num
            except ValueError:
                continue
    return None


def scrape_site(base_url, verbose=False):
    """
    Scrape a business website for contact info.
    Returns dict with: emails, social_facebook, social_instagram,
    social_linkedin, years_in_business, pages_checked
    """
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    base_url = base_url.rstrip("/")

    all_emails = []
    all_social = {}
    years_in_business = None
    pages_checked = 0

    for page_path in TARGET_PAGES:
        url = base_url + page_path if page_path else base_url
        html = fetch_page(url, verbose)
        if not html:
            continue
        pages_checked += 1

        # Extract emails
        emails = extract_emails(html)
        all_emails.extend(emails)

        # Extract social
        social = extract_social(html)
        for platform, surl in social.items():
            if platform not in all_social:
                all_social[platform] = surl

        # Extract years
        if years_in_business is None:
            years_in_business = extract_years_in_business(html)

        if verbose:
            print(f"  [OK] {url} — emails: {len(emails)}, social: {list(social.keys())}")

    # Deduplicate and prioritize emails
    seen = set()
    unique_emails = []
    for e in all_emails:
        if e.lower() not in seen:
            seen.add(e.lower())
            unique_emails.append(e)

    # Sort: prefer non-info@ emails first, then info@
    def email_priority(email):
        local = email.split("@")[0].lower()
        if local in ("info", "contact", "hello", "office"):
            return 1
        return 0

    unique_emails.sort(key=email_priority)

    return {
        "emails": unique_emails,
        "primary_email": unique_emails[0] if unique_emails else None,
        "social_facebook": all_social.get("facebook"),
        "social_instagram": all_social.get("instagram"),
        "social_linkedin": all_social.get("linkedin"),
        "has_social_presence": len(all_social) > 0,
        "years_in_business": years_in_business,
        "pages_checked": pages_checked,
    }


def enrich_campaign(campaign_id, db_path=None, verbose=False):
    """Batch-scrape websites for all qualified leads in a campaign."""
    from pipeline_db import PipelineDB
    import time

    db = PipelineDB(db_path)
    leads = db.get_contacts(campaign_id, status="qualified")
    if not leads:
        print("No qualified leads to enrich.")
        return

    total = len(leads)
    enriched = 0
    emails_found = 0
    no_website = 0
    failed = 0

    print(f"Enriching {total} qualified leads for campaign {campaign_id}...")
    print(f"{'='*60}")

    for i, lead in enumerate(leads, 1):
        bname = lead.get("business_name") or "Unknown"
        website = lead.get("website")

        if not website:
            no_website += 1
            if verbose:
                print(f"  [{i}/{total}] SKIP (no website): {bname}")
            continue

        try:
            result = scrape_site(website, verbose=verbose)

            updates = {}
            if result["primary_email"]:
                updates["email"] = result["primary_email"]
                emails_found += 1
            if result["social_facebook"]:
                updates["social_facebook"] = result["social_facebook"]
            if result["social_instagram"]:
                updates["social_instagram"] = result["social_instagram"]
            if result["social_linkedin"]:
                updates["social_linkedin"] = result["social_linkedin"]
            if result["has_social_presence"]:
                updates["has_social_presence"] = 1
            if result["years_in_business"] is not None:
                updates["years_in_business"] = result["years_in_business"]

            # Assess website quality based on pages found
            if result["pages_checked"] >= 3:
                updates["website_quality"] = "professional"
            elif result["pages_checked"] >= 1:
                updates["website_quality"] = "basic"
            else:
                updates["website_quality"] = "none"

            # Move to enriched status
            updates["status"] = "enriched"
            db.update_contact(lead["id"], **updates)
            enriched += 1

            email_str = f" -> {result['primary_email']}" if result['primary_email'] else " (no email)"
            social_str = f" | social: {', '.join(k for k in ['facebook','instagram','linkedin'] if result.get(f'social_{k}'))}" if result['has_social_presence'] else ""
            print(f"  [{i}/{total}] {bname}{email_str}{social_str}")

            # Be polite to websites
            time.sleep(0.5)

        except Exception as e:
            failed += 1
            print(f"  [{i}/{total}] FAILED: {bname} — {e}")
            continue

    print(f"\n{'='*60}")
    print(f"=== Website Enrichment Complete ===")
    print(f"Total qualified leads: {total}")
    print(f"Enriched: {enriched}")
    print(f"Emails found: {emails_found}")
    print(f"No website: {no_website}")
    print(f"Failed: {failed}")
    print(f"Email hit rate: {emails_found}/{enriched} ({100*emails_found/max(enriched,1):.0f}%)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape a business website for contact info")
    parser.add_argument("--url", help="Single business website URL")
    parser.add_argument("--campaign-id", type=int, help="Batch enrich all qualified leads in a campaign")
    parser.add_argument("--db-path", type=str, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.campaign_id:
        enrich_campaign(args.campaign_id, db_path=args.db_path, verbose=args.verbose)
    elif args.url:
        result = scrape_site(args.url, verbose=args.verbose)
        print(json.dumps(result, indent=2))
    else:
        parser.error("Provide either --url or --campaign-id")
