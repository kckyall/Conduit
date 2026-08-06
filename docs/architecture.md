# Conduit — Architecture

Conduit is a self-contained, SQLite-backed pipeline that discovers local businesses, enriches their
contact details, qualifies them, and enrolls the qualified ones into an Apollo.io email sequence — with
suppression, idempotency, dry-run, and full audit logging throughout.

```mermaid
flowchart TD
    subgraph ORCH["orchestrate.py (one command, tracked in PipelineRuns)"]
      direction TB
      A["discover<br/>scrape_places_apify.py"] --> B["normalize + dedupe<br/>contact_exists()"]
      B --> C["enrich<br/>scrape_websites_batch.py"]
      C --> D["junk-email filter"]
      D --> E["qualify.py<br/>fit score + suppression → 'qualified'"]
      E --> F["sync_to_apollo.py<br/>enroll into Apollo sequence"]
    end

    A -->|Apify Google Maps + Contact Details| APIFY[(Apify)]
    C -->|website scrape| WEB[(business sites)]
    F --> APOLLO[(Apollo.io API)]

    subgraph DB["SQLite (schema.sql)"]
      CAMP["Campaigns<br/>target_geography / target_business_types (JSON)<br/>apollo_sequence_id, sync_enabled, whitelist"]
      CT["PipelineContacts<br/>status: new→enriched→qualified→synced<br/>apollo_contact_id / sequence_id / ab_variant"]
      OL["OutreachLog / ApiUsageLog / ScrapeHistory / PipelineRuns"]
    end
    B --> CT
    E --> CT
    F --> OL

    subgraph SAFETY["Enrollment safety (sync_to_apollo)"]
      S1["1. suppression / do-not-contact blocklist"]
      S2["2. idempotency: skip if apollo_contact_id set"]
      S3["3. in-batch email de-dup"]
      S4["4. remote: skip if in an active sequence; Cold-stage only"]
      S5["5. post-enroll verification (retry on silent fail)"]
    end
    F -.enforces.-> SAFETY
```

## Data model (canonical)
One schema, `schema.sql`, is the single source of truth. Geography and business-type targeting are JSON
arrays on the campaign (`target_geography`, `target_business_types`). Contact status flows
`new → enriched → qualified → synced`, with `disqualified` / `do_not_contact` / `opted_out` terminal states.

## Apollo integration (`apollo_client.py` + `sync_to_apollo.py`)
- Auth from `APOLLO_API_KEY`; base URL and custom-field IDs from the environment (no tenant IDs hardcoded).
- Sequence chosen **by ID** from `Campaigns.apollo_sequence_id`, gated by `sync_enabled` and an optional
  `whitelisted_sequences` allowlist.
- Enrollment via `POST /emailer_campaigns/{id}/add_contact_ids`, with 429/`Retry-After` handling,
  exponential backoff, per-request spacing, and a `max_batch_size` cap.
- **Dry-run by default** — live enrollment requires `--sync-live`.

## Mocked vs live
The test suite and the seeded demo run entirely offline (mocked Apollo, synthetic fixtures). Live
discovery needs `APIFY_TOKEN`; live enrollment needs `APOLLO_API_KEY` **and** `--sync-live`.
