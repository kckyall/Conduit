"""
compare_and_import.py — Compare Apify CSV against pipeline DB,
import missing businesses, and backfill emails from Apify into existing records.

Usage:
    python compare_and_import.py --csv path/to/apify.csv --campaign-id 4 --db-path leads.db --report
    python compare_and_import.py --csv path/to/apify.csv --campaign-id 4 --db-path leads.db --import
"""

import argparse
import csv
import json
import os
import re
import sys
from difflib import SequenceMatcher

from pipeline_db import PipelineDB


def normalize_name(name):
    """Normalize business name for fuzzy matching."""
    if not name:
        return ""
    name = name.lower().strip()
    # Remove common suffixes
    for suffix in [" llc", " inc", " corp", " co", " llp", " pllc", " pa", " p.a.", ", llc", ", inc"]:
        name = name.replace(suffix, "")
    # Remove punctuation
    name = re.sub(r'[^\w\s]', '', name)
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def normalize_phone(phone):
    """Normalize phone to digits only."""
    if not phone:
        return ""
    return re.sub(r'\D', '', str(phone))[-10:]  # Last 10 digits


def fuzzy_match(a, b, threshold=0.85):
    """Check if two strings are a fuzzy match."""
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


def load_apify_csv(csv_path):
    """Load and parse the Apify CSV."""
    leads = []
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            leads.append({
                "title": (row.get("title") or "").strip(),
                "title_norm": normalize_name(row.get("title") or ""),
                "category": (row.get("categoryName") or "").strip(),
                "address": (row.get("address") or "").strip(),
                "city": (row.get("city") or "").strip(),
                "state": (row.get("state") or "").strip(),
                "zip": (row.get("postalCode") or "").strip(),
                "phone": (row.get("phone") or "").strip(),
                "phone_norm": normalize_phone(row.get("phone") or row.get("phoneUnformatted") or ""),
                "website": (row.get("website") or "").strip(),
                "email": (row.get("email") or "").strip() or None,
                "rating": float(row.get("totalScore") or 0) if row.get("totalScore") else None,
                "review_count": int(float(row.get("reviewsCount") or 0)) if row.get("reviewsCount") else None,
                "contact_name": (row.get("fullName") or "").strip() or None,
                "contact_title": (row.get("jobTitle") or "").strip() or None,
                "linkedin": (row.get("linkedinProfile") or "").strip() or None,
                "neighborhood": (row.get("neighborhood") or "").strip() or None,
                "target_tier": (row.get("TargetTier") or "").strip() or None,
                "company_linkedin": (row.get("companyLinkedin") or "").strip() or None,
                "social_facebook": None,
                "social_instagram": None,
            })
    return leads


def match_lead(apify_lead, db_leads_by_phone, db_leads_by_name):
    """Try to match an Apify lead to a DB lead. Returns (db_lead, match_type) or (None, None)."""
    # 1. Phone match (strongest signal)
    phone = apify_lead["phone_norm"]
    if phone and phone in db_leads_by_phone:
        return db_leads_by_phone[phone], "phone"

    # 2. Fuzzy name match
    norm = apify_lead["title_norm"]
    if norm and norm in db_leads_by_name:
        return db_leads_by_name[norm], "exact_name"

    # 3. Fuzzy name match with threshold
    for db_name, db_lead in db_leads_by_name.items():
        if fuzzy_match(norm, db_name, 0.85):
            return db_lead, "fuzzy_name"

    return None, None


