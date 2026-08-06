"""Apollo integration tests — all mocked, no network, no real keys."""
import pytest
import requests

from apollo_client import ApolloClient
from sync_to_apollo import sync_campaign, safety_check, _dedupe_by_email


# ── low-level client tests via a fake HTTP session ──

class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json})
        return self.responses.pop(0)


def test_add_to_sequence_payload_shape():
    sess = FakeSession([FakeResponse(200, {"ok": True})])
    client = ApolloClient(api_key="k", session=sess, request_delay=0)
    client.add_to_sequence("contact_9", "seq_1", email_account_id="inbox_2")
    call = sess.calls[0]
    assert call["url"].endswith("/emailer_campaigns/seq_1/add_contact_ids")
    assert call["json"]["contact_ids"] == ["contact_9"]
    assert call["json"]["emailer_campaign_id"] == "seq_1"
    assert call["json"]["send_email_from_email_account_id"] == "inbox_2"


def test_request_retries_on_429(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)  # don't actually wait
    sess = FakeSession([
        FakeResponse(429, headers={"Retry-After": "0"}),
        FakeResponse(200, {"ok": True}),
    ])
    client = ApolloClient(api_key="k", session=sess, request_delay=0)
    out = client.request("GET", "/contact_stages")
    assert out == {"ok": True}
    assert len(sess.calls) == 2


def test_no_key_not_configured():
    assert ApolloClient(api_key="").configured is False
    assert ApolloClient(api_key="k").configured is True


# ── enrollment flow via a fake high-level client ──

class FakeApolloClient(ApolloClient):
    """Controllable Apollo client for exercising sync_campaign without network."""
    def __init__(self, existing=None, full_responses=None, stages=None, fail_add=False):
        super().__init__(api_key="test", request_delay=0)
        self._existing = existing
        self._full = list(full_responses or [])
        self._stages = stages or {"cold": "Cold"}
        self.enroll_calls = []
        self.fail_add = fail_add

    def get_stage_map(self):
        return self._stages

    def search_contact(self, email=None):
        return self._existing

    def get_full_contact(self, apollo_id):
        return self._full.pop(0) if self._full else None

    def create_contact(self, lead):
        return {"id": "apollo_new_1"}

    def update_contact(self, apollo_id, lead):
        return {"ok": True}

    def add_to_sequence(self, contact_id, sequence_id, email_account_id=None):
        self.enroll_calls.append((contact_id, sequence_id))
        if self.fail_add:
            raise requests.exceptions.HTTPError("boom")
        return {"ok": True}


def _add_enriched(db, campaign, **over):
    base = dict(campaign_id=campaign, business_name="Lead Co", status="enriched",
                email="lead@leadco.example", phone="000-1", website="https://x.example")
    base.update(over)
    return db.insert_contact(**base)


def test_dry_run_makes_no_api_calls(db, campaign):
    _add_enriched(db, campaign)
    client = FakeApolloClient()
    summary = sync_campaign(db, campaign, client=client, dry_run=True)
    assert summary["mode"] == "dry_run"
    assert summary["synced"] == 1
    assert client.enroll_calls == []          # nothing enrolled
    assert summary["actions"][0]["action"] == "would_enroll"


def test_successful_enrollment(db, campaign):
    lead = _add_enriched(db, campaign)
    cold = {"contact_campaign_statuses": [], "contact_stage_id": "cold"}
    enrolled = {"contact_campaign_statuses": [{"emailer_campaign_id": "seq_test_123", "status": "active"}]}
    client = FakeApolloClient(existing=None, full_responses=[cold, enrolled])
    summary = sync_campaign(db, campaign, client=client, dry_run=False, source_status="enriched")
    assert summary["synced"] == 1
    assert client.enroll_calls == [("apollo_new_1", "seq_test_123")]
    row = db.get_contact(lead)
    assert row["status"] == "synced"
    assert row["apollo_contact_id"] == "apollo_new_1"
    assert row["apollo_sequence_id"] == "seq_test_123"


def test_enrollment_verification_failure(db, campaign):
    lead = _add_enriched(db, campaign)
    cold = {"contact_campaign_statuses": [], "contact_stage_id": "cold"}
    not_enrolled = {"contact_campaign_statuses": []}   # verification: not in our sequence
    client = FakeApolloClient(existing=None, full_responses=[cold, not_enrolled])
    summary = sync_campaign(db, campaign, client=client, dry_run=False, source_status="enriched")
    assert summary["enrollment_failed"] == 1
    assert summary["synced"] == 0
    assert db.get_contact(lead)["status"] == "disqualified"


def test_already_in_active_sequence_skipped(db, campaign):
    _add_enriched(db, campaign)
    in_seq = {"contact_campaign_statuses": [{"emailer_campaign_id": "other", "status": "active"}],
              "contact_stage_id": "cold"}
    client = FakeApolloClient(existing={"id": "apollo_x"}, full_responses=[in_seq])
    summary = sync_campaign(db, campaign, client=client, dry_run=False, source_status="enriched")
    assert summary["already_in_sequence"] == 1
    assert client.enroll_calls == []


def test_add_to_sequence_failure_is_recorded(db, campaign):
    lead = _add_enriched(db, campaign)
    cold = {"contact_campaign_statuses": [], "contact_stage_id": "cold"}
    client = FakeApolloClient(existing=None, full_responses=[cold], fail_add=True)
    summary = sync_campaign(db, campaign, client=client, dry_run=False, source_status="enriched")
    assert summary["failed"] == 1
    assert summary["synced"] == 0
    assert "sync error" in (db.get_contact(lead)["disqualify_reason"] or "")


def test_idempotent_skip_already_synced(db, campaign):
    _add_enriched(db, campaign, apollo_contact_id="already_there")
    client = FakeApolloClient()
    summary = sync_campaign(db, campaign, client=client, dry_run=False, source_status="enriched")
    assert summary["skipped_already_synced"] == 1
    assert client.enroll_calls == []


def test_suppression_blocks_before_enroll(db, campaign):
    db.insert_contact(campaign_id=campaign, business_name="Blocked", email="lead@leadco.example",
                      status="opted_out")
    lead = _add_enriched(db, campaign)   # same email as the opted_out contact
    client = FakeApolloClient()
    summary = sync_campaign(db, campaign, client=client, dry_run=False, source_status="enriched")
    assert summary["blocked"] == 1
    assert client.enroll_calls == []
    assert db.get_contact(lead)["status"] == "do_not_contact"


def test_live_requires_sync_enabled(db):
    import json
    cid = db.create_campaign(name="Off", slug="off", value_proposition="v",
                             apollo_sequence_id="s1", apollo_sequence_name="S",
                             sync_enabled=0, max_batch_size=50,
                             target_geography=json.dumps(["00001"]),
                             target_business_types=json.dumps(["cafe"]))
    _add_enriched(db, cid)
    with pytest.raises(PermissionError):
        sync_campaign(db, cid, client=FakeApolloClient(), dry_run=False, source_status="enriched")


def test_batch_dedupe_by_email():
    leads = [{"email": "a@x.example"}, {"email": "A@x.example"}, {"email": None}, {"email": "b@x.example"}]
    out, dupes = _dedupe_by_email(leads)
    assert dupes == 1          # A@ duplicates a@
    assert len(out) == 3


def test_safety_check_matches():
    blocklist = [{"email": "x@y.example", "phone": "", "business_name": ""}]
    ok, reason = safety_check({"email": "x@y.example"}, blocklist)
    assert ok is False and "email" in reason
