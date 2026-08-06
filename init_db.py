#!/usr/bin/env python3
"""Create the Conduit leads database from schema.sql and optionally seed synthetic demo data.

    python init_db.py                 # create empty leads.db (or $LEADGEN_DB)
    python init_db.py --seed          # also insert a synthetic demo campaign + contacts
"""
import argparse
import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))


def init(db_path, seed=False):
    schema = open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)

    if seed:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO Campaigns "
            "(name, slug, campaign_type, status, value_proposition, "
            " target_business_types, target_geography, apollo_sequence_name, "
            " sync_enabled, max_batch_size) "
            "VALUES (?, ?, 'b2b_outreach', 'active', ?, ?, ?, ?, 0, 50)",
            ("Demo Campaign", "demo",
             "We help local businesses get more customers.",
             json.dumps(["restaurant", "cafe", "bakery"]),
             json.dumps(["00001", "00002"]),
             "Demo Sequence"),
        )
        campaign_id = cur.execute("SELECT id FROM Campaigns WHERE slug='demo'").fetchone()[0]

        # Load synthetic contacts if a fixture exists.
        fixture = os.path.join(HERE, "fixtures", "sample_contacts.json")
        if os.path.exists(fixture):
            contacts = json.load(open(fixture, encoding="utf-8"))
            for ct in contacts:
                ct = dict(ct)
                ct["campaign_id"] = campaign_id
                cols = ", ".join(ct.keys())
                ph = ", ".join("?" for _ in ct)
                cur.execute(
                    f"INSERT OR IGNORE INTO PipelineContacts ({cols}) VALUES ({ph})",
                    list(ct.values()),
                )
        conn.commit()
        print(f"Seeded demo campaign (id={campaign_id}) with synthetic contacts.")

    conn.commit()
    conn.close()
    print("Initialized", db_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("LEADGEN_DB", "leads.db"))
    ap.add_argument("--seed", action="store_true")
    a = ap.parse_args()
    init(a.db, seed=a.seed)
