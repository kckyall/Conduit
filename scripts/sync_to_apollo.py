"""
sync_to_apollo.py — Enroll qualified/enriched leads into an Apollo.io sequence.

Run after enrichment. Dry-run by default; pass --sync-live to actually enroll.

Safety layers (all enforced before a contact is enrolled):
  1. Local suppression / do-not-contact blocklist (customers, opted-out, warm, do_not_contact)
  2. Idempotency: skip leads already carrying an apollo_contact_id
  3. In-batch email de-duplication
  4. Remote: skip contacts already in any active Apollo sequence, and only enroll "Cold"-stage contacts
  5. Post-enroll verification: confirm the contact is actually in the sequence, else flag for retry

Everything is recorded to OutreachLog / PipelineContacts / ApiUsageLog. No contact data or API
keys are committed; the Apollo sequence and sender inbox are configured per-campaign.

Usage:
    python sync_to_apollo.py --campaign-id 1 [--db-path leads.db]            # dry-run
    python sync_to_apollo.py --campaign-id 1 --sync-live                     # actually enroll
"""

import argparse
import json
import os
import sys
import time

from pipeline_db import PipelineDB
from apollo_client import ApolloClient


def safety_check(lead, blocklist):
    """Return (is_safe, reason) — matches a lead against the do-not-contact blocklist."""
    lead_email = (lead.get("email") or "").lower()
    lead_phone = lead.get("phone") or ""
    lead_name = (lead.get("business_name") or "").lower()
    for b in blocklist:
        if b.get("email") and lead_email and b["email"].lower() == lead_email:
            return False, f"email matches blocked contact"
        if b.get("phone") and lead_phone and b["phone"] == lead_phone:
            return False, f"phone matches blocked contact"
        if b.get("business_name") and lead_name and b["business_name"].lower() == lead_name:
            return False, f"business name matches blocked contact"
    return True, None


def already_synced(lead):
    return bool(lead.get("apollo_contact_id"))


def _dedupe_by_email(leads):
    seen, out, dupes = set(), [], 0
    for lead in leads:
        email = (lead.get("email") or "").lower().strip()
        if not email:
            out.append(lead)
            continue
        if email in seen:
            dupes += 1
            continue
        seen.add(email)
        out.append(lead)
    return out, dupes


def sync_campaign(db, campaign_id, client=None, dry_run=True, email_account_id=None, source_status="enriched"):
    """Enroll a campaign's leads into its Apollo sequence. Returns a summary dict."""
    client = client or ApolloClient()
    campaign = db.get_campaign(campaign_id=campaign_id)
    if not campaign:
        raise ValueError(f"Campaign {campaign_id} not found")

    sync_enabled = campaign.get("sync_enabled", 0)
    max_batch = campaign.get("max_batch_size", 50)
    sequence_id = campaign.get("apollo_sequence_id")
    sequence_name = campaign.get("apollo_sequence_name") or "Unknown Sequence"
    whitelisted_raw = campaign.get("whitelisted_sequences")

    # ── live-mode gates ──
    if not dry_run:
        if not sync_enabled:
            raise PermissionError(
                f"sync_enabled is OFF for campaign '{campaign['name']}'. "
                f"Set Campaigns.sync_enabled=1 to allow live enrollment."
            )
        if not sequence_id:
            raise ValueError(
                f"No apollo_sequence_id configured for campaign '{campaign['name']}'."
            )
        if whitelisted_raw:
            allowed = json.loads(whitelisted_raw)
            if sequence_id not in allowed:
                raise PermissionError(
                    f"Sequence '{sequence_id}' not in whitelisted_sequences {allowed}."
                )
        if not client.configured:
            raise PermissionError("APOLLO_API_KEY is not set; cannot run live sync.")
    if not sequence_id:
        sequence_id = "DRY_RUN_NO_SEQUENCE"

    leads = db.get_contacts(campaign_id, status=source_status)
    if max_batch and max_batch > 0 and len(leads) > max_batch:
        leads = leads[:max_batch]
    leads, email_dupes = _dedupe_by_email(leads)
    blocklist = db.get_safety_blocklist()
    ab_enabled = bool(campaign.get("apollo_ab_enabled"))
    stage_map = client.get_stage_map() if not dry_run else {}

    summary = {
        "campaign": campaign["name"], "sequence": sequence_name, "sequence_id": sequence_id,
        "mode": "dry_run" if dry_run else "live", "candidates": len(leads),
        "email_dupes_removed": email_dupes, "blocklist_size": len(blocklist),
        "synced": 0, "already_in_apollo": 0, "already_in_sequence": 0, "blocked": 0,
        "skipped_no_email": 0, "skipped_already_synced": 0, "skipped_wrong_stage": 0,
        "enrollment_failed": 0, "failed": 0, "actions": [],
    }
    ab_counter = 0

    for lead in leads:
        name = lead.get("business_name") or "Unknown"

        if not lead.get("email"):
            summary["skipped_no_email"] += 1
            continue
        if already_synced(lead):
            summary["skipped_already_synced"] += 1
            continue

        is_safe, reason = safety_check(lead, blocklist)
        if not is_safe:
            db.update_contact(lead["id"], status="do_not_contact", disqualify_reason=reason)
            summary["blocked"] += 1
            summary["actions"].append({"business": name, "action": "blocked", "reason": reason})
            continue

        variant = None
        if ab_enabled:
            variant = "A" if ab_counter % 2 == 0 else "B"
            ab_counter += 1

        if dry_run:
            summary["synced"] += 1
            summary["actions"].append({"business": name, "action": "would_enroll",
                                       "email": lead["email"], "sequence": sequence_name,
                                       "variant": variant})
            continue

        # ── LIVE ──
        try:
            apollo_id = _live_enroll(db, client, lead, campaign, sequence_id, sequence_name,
                                     variant, email_account_id, stage_map, summary)
        except Exception as e:  # network / API failure -> record, continue
            summary["failed"] += 1
            db.update_contact(lead["id"], disqualify_reason=f"sync error: {e}")
            summary["actions"].append({"business": name, "action": "failed", "reason": str(e)})
            continue
        if apollo_id:
            summary["synced"] += 1
            summary["actions"].append({"business": name, "action": "enrolled",
                                       "apollo_id": apollo_id, "variant": variant})

    return summary


