"""
pipeline_db.py — Shared database access layer for the Conduit lead-generation pipeline.

All scripts import PipelineDB for access to the leads database (default: ./leads.db, or the
LEADGEN_DB environment variable). The schema is defined in schema.sql (apply with init_db.py).
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DEFAULT_DB = os.environ.get("LEADGEN_DB", "leads.db")


class PipelineDB:
    def __init__(self, db_path=None):
        self.db_path = db_path or os.environ.get("LEADGEN_DB", "leads.db")

    @contextmanager
    def conn(self):
        """Context manager for DB connections with WAL mode and retry."""
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    # ── Campaigns ──

    def get_campaign(self, campaign_id=None, slug=None):
        with self.conn() as c:
            if campaign_id:
                row = c.execute("SELECT * FROM Campaigns WHERE id = ?", (campaign_id,)).fetchone()
            elif slug:
                row = c.execute("SELECT * FROM Campaigns WHERE slug = ?", (slug,)).fetchone()
            else:
                return None
            return dict(row) if row else None

    def list_campaigns(self, status="active"):
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM Campaigns WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
            return [dict(r) for r in rows]

    def create_campaign(self, **kwargs):
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        with self.conn() as c:
            cur = c.execute(
                f"INSERT INTO Campaigns ({cols}) VALUES ({placeholders})", list(kwargs.values())
            )
            return cur.lastrowid

    def update_campaign(self, campaign_id, **kwargs):
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        with self.conn() as c:
            c.execute(
                f"UPDATE Campaigns SET {sets} WHERE id = ?", list(kwargs.values()) + [campaign_id]
            )

    # ── Contacts ──

    def contact_exists(self, google_place_id=None, email=None, business_name=None):
        """Return an existing contact row (dict) matched by place id, email, or name, else None."""
        with self.conn() as c:
            if google_place_id:
                row = c.execute(
                    "SELECT * FROM PipelineContacts WHERE google_place_id = ?", (google_place_id,)
                ).fetchone()
                if row:
                    return dict(row)
            if email:
                row = c.execute(
                    "SELECT * FROM PipelineContacts WHERE email = ?", (email,)
                ).fetchone()
                if row:
                    return dict(row)
            if business_name:
                row = c.execute(
                    "SELECT * FROM PipelineContacts WHERE business_name = ? COLLATE NOCASE",
                    (business_name,),
                ).fetchone()
                if row:
                    return dict(row)
            return None

    def insert_contact(self, **kwargs):
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        with self.conn() as c:
            cur = c.execute(
                f"INSERT INTO PipelineContacts ({cols}) VALUES ({placeholders})",
                list(kwargs.values()),
            )
            return cur.lastrowid

    def update_contact(self, contact_id, **kwargs):
        kwargs.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        with self.conn() as c:
            c.execute(
                f"UPDATE PipelineContacts SET {sets} WHERE id = ?",
                list(kwargs.values()) + [contact_id],
            )

    def get_contacts(self, campaign_id, status=None, limit=500):
        with self.conn() as c:
            if status:
                rows = c.execute(
                    "SELECT * FROM PipelineContacts WHERE campaign_id = ? AND status = ? LIMIT ?",
                    (campaign_id, status, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM PipelineContacts WHERE campaign_id = ? LIMIT ?",
                    (campaign_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_contact(self, contact_id):
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM PipelineContacts WHERE id = ?", (contact_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_safety_blocklist(self):
        """Contacts that must never be enrolled in outreach."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT email, phone, business_name FROM PipelineContacts "
                "WHERE status IN ('customer', 'warm_contact', 'opted_out', 'do_not_contact')"
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Scrape History ──

    def start_scrape(self, campaign_id, zip_codes, place_types=None):
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO ScrapeHistory (campaign_id, zip_codes, place_types, started_at) "
                "VALUES (?, ?, ?, ?)",
                (campaign_id, zip_codes, place_types, datetime.now(timezone.utc).isoformat()),
            )
            return cur.lastrowid

    def finish_scrape(self, scrape_id, total_results, new_leads, duplicate_count,
                      api_calls_used, status="completed"):
        with self.conn() as c:
            c.execute(
                "UPDATE ScrapeHistory SET total_results=?, new_leads=?, duplicate_count=?, "
                "api_calls_used=?, completed_at=?, status=? WHERE id=?",
                (total_results, new_leads, duplicate_count, api_calls_used,
                 datetime.now(timezone.utc).isoformat(), status, scrape_id),
            )

    # ── Outreach Log ──

    def log_outreach(self, contact_id, campaign_id, sequence_name, ab_variant=None,
                     sync_status="success"):
        with self.conn() as c:
            c.execute(
                "INSERT INTO OutreachLog (contact_id, campaign_id, apollo_sequence_name, "
                "ab_variant, status, sync_status) VALUES (?, ?, ?, ?, 'active', ?)",
                (contact_id, campaign_id, sequence_name, ab_variant, sync_status),
            )

    # ── API Usage ──

    def log_api_call(self, service, endpoint=None, calls_made=1):
        billing_month = datetime.now(timezone.utc).strftime("%Y-%m")
        with self.conn() as c:
            c.execute(
                "INSERT INTO ApiUsageLog (service, endpoint, calls_made, billing_month) "
                "VALUES (?, ?, ?, ?)",
                (service, endpoint, calls_made, billing_month),
            )

    def get_api_usage(self, service, billing_month=None):
        billing_month = billing_month or datetime.now(timezone.utc).strftime("%Y-%m")
        with self.conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(calls_made), 0) as total FROM ApiUsageLog "
                "WHERE service = ? AND billing_month = ?",
                (service, billing_month),
            ).fetchone()
            return row["total"] if row else 0

    def check_api_budget(self, service, limit):
        """Returns (used, remaining, over_budget)."""
        used = self.get_api_usage(service)
        remaining = max(0, limit - used)
        return used, remaining, used >= limit

    # ── Pipeline Runs (orchestration audit) ──

    def create_run(self, campaign_id, run_type, metadata_json=None):
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO PipelineRuns (campaign_id, run_type, status, metadata_json) "
                "VALUES (?, ?, 'running', ?)",
                (campaign_id, run_type, metadata_json),
            )
            return cur.lastrowid

    def finish_run(self, run_id, status="completed", error_message=None, metadata_json=None):
        with self.conn() as c:
            c.execute(
                "UPDATE PipelineRuns SET status=?, finished_at=?, error_message=?, "
                "metadata_json=COALESCE(?, metadata_json) WHERE id=?",
                (status, datetime.now(timezone.utc).isoformat(), error_message, metadata_json, run_id),
            )

    # ── Analytics ──

    def campaign_summary(self, campaign_id):
        with self.conn() as c:
            total = c.execute(
                "SELECT COUNT(*) as n FROM PipelineContacts WHERE campaign_id = ?", (campaign_id,)
            ).fetchone()["n"]
            by_status = c.execute(
                "SELECT status, COUNT(*) as n FROM PipelineContacts WHERE campaign_id = ? "
                "GROUP BY status", (campaign_id,)
            ).fetchall()
            return {
                "total": total,
                "by_status": {r["status"]: r["n"] for r in by_status},
            }
