"""
qualify.py — Score and qualify enriched leads before Apollo enrollment.

For each 'enriched' contact:
  * enforce a deliverable (non-junk) email,
  * compute a deterministic fit_score (1-10),
  * apply the suppression / do-not-contact blocklist,
  * transition status to 'qualified' (ready to enroll) or 'disqualified'.

No external APIs — pure, deterministic, and safe to run offline / in tests.
"""
import argparse
import json
import re

from pipeline_db import PipelineDB

JUNK_EMAIL = re.compile(
    r"(filler@|no-?reply|do-?not-?reply|example\.(com|org|net)|godaddy|wixpress|"
    r"sentry|your@|youremail|user@|email@example|name@|domain\.com|yourdomain|test@|"
    r"@wix\.com|@squarespace|cloudflare)", re.I)

FIT_THRESHOLD = 4  # minimum fit_score to qualify


def is_deliverable(email):
    return bool(email) and "@" in email and not JUNK_EMAIL.search(email)


def fit_score(contact):
    """Deterministic 1-10 fit score from signals already in the record."""
    score = 1
    if is_deliverable(contact.get("email")):
        score += 4
    if contact.get("website"):
        score += 1
    if contact.get("has_social_presence"):
        score += 1
    rc = contact.get("review_count") or 0
    if rc >= 10:
        score += 1
    if (contact.get("google_rating") or 0) >= 4.0:
        score += 1
    wq = contact.get("website_quality")
    if wq in ("professional", "strong"):
        score += 1
    return min(score, 10)


def suppressed(contact, blocklist):
    email = (contact.get("email") or "").lower()
    phone = contact.get("phone") or ""
    name = (contact.get("business_name") or "").lower()
    for b in blocklist:
        if b.get("email") and email and b["email"].lower() == email:
            return "email on suppression list"
        if b.get("phone") and phone and b["phone"] == phone:
            return "phone on suppression list"
        if b.get("business_name") and name and b["business_name"].lower() == name:
            return "business on suppression list"
    return None


def qualify_campaign(db, campaign_id, source_status="enriched", threshold=FIT_THRESHOLD):
    contacts = db.get_contacts(campaign_id, status=source_status)
    blocklist = db.get_safety_blocklist()
    summary = {"considered": len(contacts), "qualified": 0, "disqualified": 0, "suppressed": 0}

    for ct in contacts:
        reason = suppressed(ct, blocklist)
        if reason:
            db.update_contact(ct["id"], status="do_not_contact", disqualify_reason=reason)
            summary["suppressed"] += 1
            continue
        score = fit_score(ct)
        if is_deliverable(ct.get("email")) and score >= threshold:
            db.update_contact(ct["id"], status="qualified", fit_score=score)
            summary["qualified"] += 1
        else:
            why = "no deliverable email" if not is_deliverable(ct.get("email")) else f"fit_score {score} < {threshold}"
            db.update_contact(ct["id"], status="disqualified", fit_score=score, disqualify_reason=why)
            summary["disqualified"] += 1
    return summary


def main():
    ap = argparse.ArgumentParser(description="Qualify enriched leads")
    ap.add_argument("--campaign-id", type=int, required=True)
    ap.add_argument("--db-path", default=None)
    ap.add_argument("--status", default="enriched", help="Source status to qualify from")
    ap.add_argument("--threshold", type=int, default=FIT_THRESHOLD)
    a = ap.parse_args()
    db = PipelineDB(a.db_path)
    print(json.dumps(qualify_campaign(db, a.campaign_id, a.status, a.threshold), indent=2))


if __name__ == "__main__":
    main()
