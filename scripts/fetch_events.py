"""Pull upcoming public events from the Colorado Democratic Party's Mobilize page
and save them to events.json for the Turf Finder map.

Uses Mobilize's public API. Public events need no login or API key.
"""
import json, time, urllib.request, datetime

ORG_ID = 53703  # mobilize.us/coloradodemocraticparty
API = f"https://api.mobilize.us/v1/organizations/{ORG_ID}/events?timeslot_start=gte_now&per_page=100"
CO_BOX = (36.9, 41.1, -109.1, -102.0)  # lat min, lat max, lng min, lng max


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "turf-finder-events/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


def main():
    now = time.time()
    events, virtual, url, pages = [], [], API, 0
    while url and pages < 50:
        data = get(url)
        pages += 1
        for e in data.get("data", []):
            slots = sorted(
                (t for t in e.get("timeslots") or [] if (t.get("end_date") or t.get("start_date") or 0) >= now),
                key=lambda t: t["start_date"],
            )
            if not slots:
                continue
            loc = e.get("location") or {}
            geo = loc.get("location") or {}
            lat, lng = geo.get("latitude"), geo.get("longitude")
            base = {
                "id": e.get("id"),
                "title": (e.get("title") or "").strip(),
                "type": e.get("event_type") or "",
                "url": e.get("browser_url") or "",
                "sponsor": (e.get("sponsor") or {}).get("name", ""),
                "venue": loc.get("venue") or "",
                "address": ", ".join(x for x in (loc.get("address_lines") or []) if x),
                "city": loc.get("locality") or "",
            }
            # one entry per upcoming date, so date filters work for recurring events
            for t in slots[:30]:
                item = dict(base, start=t["start_date"], end=t.get("end_date") or t["start_date"])
                in_co = lat is not None and lng is not None and CO_BOX[0] <= lat <= CO_BOX[1] and CO_BOX[2] <= lng <= CO_BOX[3]
                if e.get("is_virtual") or not in_co:
                    virtual.append(item)
                else:
                    events.append(dict(item, lat=round(lat, 5), lng=round(lng, 5)))
        url = data.get("next")
    out = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "org": ORG_ID,
        "events": events,
        "virtual": virtual,
    }
    with open("events.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Saved {len(events)} in-person and {len(virtual)} virtual event dates")


if __name__ == "__main__":
    main()
