"""
text_search_places.py — Google Places Text Search for single-company gap-fill.
Used by the pipeline when he has a business name but needs Google Place data
(address, phone, rating, place ID) that wasn't captured during bulk scrape.

Usage:
    python text_search_places.py --query "Joe's Pizza Tampa FL" [--db-path leads.db --contact-id 42]
    python text_search_places.py --query "Joe's Pizza" --near "Tampa, FL"

Without --contact-id, prints JSON result. With --contact-id, writes gap-fill
data directly to PipelineContacts.

Also callable as a module:
    from text_search_places import text_search
    result = text_search("Joe's Pizza Tampa FL")
"""

import argparse
import json
import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

from pipeline_db import PipelineDB

API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Budget: Text Search is also $32/1000 (Pro SKU), shares the same $200 free credit
TEXT_SEARCH_MONTHLY_LIMIT = 500  # conservative cap for gap-fill use

# Fields to request (Pro SKU tier — same fields as Nearby Search)
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


def text_search(query, location_bias=None):
    """
    Execute a Text Search (New) POST request.

    Args:
        query: Search string, e.g. "Joe's Pizza Tampa FL"
        location_bias: Optional dict with lat/lng/radius for bias, e.g.
                       {"lat": 27.95, "lng": -82.46, "radius": 10000}

    Returns:
        List of place dicts from the API (usually 1-5 for specific queries).
    """
    import requests

    body = {
        "textQuery": query,
        "maxResultCount": 5,
    }

    if location_bias:
        body["locationBias"] = {
            "circle": {
                "center": {
                    "latitude": location_bias["lat"],
                    "longitude": location_bias["lng"],
                },
                "radius": location_bias.get("radius", 10000),
            }
        }

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }

    resp = requests.post(TEXT_SEARCH_URL, json=body, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("places", [])


def geocode_location(location_str):
    """Convert a location string like 'Tampa, FL' to lat/lng for bias."""
    import requests

    resp = requests.get("https://maps.googleapis.com/maps/api/geocode/json", params={
        "address": location_str,
        "key": API_KEY,
    })
    resp.raise_for_status()
    data = resp.json()
    if data["status"] == "OK" and data["results"]:
        loc = data["results"][0]["geometry"]["location"]
        return {"lat": loc["lat"], "lng": loc["lng"], "radius": 10000}
    return None


def extract_component(address_components, comp_type):
    """Extract a specific address component type."""
    if not address_components:
        return None
    for comp in address_components:
        types = comp.get("types", [])
        if comp_type in types:
            return comp.get("longText") or comp.get("shortText")
    return None


def place_to_dict(place):
    """Convert a Places API result to a flat dict matching PipelineContacts columns."""
    addr_comps = place.get("addressComponents", [])
    display = place.get("displayName", {})

    return {
        "google_place_id": place.get("id"),
        "business_name": display.get("text", "Unknown"),
        "primary_type": place.get("primaryType"),
        "address": place.get("formattedAddress"),
        "city": extract_component(addr_comps, "locality"),
        "state": extract_component(addr_comps, "administrative_area_level_1"),
        "zip_code": extract_component(addr_comps, "postal_code"),
        "neighborhood": extract_component(addr_comps, "neighborhood") or extract_component(addr_comps, "sublocality"),
        "phone": place.get("nationalPhoneNumber"),
        "website": place.get("websiteUri"),
        "google_rating": place.get("rating"),
        "review_count": place.get("userRatingCount"),
        "google_maps_uri": place.get("googleMapsUri"),
        "business_status": place.get("businessStatus"),
    }


def gap_fill_contact(db, contact_id, place_data):
    """
    Update a PipelineContacts row with data from a Text Search result.
    Only fills in fields that are currently NULL/empty — never overwrites.
    """
    contact = db.get_contact(contact_id)
    if not contact:
        print(f"ERROR: Contact {contact_id} not found")
        return False

    # Fields eligible for gap-fill (only update if currently empty)
    fillable = {
        "google_place_id": place_data.get("google_place_id"),
        "primary_type": place_data.get("primary_type"),
        "address": place_data.get("address"),
        "city": place_data.get("city"),
        "state": place_data.get("state"),
        "zip_code": place_data.get("zip_code"),
        "neighborhood": place_data.get("neighborhood"),
        "phone": place_data.get("phone"),
        "website": place_data.get("website"),
        "google_rating": place_data.get("google_rating"),
        "review_count": place_data.get("review_count"),
    }

    updates = {}
    for field, value in fillable.items():
        if value is not None and not contact.get(field):
            updates[field] = value

    if updates:
        db.update_contact(contact_id, **updates)
        print(f"  Gap-filled {len(updates)} fields: {', '.join(updates.keys())}")
        return True
    else:
        print(f"  No gaps to fill for contact {contact_id}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Text Search a single business on Google Places")
    parser.add_argument("--query", required=True, help="Business name + location, e.g. 'Joe\\'s Pizza Tampa FL'")
    parser.add_argument("--near", default=None, help="Location bias, e.g. 'Tampa, FL' (optional)")
    parser.add_argument("--contact-id", type=int, default=None, help="If set, gap-fill this contact row")
    parser.add_argument("--db-path", type=str, default=None)
    parser.add_argument("--pick", type=int, default=0, help="Which result to use (0=best match, default)")
    args = parser.parse_args()

    if not API_KEY:
        print("ERROR: Set GOOGLE_PLACES_API_KEY environment variable")
        sys.exit(1)

    # Budget check
    db = PipelineDB(args.db_path)
    used, remaining, over = db.check_api_budget("google_places_text_search", TEXT_SEARCH_MONTHLY_LIMIT)
    if over:
        print(f"ERROR: Text Search API budget exhausted ({used}/{TEXT_SEARCH_MONTHLY_LIMIT} used this month)")
        sys.exit(1)
    print(f"API budget: {used}/{TEXT_SEARCH_MONTHLY_LIMIT} Text Search calls used ({remaining} remaining)")

    # Location bias
    location_bias = None
    if args.near:
        print(f"Geocoding location bias: {args.near}")
        location_bias = geocode_location(args.near)
        db.log_api_call("google_geocoding", "geocode", 1)
        if location_bias:
            print(f"  Bias: {location_bias['lat']:.4f}, {location_bias['lng']:.4f}")
        else:
            print(f"  WARN: Could not geocode '{args.near}', searching without bias")

    # Search
    print(f"Searching: \"{args.query}\"")
    try:
        places = text_search(args.query, location_bias)
        db.log_api_call("google_places_text_search", "searchText", 1)
    except Exception as e:
        print(f"ERROR: Text Search failed: {e}")
        sys.exit(1)

    if not places:
        print("No results found.")
        sys.exit(0)

    # Convert results
    results = [place_to_dict(p) for p in places]

    print(f"\nFound {len(results)} result(s):")
    for i, r in enumerate(results):
        status = r.get("business_status", "")
        marker = " [CLOSED]" if status and status != "OPERATIONAL" else ""
        print(f"  [{i}] {r['business_name']} — {r.get('address', 'no address')}{marker}")
        print(f"      Phone: {r.get('phone', '-')} | Rating: {r.get('google_rating', '-')} ({r.get('review_count', 0)} reviews)")

    # Pick best result
    pick = args.pick
    if pick >= len(results):
        print(f"WARN: --pick {pick} out of range, using 0")
        pick = 0
    chosen = results[pick]

    # Gap-fill if contact-id provided
    if args.contact_id:
        print(f"\nGap-filling contact {args.contact_id} with result [{pick}]...")
        gap_fill_contact(db, args.contact_id, chosen)
    else:
        # Just print the result as JSON
        print(json.dumps(chosen, indent=2))


if __name__ == "__main__":
    main()
