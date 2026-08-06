"""
scrape_places.py â€” Google Places API (New) Nearby Search wrapper.
Used by the pipeline to discover businesses in target zip codes.

Usage:
    python scrape_places.py --campaign-id 1 --db-path leads.db

Reads target_geography and target_business_types from the campaign config.
Writes discovered businesses to PipelineContacts.
Tracks API usage in ApiUsageLog.
"""

import argparse
import json
import os
import sys
import time
import requests
from datetime import datetime, timezone

from pipeline_db import PipelineDB

API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
BASE_URL = "https://places.googleapis.com/v1/places:searchNearby"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Pro SKU free tier limit (per billing month)
NEARBY_SEARCH_MONTHLY_LIMIT = 4500  # 5000 free, 500 buffer
PLACE_DETAILS_MONTHLY_LIMIT = 4500

# Fields to request (stay in Pro SKU tier)
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.addressComponents",
    "places.nationalPhoneNumber",
    "places.websiteUri",
    "places.googleMapsUri",
    "places.types",
    "places.primaryType",
    "places.primaryTypeDisplayName",
    "places.businessStatus",
    "places.rating",
    "places.userRatingCount",
])


def geocode_zip(zip_code):
    """Convert a zip code to lat/lng centroid."""
    resp = requests.get(GEOCODE_URL, params={
        "address": zip_code,
        "key": API_KEY,
    })
    resp.raise_for_status()
    data = resp.json()
    if data["status"] == "OK" and data["results"]:
        loc = data["results"][0]["geometry"]["location"]
        return loc["lat"], loc["lng"]
    raise ValueError(f"Could not geocode zip {zip_code}: {data['status']}")


def nearby_search(lat, lng, radius_m, included_types, page_token=None):
    """Execute a Nearby Search (New) POST request."""
    body = {
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": radius_m,
            }
        },
        "maxResultCount": 20,
    }
    if included_types:
        body["includedTypes"] = included_types
    if page_token:
        body["pageToken"] = page_token

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }

    resp = requests.post(BASE_URL, json=body, headers=headers)
    resp.raise_for_status()
    return resp.json()


def extract_neighborhood(address_components):
    """Try to extract neighborhood from address components."""
    if not address_components:
        return None
    for comp in address_components:
        types = comp.get("types", [])
        if "neighborhood" in types or "sublocality" in types:
            return comp.get("longText") or comp.get("shortText")
    return None


def extract_zip(address_components):
    """Extract zip code from address components."""
    if not address_components:
        return None
    for comp in address_components:
        types = comp.get("types", [])
        if "postal_code" in types:
            return comp.get("longText") or comp.get("shortText")
    return None


def extract_city(address_components):
    if not address_components:
        return None
    for comp in address_components:
        types = comp.get("types", [])
        if "locality" in types:
            return comp.get("longText") or comp.get("shortText")
    return None


def extract_state(address_components):
    if not address_components:
        return None
    for comp in address_components:
        types = comp.get("types", [])
        if "administrative_area_level_1" in types:
            return comp.get("shortText")
    return None


def place_to_contact(place, campaign_id, scrape_id):
    """Convert a Google Places result to a PipelineContacts row dict."""
    addr_comps = place.get("addressComponents", [])
    display = place.get("displayName", {})

    return {
        "campaign_id": campaign_id,
        "google_place_id": place.get("id"),
        "business_name": display.get("text", "Unknown"),
        "primary_type": place.get("primaryType"),
        "address": place.get("formattedAddress"),
        "city": extract_city(addr_comps),
        "state": extract_state(addr_comps),
        "zip_code": extract_zip(addr_comps),
        "neighborhood": extract_neighborhood(addr_comps),
        "phone": place.get("nationalPhoneNumber"),
        "website": place.get("websiteUri"),
        "google_rating": place.get("rating"),
        "review_count": place.get("userRatingCount"),
        "status": "new",
        "source_type": "scrape",
        "source_scrape_id": scrape_id,
    }


