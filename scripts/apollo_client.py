"""
apollo_client.py — Single, consolidated Apollo.io REST client.

Handles auth (env-based), rate limiting, retry/backoff, and the contact + sequence-enrollment
endpoints used by the pipeline. Credentials and tenant-specific IDs come only from the
environment — nothing is hardcoded, and no real values are committed.

Environment:
    APOLLO_API_KEY                 required for live calls
    APOLLO_BASE_URL                optional, defaults to https://api.apollo.io/v1
    APOLLO_CF_SNIPPET_SUBJECT      optional Apollo custom-field ID for the subject mail-merge field
    APOLLO_CF_SNIPPET_HOOK         optional Apollo custom-field ID for the hook field
    APOLLO_CF_SNIPPET_BODY         optional Apollo custom-field ID for the body field
"""

import os
import time

import requests

APOLLO_BASE = os.environ.get("APOLLO_BASE_URL", "https://api.apollo.io/v1")

# Contact stages that are safe to enroll into a cold sequence.
SAFE_STAGES = {"Cold"}


def custom_field_ids():
    """Apollo custom-field IDs for snippet mail-merge, from env. Absent => snippets skipped."""
    ids = {
        "snippet_subject": os.environ.get("APOLLO_CF_SNIPPET_SUBJECT"),
        "snippet_hook": os.environ.get("APOLLO_CF_SNIPPET_HOOK"),
        "snippet_body": os.environ.get("APOLLO_CF_SNIPPET_BODY"),
    }
    return {k: v for k, v in ids.items() if v}


class ApolloError(Exception):
    pass


class ApolloClient:
    def __init__(self, api_key=None, base=None, request_delay=0.35, max_retries=3, session=None):
        self.api_key = api_key if api_key is not None else os.environ.get("APOLLO_API_KEY", "")
        self.base = base or APOLLO_BASE
        self.request_delay = request_delay
        self.max_retries = max_retries
        self.session = session or requests
        self.custom_fields = custom_field_ids()

    @property
    def configured(self):
        return bool(self.api_key)

    def request(self, method, endpoint, payload=None):
        """Apollo API request with auth, 429/Retry-After handling, and exponential backoff."""
        url = f"{self.base}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": self.api_key,
        }
        body = payload if method != "GET" else None
        for attempt in range(self.max_retries):
            resp = self.session.request(method, url, json=body, headers=headers, timeout=30)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 60))
                time.sleep(wait)
                continue
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise
            return resp.json()
        return None

    # ── contact discovery / dup detection ──

    def search_contact(self, email=None):
        if not email:
            return None
        data = self.request("POST", "/contacts/search",
                            {"q_keywords": email, "page": 1, "per_page": 1})
        contacts = (data or {}).get("contacts", [])
        return contacts[0] if contacts else None

    def get_full_contact(self, apollo_id):
        data = self.request("GET", f"/contacts/{apollo_id}")
        return (data or {}).get("contact")

    def get_stage_map(self):
        data = self.request("GET", "/contact_stages")
        return {s["id"]: s["name"] for s in (data or {}).get("contact_stages", [])}

    # ── contact create / update ──

    def create_contact(self, lead):
        contact_name = (lead.get("contact_name") or "").strip()
        parts = contact_name.split(maxsplit=1)
        first = parts[0] if parts else (lead.get("display_contact") or "Team")
        last = parts[1] if len(parts) > 1 else ""
        payload = {
            "first_name": first,
            "last_name": last,
            "email": lead.get("email"),
            "organization_name": lead.get("display_name") or lead.get("business_name"),
            "title": lead.get("contact_title") or "Owner",
            "phone_numbers": [{"raw_number": lead["phone"]}] if lead.get("phone") else [],
            "present_raw_address": lead.get("address"),
            "city": lead.get("city"),
            "state": lead.get("state"),
            "postal_code": lead.get("zip_code"),
            "website_url": lead.get("website"),
        }
        tcf = self._typed_custom_fields(lead)
        if tcf:
            payload["typed_custom_fields"] = tcf
        data = self.request("POST", "/contacts", payload)
        return (data or {}).get("contact")

    def update_contact(self, apollo_id, lead):
        tcf = self._typed_custom_fields(lead)
        if not tcf:
            return None
        return self.request("PUT", f"/contacts/{apollo_id}",
                            {"id": apollo_id, "typed_custom_fields": tcf})

    def _typed_custom_fields(self, lead):
        out = {}
        for key, field_id in self.custom_fields.items():
            out[field_id] = lead.get(key) or ""
        return out

    # ── sequence enrollment ──

    def add_to_sequence(self, contact_id, sequence_id, email_account_id=None):
        payload = {"contact_ids": [contact_id], "emailer_campaign_id": sequence_id}
        if email_account_id:
            payload["send_email_from_email_account_id"] = email_account_id
        return self.request("POST", f"/emailer_campaigns/{sequence_id}/add_contact_ids", payload)

    def remove_from_sequence(self, contact_id, sequence_id):
        return self.request("POST", f"/emailer_campaigns/{sequence_id}/remove_or_stop_contact_ids",
                            {"contact_ids": [contact_id]})

    # ── safety helpers (pure functions on a full contact object) ──

    @staticmethod
    def active_sequences(apollo_contact):
        """Return (is_in_sequence, [sequence names/ids]) for an Apollo full-contact object."""
        if not apollo_contact:
            return False, []
        active = []
        for camp in apollo_contact.get("contact_campaign_statuses") or []:
            if (camp.get("status") or "") in ("active", "paused", "not_started"):
                active.append(camp.get("emailer_campaign_name")
                              or camp.get("emailer_campaign_id") or "unknown")
        return len(active) > 0, active

    @staticmethod
    def stage_safe(apollo_contact, stage_map):
        """Return (is_safe, stage_name)."""
        if not apollo_contact:
            return True, "New"
        stage_id = apollo_contact.get("contact_stage_id") or ""
        stage_name = stage_map.get(stage_id, "Unknown")
        return stage_name in SAFE_STAGES, stage_name

    @staticmethod
    def enrolled_in(apollo_contact, sequence_id):
        """True if the contact is currently in the given sequence (post-enroll verification)."""
        for c in (apollo_contact or {}).get("contact_campaign_statuses") or []:
            if c.get("emailer_campaign_id") == sequence_id:
                return True
        return False