def _live_enroll(db, client, lead, campaign, sequence_id, sequence_name, variant,
                 email_account_id, stage_map, summary):
    """Enroll one lead live. Returns apollo_id on success, None if skipped/failed (summary updated)."""
    existing = client.search_contact(email=lead["email"])
    time.sleep(client.request_delay)

    apollo_id = None
    if existing:
        apollo_id = existing.get("id")
        summary["already_in_apollo"] += 1
        full = client.get_full_contact(apollo_id)
        time.sleep(client.request_delay)
        if full:
            in_seq, seq_names = client.active_sequences(full)
            if in_seq:
                summary["already_in_sequence"] += 1
                db.update_contact(lead["id"], apollo_contact_id=apollo_id,
                                  disqualify_reason=f"already in sequence(s): {', '.join(seq_names)}")
                return None
            safe, stage_name = client.stage_safe(full, stage_map)
            if not safe:
                summary["skipped_wrong_stage"] += 1
                db.update_contact(lead["id"], apollo_contact_id=apollo_id,
                                  disqualify_reason=f"stage not safe: {stage_name}")
                return None
        client.update_contact(apollo_id, lead)
        time.sleep(client.request_delay)
    else:
        contact = client.create_contact(lead)
        time.sleep(client.request_delay)
        if not contact:
            summary["failed"] += 1
            return None
        apollo_id = contact.get("id")
        full_new = client.get_full_contact(apollo_id)
        time.sleep(client.request_delay)
        if full_new:
            in_seq, seq_names = client.active_sequences(full_new)
            if in_seq:
                summary["already_in_sequence"] += 1
                db.update_contact(lead["id"], apollo_contact_id=apollo_id,
                                  disqualify_reason=f"already in sequence(s): {', '.join(seq_names)}")
                return None
            safe, stage_name = client.stage_safe(full_new, stage_map)
            if not safe:
                summary["skipped_wrong_stage"] += 1
                db.update_contact(lead["id"], apollo_contact_id=apollo_id,
                                  disqualify_reason=f"stage not safe: {stage_name}")
                return None

    # enroll
    client.add_to_sequence(apollo_id, sequence_id, email_account_id=email_account_id)
    time.sleep(client.request_delay)

    # post-enroll verification
    verify = client.get_full_contact(apollo_id)
    if verify is not None and not client.enrolled_in(verify, sequence_id):
        summary["enrollment_failed"] += 1
        db.update_contact(lead["id"], status="disqualified", apollo_contact_id=apollo_id,
                          disqualify_reason="enrollment verification failed")
        return None

    db.update_contact(lead["id"], status="synced", apollo_contact_id=apollo_id,
                      apollo_sequence_id=sequence_id, apollo_ab_variant=variant)
    db.log_outreach(lead["id"], campaign["id"], sequence_name, variant)
    db.log_api_call("apollo", "sync", 4 if existing else 3)
    return apollo_id


def main():
    ap = argparse.ArgumentParser(description="Enroll enriched leads into an Apollo sequence")
    ap.add_argument("--campaign-id", type=int, required=True)
    ap.add_argument("--db-path", default=None)
    ap.add_argument("--dry-run", action="store_true", help="Preview (default behavior)")
    ap.add_argument("--sync-live", action="store_true", help="Actually enroll (opt-in)")
    ap.add_argument("--email-account-id", default=None, help="Apollo sender inbox ID")
    ap.add_argument("--status", default="enriched", help="Source contact status to enroll")
    args = ap.parse_args()

    dry_run = not args.sync_live
    db = PipelineDB(args.db_path)
    email_acct = args.email_account_id
    if not email_acct:
        camp = db.get_campaign(campaign_id=args.campaign_id)
        email_acct = camp.get("sender_inbox_id") if camp else None

    summary = sync_campaign(db, args.campaign_id, dry_run=dry_run,
                            email_account_id=email_acct, source_status=args.status)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