def run_scrape(db, campaign_id):
    """Main scrape orchestration."""
    campaign = db.get_campaign(campaign_id=campaign_id)
    if not campaign:
        print(f"ERROR: Campaign {campaign_id} not found")
        return

    if campaign["status"] != "active":
        print(f"ERROR: Campaign '{campaign['name']}' is {campaign['status']}, not active")
        return

    # Parse campaign config
    zip_codes = json.loads(campaign.get("target_geography") or "[]")
    business_types = json.loads(campaign.get("target_business_types") or "[]")

    if not zip_codes:
        print("ERROR: No target_geography configured for this campaign")
        return

    # Check API budget
    used, remaining, over = db.check_api_budget("google_places_nearby", NEARBY_SEARCH_MONTHLY_LIMIT)
    if over:
        print(f"ERROR: Nearby Search API budget exhausted ({used}/{NEARBY_SEARCH_MONTHLY_LIMIT} used this month)")
        return
    print(f"API budget: {used}/{NEARBY_SEARCH_MONTHLY_LIMIT} Nearby Search calls used this month ({remaining} remaining)")

    # Start scrape record
    scrape_id = db.start_scrape(
        campaign_id=campaign_id,
        zip_codes=",".join(zip_codes),
        place_types=json.dumps(business_types) if business_types else None,
    )

    total_found = 0
    new_leads = 0
    duplicates = 0
    api_calls = 0

    # Batch types into groups of 5 (API limit per request)
    type_batches = []
    if business_types:
        for i in range(0, len(business_types), 5):
            type_batches.append(business_types[i:i+5])
    else:
        type_batches = [None]  # Search all types

    for zip_code in zip_codes:
        print(f"\n--- Scraping zip {zip_code} ---")
        try:
            lat, lng = geocode_zip(zip_code)
            api_calls += 1
            db.log_api_call("google_geocoding", "geocode", 1)
        except Exception as e:
            print(f"  WARN: Could not geocode {zip_code}: {e}")
            continue

        # Search with 5km radius (covers most zip code areas)
        radius = 5000

        for type_batch in type_batches:
            # Check budget before each call
            _, remaining, over = db.check_api_budget("google_places_nearby", NEARBY_SEARCH_MONTHLY_LIMIT)
            if over:
                print("  WARN: API budget reached mid-scrape. Stopping.")
                db.finish_scrape(scrape_id, total_found, new_leads, duplicates, api_calls, "partial")
                return

            page_token = None
            while True:
                try:
                    data = nearby_search(lat, lng, radius, type_batch, page_token)
                    api_calls += 1
                    db.log_api_call("google_places_nearby", "searchNearby", 1)
                except Exception as e:
                    print(f"  ERROR: API call failed: {e}")
                    time.sleep(2)
                    break

                places = data.get("places", [])
                if not places:
                    break

                for place in places:
                    total_found += 1
                    gid = place.get("id")

                    # Check for business status
                    bstatus = place.get("businessStatus")
                    if bstatus and bstatus != "OPERATIONAL":
                        continue

                    # Dedup check
                    existing = db.contact_exists(google_place_id=gid)
                    if existing:
                        duplicates += 1
                        continue

                    # Insert new contact
                    contact_data = place_to_contact(place, campaign_id, scrape_id)
                    try:
                        db.insert_contact(**contact_data)
                        new_leads += 1
                    except Exception as e:
                        print(f"  WARN: Insert failed for {contact_data.get('business_name')}: {e}")
                        duplicates += 1

                # Pagination
                page_token = data.get("nextPageToken")
                if not page_token:
                    break

                time.sleep(1)  # Rate limit courtesy

            types_label = ", ".join(type_batch) if type_batch else "all"
            print(f"  Types [{types_label}]: {total_found} found so far, {new_leads} new")
            time.sleep(0.5)

    # Finalize
    db.finish_scrape(scrape_id, total_found, new_leads, duplicates, api_calls)
    print(f"\n=== Scrape Complete ===")
    print(f"Campaign: {campaign['name']}")
    print(f"Zips: {', '.join(zip_codes)}")
    print(f"Total found: {total_found}")
    print(f"New leads: {new_leads}")
    print(f"Duplicates skipped: {duplicates}")
    print(f"API calls: {api_calls}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape Google Places for a campaign")
    parser.add_argument("--campaign-id", type=int, required=True)
    parser.add_argument("--db-path", type=str, default=None)
    args = parser.parse_args()

    if not API_KEY:
        print("ERROR: Set GOOGLE_PLACES_API_KEY environment variable")
        sys.exit(1)

    db = PipelineDB(args.db_path)
    run_scrape(db, args.campaign_id)

