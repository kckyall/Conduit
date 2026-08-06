"""
scrape_websites_batch.py — Batch website scraping for pipeline contacts.
Used by the pipeline after Google Places scrape to gap-fill emails, social
profiles, and years-in-business from business websites.

This is the PRIMARY email discovery method for local businesses.
Apollo enrichment is a secondary/supplemental source.

Usage:
    python scrape_websites_batch.py --campaign-id 3 --db-path leads.db [--status qualified] [--limit 50]

For each contact with a website URL, crawls the site and fills:
  - email (from /contact, /about, homepage, etc.)
  - social_facebook, social_instagram, social_linkedin
  - years_in_business
  - has_social_presence, website_quality

Gap-fill only: never overwrites existing data.
"""

import argparse
import json
import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Import the existing single-site scraper
from scrape_website import scrape_site


from pipeline_db import PipelineDB


def assess_website_quality(scrape_result, website_url):
    """
    Rate website quality based on what we found.
    Returns: 'none', 'basic', 'professional', or 'strong'
    (matches DB CHECK constraint)
    """
    if scrape_result["pages_checked"] == 0:
        return "none"  # site is dead/unreachable

    score = 0
    if scrape_result["pages_checked"] >= 3:
        score += 1  # multi-page site
    if scrape_result["primary_email"]:
        score += 1  # has email
    if scrape_result["has_social_presence"]:
        score += 1  # has social links
    if scrape_result["years_in_business"]:
        score += 1  # established business

    if score >= 3:
        return "strong"
    elif score >= 2:
        return "professional"
    else:
        return "basic"


def run_batch_scrape(db, campaign_id, status="qualified", limit=100, verbose=False):
    """Scrape websites for all contacts with a website URL."""
    contacts = db.get_contacts(campaign_id, status=status, limit=limit)

    if not contacts:
        print(f"No '{status}' contacts found for campaign {campaign_id}.")
        return

    # Filter to those with a website but missing email
    needs_scrape = []
    for c in contacts:
        website = c.get("website")
        if not website:
            continue
        # Scrape if missing email OR missing social presence data
        if not c.get("email") or not c.get("social_facebook") and not c.get("social_instagram"):
            needs_scrape.append(c)

    if not needs_scrape:
        print(f"All {len(contacts)} contacts already have email + social data. Nothing to scrape.")
        return

    print(f"Found {len(needs_scrape)} contacts needing website scrape (of {len(contacts)} {status})")
    print(f"{'='*60}")

    scraped = 0
    emails_found = 0
    social_found = 0
    failed = 0

    for contact in needs_scrape:
        website = contact["website"]
        bname = contact.get("business_name") or "Unknown"

        print(f"\n  [{scraped+1}/{len(needs_scrape)}] {bname}")
        print(f"    URL: {website}")

        try:
            result = scrape_site(website, verbose=verbose)
        except Exception as e:
            print(f"    FAILED: {e}")
            failed += 1
            continue

        scraped += 1

        # Build gap-fill updates (only fill empty fields)
        updates = {}

        # Email — primary discovery method
        if result.get("primary_email") and not contact.get("email"):
            updates["email"] = result["primary_email"]
            emails_found += 1
            print(f"    EMAIL: {result['primary_email']}")

        # Social profiles
        if result.get("social_facebook") and not contact.get("social_facebook"):
            updates["social_facebook"] = result["social_facebook"]
        if result.get("social_instagram") and not contact.get("social_instagram"):
            updates["social_instagram"] = result["social_instagram"]
        if result.get("social_linkedin") and not contact.get("social_linkedin"):
            updates["social_linkedin"] = result["social_linkedin"]

        if result.get("has_social_presence"):
            social_found += 1
            socials = [k for k in ["social_facebook", "social_instagram", "social_linkedin"]
                       if result.get(k.replace("social_", ""))]
            if socials or result.get("social_facebook") or result.get("social_instagram"):
                found_platforms = []
                if result.get("social_facebook"):
                    found_platforms.append("FB")
                if result.get("social_instagram"):
                    found_platforms.append("IG")
                if result.get("social_linkedin"):
                    found_platforms.append("LI")
                print(f"    SOCIAL: {', '.join(found_platforms)}")

        # Years in business
        if result.get("years_in_business") and not contact.get("years_in_business"):
            updates["years_in_business"] = result["years_in_business"]
            print(f"    YEARS: {result['years_in_business']} years")

        # Website quality + social presence flags
        quality = assess_website_quality(result, website)
        if quality and not contact.get("website_quality"):
            updates["website_quality"] = quality

        has_social = 1 if result.get("has_social_presence") else 0
        if not contact.get("has_social_presence"):
            updates["has_social_presence"] = has_social

        if updates:
            db.update_contact(contact["id"], **updates)
            fields = [k for k in updates if k not in ("website_quality", "has_social_presence")]
            if fields:
                print(f"    FILLED: {', '.join(fields)}")
        else:
            print(f"    (no new data)")

        # Be polite to web servers
        time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"=== Website Scrape Complete ===")
    print(f"Campaign: {campaign_id}")
    print(f"Websites scraped: {scraped}")
    print(f"Emails found: {emails_found}")
    print(f"Social profiles found: {social_found}")
    print(f"Failed: {failed}")
    print(f"Email hit rate: {emails_found}/{scraped} ({100*emails_found//max(scraped,1)}%)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch website scraping for pipeline contacts")
    parser.add_argument("--campaign-id", type=int, required=True)
    parser.add_argument("--db-path", type=str, default=None)
    parser.add_argument("--status", type=str, default="qualified",
                        help="Contact status to scrape (default: qualified)")
    parser.add_argument("--limit", type=int, default=100,
                        help="Max contacts to scrape (default: 100)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    db = PipelineDB(args.db_path)
    run_batch_scrape(db, args.campaign_id, status=args.status, limit=args.limit, verbose=args.verbose)
