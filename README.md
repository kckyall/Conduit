# Conduit

**A self-contained, automated go-to-market pipeline for local B2B lead generation.** Conduit discovers
businesses (Apify Google Maps), enriches their contact details from their websites, de-duplicates and
qualifies them, and enrolls the qualified leads into an **Apollo.io** email sequence — with suppression,
idempotency, dry-run, rate limiting, retries, and full audit logging.

> Built by an operator using AI-assisted development. Shared as a portfolio project.
> **No customer, lead, or recipient data is included** — the repo ships an empty schema and synthetic
> fixtures only. See [`SANITIZATION.md`](SANITIZATION.md).

---

## Pipeline

```
discover → normalize/dedupe → enrich → junk-filter → qualify (+suppression) → Apollo sequence enroll → summary
```

One command (`scripts/orchestrate.py`) runs the whole thing, tracked in `PipelineRuns`. Every stage is
optional/resumable, and **enrollment is dry-run by default**. See [`docs/architecture.md`](docs/architecture.md).

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env                 # fill in APIFY_TOKEN / APOLLO_API_KEY as needed
python init_db.py --seed             # create leads.db + a synthetic demo campaign & contacts
```

## Demo (offline, no API keys)

Runs qualification + a **dry-run** Apollo enrollment against the seeded synthetic data:

```bash
python scripts/orchestrate.py --campaign-id 1 --skip-discover --skip-enrich
```

Expected: 4 enriched contacts considered → 2 qualified (2 disqualified for junk/missing email) → 2
"would enroll", with the opted-out contact on the suppression blocklist. Nothing is sent; no API is called.

## Live usage

```bash
# Discover + enrich (needs APIFY_TOKEN), qualify, and dry-run enroll
python scripts/orchestrate.py --campaign-id 1

# Actually enroll qualified leads into the campaign's Apollo sequence (needs APOLLO_API_KEY)
python scripts/orchestrate.py --campaign-id 1 --sync-live
```

Individual stages can also be run directly: `scripts/scrape_places_apify.py`,
`scripts/scrape_websites_batch.py`, `scripts/qualify.py`, `scripts/sync_to_apollo.py`.

Live enrollment requires **all** of: `APOLLO_API_KEY` set, `Campaigns.sync_enabled = 1`, a configured
`apollo_sequence_id` (and, if set, membership in `whitelisted_sequences`), and the `--sync-live` flag.

## Data model

`schema.sql` is the single source of truth. Campaigns store targeting as JSON arrays
(`target_geography`, `target_business_types`). Contacts flow `new → enriched → qualified → synced`.
Apollo enrollment is recorded on the contact and in `OutreachLog` / `ApiUsageLog`.

## Configuration

| Variable | Purpose |
|----------|---------|
| `LEADGEN_DB` | SQLite database path (default `leads.db`) |
| `APIFY_TOKEN` | Apify token for discovery |
| `GOOGLE_PLACES_API_KEY` | legacy Google Places fallback only |
| `APOLLO_API_KEY` | Apollo API key (live enrollment) |
| `APOLLO_BASE_URL` | Apollo base URL override (optional) |
| `APOLLO_CF_SNIPPET_*` | Apollo custom-field IDs for snippet mail-merge (optional, tenant-specific) |

Secrets come only from the environment or a git-ignored `secrets/` directory — never a
repository-adjacent file.

## Tests

```bash
pip install pytest
pytest
```

All tests are **mocked** (no network, no real keys): campaign/contact model, dedupe, website-enrichment
signals, qualification + status transitions, suppression, Apollo payload shape, 429/retry, dry-run,
successful enrollment, enrollment-verification failure, idempotency, and the `sync_enabled` gate.

## Mocked vs live

Everything in this repo — the demo and the tests — runs against **synthetic data and mocked APIs**. No
production contacts, campaigns, or credentials are distributed. Live discovery/enrollment only happen
when you supply your own API keys and explicitly opt in with `--sync-live`.

## License

MIT — see [LICENSE](LICENSE).
