import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

from pipeline_db import PipelineDB  # noqa: E402


@pytest.fixture
def db(tmp_path):
    """A fresh leads database with the canonical schema applied."""
    db_path = str(tmp_path / "test_leads.db")
    schema = open(os.path.join(ROOT, "schema.sql"), encoding="utf-8").read()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return PipelineDB(db_path)


@pytest.fixture
def campaign(db):
    """A seeded demo campaign id with an Apollo sequence configured."""
    import json
    cid = db.create_campaign(
        name="Test Campaign", slug="test", value_proposition="vp",
        target_business_types=json.dumps(["restaurant", "cafe"]),
        target_geography=json.dumps(["00001", "00002"]),
        apollo_sequence_id="seq_test_123", apollo_sequence_name="Test Sequence",
        sync_enabled=1, max_batch_size=50,
    )
    return cid
