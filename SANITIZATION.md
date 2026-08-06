# Sanitization Report — Conduit

This repository is a sanitized public snapshot of an internal lead-generation pipeline. This document
records what was removed, generalized, or replaced.

## Removed (internal names / private dependencies)
- All internal agent/person names ("Patryck", "Kason", "Liza") from docstrings and comments.
- The private sibling-repository dependency: scripts previously imported `pipeline_db` from a separate
  `liza-campaign-manager` skill via `sys.path` injection. The repo now ships its own `pipeline_db.py`
  and imports it directly — it is fully self-contained.
- Brand/market leakage: hardcoded "St. Pete / St. Petersburg" name suffixes in the name-normalizer.

## Removed (private data)
- **No database, no CSV exports, no scrape/enrichment result files, and no contact records are
  included.** The repo ships only `schema.sql` (empty schema) and clearly-labeled synthetic fixtures.
- Hardcoded Windows/user paths and the private database name were replaced with `leads.db` / the
  `LEADGEN_DB` environment variable.
- Apollo tenant-specific IDs (custom-field IDs, stage IDs, default sender-inbox ID) were removed from the
  code and made environment-/campaign-configurable. No real IDs are committed.

## Credential hardening
- API tokens are read **only** from environment variables (`APIFY_TOKEN`, `APOLLO_API_KEY`,
  `GOOGLE_PLACES_API_KEY`) or an explicitly git-ignored `secrets/` directory.
- The previous repository-adjacent `scripts/apify.json` credential fallback (which risked being
  committed) was removed. `.gitignore` also blocks `.env`, `secrets/`, `apify.json`, `*.db`, `*.sqlite`,
  `*.csv`, and logs. `.env.example` contains variable names and descriptions only — never values.

## Reconciled (data model)
- The schema and every script now use one canonical model. Previously the scripts read
  `target_geography` / `target_business_types` (JSON arrays) while the shipped schema defined
  `zip_codes` / `business_types` (comma strings) — discovery would have failed on first write. The schema
  now matches what the code uses, including seven `PipelineContacts` columns that were missing
  (`years_in_business`, `website_quality`, `display_name`, `contact_name`, `contact_title`,
  `display_contact`, `source_reference`) and the full status domain.

## Consolidated / added
- Two divergent Apollo clients were consolidated into a single `apollo_client.py`.
- Added `init_db.py`, `qualify.py`, `orchestrate.py` (a self-contained single-command pipeline),
  mocked tests, synthetic fixtures, and an architecture diagram.

## What remains (and why it is safe)
- The pipeline code, the canonical schema, a Google Place-type taxonomy (`references/business_types.json`,
  a generic category list), and synthetic fixtures (`fixtures/sample_contacts.json`) whose businesses,
  emails (`*.example`), and phone numbers are fabricated.

## Verification
Full-history secret scans (gitleaks, TruffleHog) are run against the recreated repository and expected to
return zero findings. All demo/test data is synthetic; no production contacts, campaigns, or credentials
are distributed.