def run_comparison(csv_path, campaign_id, db_path, do_import=False):
    db = PipelineDB(db_path)

    # Load Apify data
    apify_leads = load_apify_csv(csv_path)
    print(f"Apify CSV: {len(apify_leads)} businesses")
    apify_with_email = sum(1 for l in apify_leads if l["email"])
    print(f"Apify with email: {apify_with_email}")

    # Load DB data
    db_leads = db.get_contacts(campaign_id, limit=9999)
    print(f"Pipeline DB (campaign {campaign_id}): {len(db_leads)} businesses")
    db_with_email = sum(1 for l in db_leads if l.get("email"))
    print(f"Pipeline with email: {db_with_email}")
    print(f"{'='*60}")

    # Build lookup indexes for DB leads
    db_by_phone = {}
    db_by_name = {}
    for lead in db_leads:
        phone = normalize_phone(lead.get("phone") or "")
        if phone:
            db_by_phone[phone] = lead
        name = normalize_name(lead.get("business_name") or "")
        if name:
            db_by_name[name] = lead

    # Categorize Apify leads
    matched_has_email_we_dont = []   # In both, Apify has email, we don't
    matched_both_have_email = []     # In both, both have email
    matched_no_email_either = []     # In both, neither has email
    new_with_email = []              # Only in Apify, has email
    new_no_email = []                # Only in Apify, no email
    matched_we_have_email = []       # In both, we have email, they don't

    for apify_lead in apify_leads:
        db_lead, match_type = match_lead(apify_lead, db_by_phone, db_by_name)

        if db_lead:
            # Matched — check email status
            apify_email = apify_lead["email"]
            db_email = db_lead.get("email")

            if apify_email and not db_email:
                matched_has_email_we_dont.append((apify_lead, db_lead, match_type))
            elif apify_email and db_email:
                matched_both_have_email.append((apify_lead, db_lead, match_type))
            elif not apify_email and db_email:
                matched_we_have_email.append((apify_lead, db_lead, match_type))
            else:
                matched_no_email_either.append((apify_lead, db_lead, match_type))
        else:
            # Not in our DB
            if apify_lead["email"]:
                new_with_email.append(apify_lead)
            else:
                new_no_email.append(apify_lead)

    # Report
    total_matched = (len(matched_has_email_we_dont) + len(matched_both_have_email) +
                     len(matched_no_email_either) + len(matched_we_have_email))

    print(f"\n--- MATCH RESULTS ---")
    print(f"Matched to pipeline: {total_matched}")
    print(f"  - Apify has email, we don't: {len(matched_has_email_we_dont)} <-- BACKFILL THESE")
    print(f"  - Both have email: {len(matched_both_have_email)}")
    print(f"  - We have email, Apify doesn't: {len(matched_we_have_email)}")
    print(f"  - Neither has email: {len(matched_no_email_either)}")
    print(f"")
    print(f"NEW (not in pipeline): {len(new_with_email) + len(new_no_email)}")
    print(f"  - With email: {len(new_with_email)} <-- IMPORT AS ENRICHED")
    print(f"  - Without email: {len(new_no_email)} <-- IMPORT + RUN WEBSITE SCRAPER")

    # Show backfill details
    if matched_has_email_we_dont:
        print(f"\n--- BACKFILL: Apify emails for existing leads ---")
        for apify, db_lead, mtype in matched_has_email_we_dont[:20]:
            print(f"  {db_lead['business_name']} <-- {apify['email']} (matched by {mtype})")
        if len(matched_has_email_we_dont) > 20:
            print(f"  ... and {len(matched_has_email_we_dont) - 20} more")

    # Show new leads with email
    if new_with_email:
        print(f"\n--- NEW LEADS WITH EMAIL (sample) ---")
        for lead in new_with_email[:15]:
            print(f"  {lead['title']} | {lead['email']} | {lead['zip']} | {lead['category']}")
        if len(new_with_email) > 15:
            print(f"  ... and {len(new_with_email) - 15} more")

    # Show new leads without email
    if new_no_email:
        print(f"\n--- NEW LEADS WITHOUT EMAIL (sample) ---")
        for lead in new_no_email[:10]:
            print(f"  {lead['title']} | {lead['zip']} | {lead['category']}")
        if len(new_no_email) > 10:
            print(f"  ... and {len(new_no_email) - 10} more")

    # Do the import if requested
    if do_import:
        print(f"\n{'='*60}")
        print(f"=== IMPORTING ===")

        # 1. Backfill emails + contact info into existing leads
        backfilled = 0
        for apify, db_lead, mtype in matched_has_email_we_dont:
            updates = {"email": apify["email"]}
            if apify["contact_name"] and not db_lead.get("contact_name"):
                updates["contact_name"] = apify["contact_name"]
                updates["display_contact"] = apify["contact_name"].split()[0] if apify["contact_name"] else None
            if apify["contact_title"] and not db_lead.get("contact_title"):
                updates["contact_title"] = apify["contact_title"]
            if apify["linkedin"] and not db_lead.get("social_linkedin"):
                updates["social_linkedin"] = apify["linkedin"]
            if apify["company_linkedin"] and not db_lead.get("social_linkedin"):
                updates["social_linkedin"] = apify["company_linkedin"]
            db.update_contact(db_lead["id"], **updates)
            backfilled += 1
        print(f"Backfilled emails into {backfilled} existing leads")

        # 2. Also backfill contact names/linkedin for leads where both have email
        contact_updated = 0
        for apify, db_lead, mtype in matched_both_have_email:
            updates = {}
            if apify["contact_name"] and not db_lead.get("contact_name"):
                updates["contact_name"] = apify["contact_name"]
                updates["display_contact"] = apify["contact_name"].split()[0] if apify["contact_name"] else None
            if apify["contact_title"] and not db_lead.get("contact_title"):
                updates["contact_title"] = apify["contact_title"]
            if apify["linkedin"] and not db_lead.get("social_linkedin"):
                updates["social_linkedin"] = apify["linkedin"]
            if updates:
                db.update_contact(db_lead["id"], **updates)
                contact_updated += 1
        print(f"Updated contact info for {contact_updated} existing leads")

        # 3. Import new leads with email as enriched
        imported_enriched = 0
        for lead in new_with_email:
            try:
                db.insert_contact(
                    campaign_id=campaign_id,
                    business_name=lead["title"],
                    display_name=lead["title"],
                    primary_type=lead["category"],
                    address=lead["address"],
                    city=lead["city"],
                    state=lead["state"],
                    zip_code=lead["zip"],
                    neighborhood=lead["neighborhood"],
                    phone=lead["phone"],
                    website=lead["website"] or None,
                    email=lead["email"],
                    contact_name=lead["contact_name"],
                    contact_title=lead["contact_title"],
                    display_contact=lead["contact_name"].split()[0] if lead["contact_name"] else None,
                    social_linkedin=lead["linkedin"] or lead["company_linkedin"],
                    google_rating=lead["rating"],
                    review_count=lead["review_count"],
                    status="enriched",
                    source_type="manual_batch",
                    source_reference="Apify import",
                )
                imported_enriched += 1
            except Exception as e:
                print(f"  SKIP (dup?): {lead['title']} — {e}")
        print(f"Imported {imported_enriched} new leads WITH email (status=enriched)")

        # 4. Import new leads without email as qualified (for website scraping)
        imported_qualified = 0
        for lead in new_no_email:
            try:
                db.insert_contact(
                    campaign_id=campaign_id,
                    business_name=lead["title"],
                    display_name=lead["title"],
                    primary_type=lead["category"],
                    address=lead["address"],
                    city=lead["city"],
                    state=lead["state"],
                    zip_code=lead["zip"],
                    neighborhood=lead["neighborhood"],
                    phone=lead["phone"],
                    website=lead["website"] or None,
                    google_rating=lead["rating"],
                    review_count=lead["review_count"],
                    status="qualified",
                    source_type="manual_batch",
                    source_reference="Apify import — needs website scrape for email",
                )
                imported_qualified += 1
            except Exception as e:
                print(f"  SKIP (dup?): {lead['title']} — {e}")
        print(f"Imported {imported_qualified} new leads WITHOUT email (status=qualified, ready for scraper)")

        print(f"\n=== IMPORT COMPLETE ===")
        print(f"Backfilled emails: {backfilled}")
        print(f"Contact info updated: {contact_updated}")
        print(f"New enriched (has email): {imported_enriched}")
        print(f"New qualified (needs scrape): {imported_qualified}")
        print(f"Total pipeline leads now: {len(db.get_contacts(campaign_id, limit=9999))}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare Apify CSV to pipeline and import missing leads")
    parser.add_argument("--csv", required=True, help="Path to Apify CSV")
    parser.add_argument("--campaign-id", type=int, required=True)
    parser.add_argument("--db-path", type=str, default=None)
    parser.add_argument("--report", action="store_true", help="Just show the comparison, don't import")
    parser.add_argument("--import", dest="do_import", action="store_true", help="Actually import missing leads")
    args = parser.parse_args()

    if not args.report and not args.do_import:
        args.report = True  # Default to report mode

    run_comparison(args.csv, args.campaign_id, args.db_path, do_import=args.do_import)
