"""
orchestrate.py — Run the full Conduit lead-gen pipeline as one command.

Stages (each optional / resumable):
    discover  -> Apify Google Maps scrape into PipelineContacts        (needs APIFY_TOKEN)
    enrich    -> website email/social waterfall on 'new' contacts       (network)
    qualify   -> deterministic fit scoring + suppression -> 'qualified'  (offline)
    sync      -> enroll 'qualified' leads into an Apollo sequence        (dry-run by default)

Every run is tracked in PipelineRuns. The whole thing is idempotent and dry-run by default;
pass --sync-live to actually enroll. For an offline demo on seeded data, skip discover + enrich.

Examples:
    python scripts/orchestrate.py --campaign-id 1                       # all stages, sync dry-run
    python scripts/orchestrate.py --campaign-id 1 --skip-discover --skip-enrich   # offline: qualify + sync
    python scripts/orchestrate.py --campaign-id 1 --sync-live           # enroll for real
"""
import argparse
import json
import os
import subprocess
import sys

from pipeline_db import PipelineDB
from qualify import qualify_campaign
from sync_to_apollo import sync_campaign

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ALL_STAGES = ["discover", "enrich", "qualify", "sync"]


def _run_script(script, args):
    """Run an in-repo stage script as a subprocess; return (ok, output)."""
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, script)] + args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def run_pipeline(db, campaign_id, stages, sync_live=False, max_places=40, db_path=None):
    campaign = db.get_campaign(campaign_id=campaign_id)
    if not campaign:
        raise ValueError(f"Campaign {campaign_id} not found")

    run_id = db.create_run(campaign_id, "pipeline",
                           metadata_json=json.dumps({"stages": stages, "sync_live": sync_live}))
    results = {}
    try:
        db_args = (["--db-path", db_path] if db_path else [])

        if "discover" in stages:
            ok, out = _run_script("scrape_places_apify.py",
                                  ["--campaign-id", str(campaign_id), "--max-places", str(max_places)] + db_args)
            results["discover"] = {"ok": ok, "output_tail": out[-500:]}

        if "enrich" in stages:
            ok, out = _run_script("scrape_websites_batch.py",
                                  ["--campaign-id", str(campaign_id), "--status", "new"] + db_args)
            results["enrich"] = {"ok": ok, "output_tail": out[-500:]}

        if "qualify" in stages:
            results["qualify"] = qualify_campaign(db, campaign_id)

        if "sync" in stages:
            email_acct = campaign.get("sender_inbox_id")
            results["sync"] = sync_campaign(
                db, campaign_id, dry_run=not sync_live,
                email_account_id=email_acct, source_status="qualified",
            )

        results["summary"] = db.campaign_summary(campaign_id)
        db.finish_run(run_id, "completed", metadata_json=json.dumps(results, default=str))
        results["run_id"] = run_id
        return results
    except Exception as e:
        db.finish_run(run_id, "failed", error_message=str(e))
        raise


def main():
    ap = argparse.ArgumentParser(description="Run the Conduit lead-gen pipeline")
    ap.add_argument("--campaign-id", type=int, required=True)
    ap.add_argument("--db-path", default=None)
    ap.add_argument("--skip-discover", action="store_true")
    ap.add_argument("--skip-enrich", action="store_true")
    ap.add_argument("--skip-qualify", action="store_true")
    ap.add_argument("--skip-sync", action="store_true")
    ap.add_argument("--sync-live", action="store_true", help="Actually enroll in Apollo (opt-in)")
    ap.add_argument("--max-places", type=int, default=40)
    a = ap.parse_args()

    stages = [s for s in ALL_STAGES if not getattr(a, f"skip_{s}")]
    db = PipelineDB(a.db_path)
    results = run_pipeline(db, a.campaign_id, stages,
                           sync_live=a.sync_live, max_places=a.max_places, db_path=a.db_path)
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
