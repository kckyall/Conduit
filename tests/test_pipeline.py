"""Pipeline tests: campaign + contact model, dedupe, qualification, suppression, status transitions."""
import json

from qualify import qualify_campaign, fit_score, is_deliverable


def test_campaign_creation_with_json_arrays(db, campaign):
    c = db.get_campaign(campaign_id=campaign)
    assert c["name"] == "Test Campaign"
    assert json.loads(c["target_geography"]) == ["00001", "00002"]
    assert json.loads(c["target_business_types"]) == ["restaurant", "cafe"]
    assert c["apollo_sequence_id"] == "seq_test_123"


def test_contact_insert_and_dedupe(db, campaign):
    cid = db.insert_contact(campaign_id=campaign, business_name="Bluebird",
                            google_place_id="p1", email="a@b.example", status="new")
    assert cid
    # dedupe by place id / email / name
    assert db.contact_exists(google_place_id="p1")["id"] == cid
    assert db.contact_exists(email="a@b.example")["id"] == cid
    assert db.contact_exists(business_name="bluebird")["id"] == cid   # case-insensitive
    assert db.contact_exists(google_place_id="nope") is None


def test_missing_columns_now_exist(db, campaign):
    # Columns the scripts write that were previously absent from schema.
    cid = db.insert_contact(
        campaign_id=campaign, business_name="X", status="enriched",
        years_in_business=12, website_quality="professional", display_name="X Co",
        contact_name="Jo Sample", contact_title="Owner", display_contact="Jo",
        source_reference="synthetic",
    )
    row = db.get_contact(cid)
    assert row["years_in_business"] == 12
    assert row["website_quality"] == "professional"
    assert row["source_reference"] == "synthetic"


def test_is_deliverable_filters_junk():
    assert is_deliverable("hello@bluebird.example")
    assert not is_deliverable("no-reply@copperline.example")
    assert not is_deliverable(None)
    assert not is_deliverable("owner@example.com")   # example.com is a placeholder domain


def test_qualify_transitions_status(db, campaign):
    good = db.insert_contact(campaign_id=campaign, business_name="Good", status="enriched",
                             email="team@good.example", website="https://good.example",
                             has_social_presence=1, google_rating=4.7, review_count=50,
                             website_quality="strong")
    junk = db.insert_contact(campaign_id=campaign, business_name="Junk", status="enriched",
                             email="no-reply@junk.example")
    noemail = db.insert_contact(campaign_id=campaign, business_name="NoEmail", status="enriched",
                                email=None)

    summary = qualify_campaign(db, campaign)
    assert summary["qualified"] == 1
    assert summary["disqualified"] == 2
    assert db.get_contact(good)["status"] == "qualified"
    assert db.get_contact(good)["fit_score"] >= 4
    assert db.get_contact(junk)["status"] == "disqualified"
    assert db.get_contact(noemail)["status"] == "disqualified"


def test_qualify_enforces_suppression(db, campaign):
    # A blocklisted (opted_out) contact, plus a fresh enriched lead with the SAME email.
    db.insert_contact(campaign_id=campaign, business_name="Blocked Co",
                      email="dupe@blocked.example", status="opted_out")
    lead = db.insert_contact(campaign_id=campaign, business_name="Fresh Co", status="enriched",
                             email="dupe@blocked.example", website="https://x.example",
                             google_rating=4.5, review_count=30)
    summary = qualify_campaign(db, campaign)
    assert summary["suppressed"] == 1
    assert db.get_contact(lead)["status"] == "do_not_contact"


def test_api_usage_budget(db):
    db.log_api_call("apify", "scrape", 5)
    used, remaining, over = db.check_api_budget("apify", 10)
    assert used == 5 and remaining == 5 and over is False
