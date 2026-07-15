"""Check Gamma API event fields for match start times."""
import requests
import json

resp = requests.get(
    "https://gamma-api.polymarket.com/events",
    params={
        "tag_slug": "counter-strike-2",
        "closed": "false",
        "end_date_min": "2026-07-15T00:00:00Z",
        "limit": 5,
        "order": "volume24hr",
        "ascending": "false",
    },
    timeout=15,
)
events = resp.json()
for e in events[:3]:
    slug = e.get("slug")
    print(f"slug={slug}")
    print(f"  event startDate={e.get('startDate')}")
    print(f"  event endDate={e.get('endDate')}")
    print(f"  event eventDate={e.get('eventDate')}")
    print(f"  event startTime={e.get('startTime')}")
    print(f"  event live={e.get('live')}")
    print(f"  event ended={e.get('ended')}")
    markets = e.get("markets") or []
    if markets:
        m = markets[0]
        print(f"  market gameStartTime={m.get('gameStartTime')}")
        print(f"  market eventStartTime={m.get('eventStartTime')}")
    print()
