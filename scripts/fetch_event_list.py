"""Build the Mobilize event list for the Colorado field program.

Pulls every upcoming event from the Colorado Democratic Party's Mobilize feed
and writes ONE ROW PER SHIFT (timeslot), tagged with the field team whose turf
it falls in. Runs hourly from .github/workflows/update-events.yml.

Outputs (repo root):
  event-list.json   the feed the event-list.html page reads
  event-list.csv    the same rows for spreadsheets

Inputs:
  data.json                    turf + house district boundaries (same file the map uses)
  scripts/co_zip_centroids.json  ZIP -> lat/lng, for events whose address is private
  event-edits.json             saved from the page: team / contact / co-host / campaign
                               changes, the dropdown option lists, and events added by
                               hand (e.g. private events). This script never rewrites it.

Optional: set a MOBILIZE_API_KEY secret to also get event contact info (the
event owner) and private-visibility events. Mobilize hides both from the
public feed.

Test without the network:  python scripts/fetch_event_list.py --from-file sample.json
"""
import csv, datetime, json, os, re, sys, time, urllib.request
from zoneinfo import ZoneInfo

ORG_ID = 53703  # mobilize.us/coloradodemocraticparty
ORG_NAME = "Colorado Democratic Party"
API = f"https://api.mobilize.us/v1/organizations/{ORG_ID}/events?timeslot_start=gte_now&per_page=100"
API_KEY = os.environ.get("MOBILIZE_API_KEY", "").strip()
CO_BOX = (36.9, 41.1, -109.1, -102.0)  # lat min, lat max, lng min, lng max

# ---------------------------------------------------------------- teams
# code, organizer, and how events land there automatically:
#   turf: name of a turf shape in data.json (the shaded areas on the map)
#   hds:  state house districts, for teams the map has no shape for
# Teams with neither are only ever picked by hand in the dropdown.
TEAMS = [
    {"code": "1A", "region": "1", "organizer": "Emile Smith", "turf": "1A"},
    {"code": "1B", "region": "1", "organizer": "Faraj Atwi", "turf": "1B"},
    {"code": "1C", "region": "1", "organizer": "Danice Crawford", "turf": "1C"},
    {"code": "2A", "region": "2", "organizer": "Stephanie Bowman", "hds": [14, 15]},
    {"code": "2B", "region": "2", "organizer": "John Jerrald", "hds": [16, 17, 18]},
    {"code": "2C", "region": "2", "organizer": "Yvonne Weissbarth", "turf": "2C"},
    {"code": "3A", "region": "3", "organizer": "Keith Pierce", "turf": "3A"},
    {"code": "3B", "region": "3", "organizer": "Garrett Kelley", "turf": "3B"},
    {"code": "4A", "region": "4", "organizer": "Grayson Kern"},
    {"code": "4B", "region": "4", "organizer": "Sam Heller"},
    {"code": "4C", "region": "4", "organizer": "Mario Quinones Rabelo"},
]
# Everything outside the turf above (CD-8, the rest of the state, virtual
# events) lands here until someone picks 4A, 4B or 4C in the dropdown.
UNASSIGNED = {"code": "4", "region": "4", "organizer": "", "name": "Region 4 / Distributed, not yet split"}
# Map turf names that are not one of the teams above, for the "why" note.
OTHER_TURF = {"4A-W": "CD-8 Weld", "4A-A": "CD-8 Adams", "4A-L": "CD-8 Larimer", "Dist": "Distributed turf"}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- geometry
def load_shapes(path):
    """Decode the map's TopoJSON into plain polygons: {layer: {id: [polygon, ...]}}."""
    with open(path) as f:
        data = json.load(f)
    topo = data["topo"]
    (sx, sy), (tx, ty) = topo["transform"]["scale"], topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        arcs.append(pts)

    def ring(idxs):
        out = []
        for i in idxs:
            pts = arcs[i] if i >= 0 else arcs[~i][::-1]
            out.extend(pts if not out else pts[1:])
        return out

    def polys(g):
        groups = [g["arcs"]] if g["type"] == "Polygon" else g["arcs"]
        return [[ring(r) for r in p] for p in groups]

    def layer(name, key):
        out = {}
        for g in topo["objects"][name]["geometries"]:
            ps = polys(g)
            xs = [x for p in ps for x, _ in p[0]]
            ys = [y for p in ps for _, y in p[0]]
            out[str(g["properties"][key])] = {"polys": ps, "bbox": (min(xs), min(ys), max(xs), max(ys))}
        return out

    return {
        "turf": layer("L_turf", "team"),
        "hd": layer("L_hd", "id"),
        "places": {k.lower(): v for k, v in data.get("lab", {}).get("place", {}).items()},
        "hd_turf": data.get("hdGeo", {}),
    }


def in_ring(x, y, r):
    c = False
    j = len(r) - 1
    for i in range(len(r)):
        xi, yi = r[i]
        xj, yj = r[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            c = not c
        j = i
    return c


def locate(layer, lat, lng):
    """Ids of every shape in the layer that contains the point."""
    hits = []
    for key, s in layer.items():
        b = s["bbox"]
        if lng < b[0] or lng > b[2] or lat < b[1] or lat > b[3]:
            continue
        for p in s["polys"]:
            if in_ring(lng, lat, p[0]) and not any(in_ring(lng, lat, h) for h in p[1:]):
                hits.append(key)
                break
    return hits


# ---------------------------------------------------------------- team assignment
TURF_TEAM = {t["turf"]: t["code"] for t in TEAMS if t.get("turf")}
HD_TEAM = {int(h): t["code"] for t in TEAMS for h in t.get("hds", [])}


def team_at(shapes, lat, lng):
    """(team code, house district, turf note) for a point."""
    turfs = locate(shapes["turf"], lat, lng)
    hds = locate(shapes["hd"], lat, lng)
    hd = int(hds[0]) if hds else None
    for t in turfs:
        if t in TURF_TEAM:
            return TURF_TEAM[t], hd, ""
    if hd in HD_TEAM:
        return HD_TEAM[hd], hd, ""
    for t in turfs:
        if t in OTHER_TURF:
            return UNASSIGNED["code"], hd, OTHER_TURF[t]
    return UNASSIGNED["code"], hd, "outside mapped turf"


def team_for_hd(shapes, hd):
    """Team that holds most of a house district, or None."""
    if hd in HD_TEAM:
        return HD_TEAM[hd]
    share = shapes["hd_turf"].get(str(hd)) or {}
    if not share:
        return None
    top = max(share, key=share.get)
    return TURF_TEAM.get(top)


HD_IN_TITLE = re.compile(r"\bHD\s*[-#]?\s*(\d{1,2})\b|\bHouse District\s*(\d{1,2})\b", re.I)


def assign(shapes, zips, e):
    """Pick a team for one Mobilize event. Returns (code, why, lat, lng, hd)."""
    loc = e.get("location") or {}
    geo = loc.get("location") or {}
    lat, lng = geo.get("latitude"), geo.get("longitude")
    virtual = bool(e.get("is_virtual"))
    in_co = lambda a, b: a is not None and b is not None and CO_BOX[0] <= a <= CO_BOX[1] and CO_BOX[2] <= b <= CO_BOX[3]

    if not virtual:
        if in_co(lat, lng):
            code, hd, note = team_at(shapes, lat, lng)
            why = "Event address" + (f" (HD {hd})" if hd else "") + (f", {note}" if note else "")
            return code, why, round(lat, 5), round(lng, 5), hd
        z = (loc.get("postal_code") or "").strip()[:5]
        if z in zips:
            code, hd, note = team_at(shapes, *zips[z])
            why = f"Approximate, from ZIP {z}" + (f" (HD {hd})" if hd else "") + (f", {note}" if note else "")
            return code, why, None, None, hd

    # No exact address or ZIP: a house district in the title is the next best
    # hint for whose event it is (this is also how virtual events get a team).
    title = e.get("title") or ""
    nums = [int(a or b) for a, b in HD_IN_TITLE.findall(title)]
    for hd in nums:
        code = team_for_hd(shapes, hd)
        if code:
            label = ", ".join(f"HD {n}" for n in nums)
            return code, f"Title mentions {label}", None, None, hd

    city = (loc.get("locality") or "").strip()
    if not virtual and city.lower() in shapes["places"]:
        code, hd, note = team_at(shapes, *shapes["places"][city.lower()])
        return code, f"Approximate, from city {city}" + (f", {note}" if note else ""), None, None, hd
    return UNASSIGNED["code"], ("Virtual event" if virtual else "No address on Mobilize"), None, None, None


# ---------------------------------------------------------------- Mobilize
def get(url):
    headers = {"User-Agent": "turf-finder-event-list/1.0"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


def fetch_all():
    out, url, pages = [], API, 0
    while url and pages < 50:
        data = get(url)
        pages += 1
        out.extend(data.get("data") or [])
        url = data.get("next")
    return out


# ---------------------------------------------------------------- rows
def pretty(s):
    return (s or "").replace("_", " ").strip().capitalize()


def clock(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def build_rows(events, shapes, zips, now):
    rows = []
    for e in events:
        slots = [t for t in e.get("timeslots") or [] if (t.get("end_date") or t.get("start_date") or 0) >= now]
        if not slots:
            continue
        loc = e.get("location") or {}
        private_addr = (e.get("address_visibility") or "").upper() == "PRIVATE"
        # Mobilize fills venue/address with a "this address is private" sentence; drop it.
        hidden = lambda s: "address is private" in (s or "").lower()
        venue = "" if hidden(loc.get("venue")) else (loc.get("venue") or "").strip()
        address = ", ".join(x.strip() for x in (loc.get("address_lines") or []) if x and x.strip() and not hidden(x))
        city, state, zipc = (loc.get("locality") or "").strip(), (loc.get("region") or "").strip(), (loc.get("postal_code") or "").strip()
        virtual = bool(e.get("is_virtual"))
        code, why, lat, lng, hd = assign(shapes, zips, e)
        sponsor = e.get("sponsor") or {}
        contact = e.get("contact") or {}
        tz_name = e.get("timezone") or "America/Denver"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("America/Denver")
        if virtual:
            place = "Virtual"
        else:
            parts = [venue, address, ", ".join(x for x in (city, state) if x) + (f" {zipc}" if zipc else "")]
            place = " · ".join(p for p in parts if p.strip()) or "No address listed"
            if private_addr:
                place += " (address private until signup)"
        base = {
            "event_id": e.get("id"),
            "title": (e.get("title") or "").strip(),
            "activity": pretty(e.get("event_type")),
            "format": "Virtual" if virtual else "In person",
            "visibility": pretty(e.get("visibility")) or "Public",
            "address_visibility": pretty(e.get("address_visibility")),
            "tags": [t.get("name", "").strip() for t in e.get("tags") or [] if t.get("name")],
            "location": place,
            "venue": venue, "address": address, "city": city, "state": state, "zip": zipc,
            "lat": lat, "lng": lng, "hd": hd,
            "virtual_url": e.get("virtual_action_url") or "",
            "host_org": (sponsor.get("name") or "").strip(),
            # The event owner as Mobilize reports it (needs an API key). The page's
            # contact dropdown starts here; contact_* below hold the final answer.
            "api_contact": {
                "name": (contact.get("name") or "").strip(),
                "email": (contact.get("email_address") or "").strip(),
                "phone": (contact.get("phone_number") or "").strip(),
            },
            "volunteer_hosted": bool(e.get("created_by_volunteer_host")),
            # Mobilize's API has no co-host field. When another organization owns
            # the event and it is only shared onto our feed, list that organization.
            "auto_cohosts": "" if sponsor.get("id") == ORG_ID or not sponsor.get("name") else sponsor["name"].strip(),
            "url": e.get("browser_url") or "",
            "auto_team": code,
            "auto_why": why,
            "source": "Mobilize",
        }
        for t in sorted(slots, key=lambda t: t["start_date"]):
            start = datetime.datetime.fromtimestamp(t["start_date"], tz)
            end = datetime.datetime.fromtimestamp(t.get("end_date") or t["start_date"], tz)
            rows.append(dict(
                base,
                key=f"t{t['id']}",
                timeslot_id=t["id"],
                start=t["start_date"],
                end=t.get("end_date") or t["start_date"],
                date=start.strftime("%Y-%m-%d"),
                day=start.strftime("%a"),
                time=f"{clock(start)} – {clock(end)}",
                all_day=(end - start).total_seconds() >= 23 * 3600,
                full=bool(t.get("is_full")),
            ))
    return rows


def build_manual_rows(custom_events, now):
    """Rows for events added by hand on the page (anything not on the Mobilize feed)."""
    valid = {t["code"] for t in TEAMS} | {UNASSIGNED["code"]}
    tz = ZoneInfo("America/Denver")
    rows = []
    for eid, ev in (custom_events or {}).items():
        if not isinstance(ev, dict):
            continue
        s = lambda k: str(ev.get(k) or "").strip()
        virtual = s("format") == "Virtual"
        city, zipc = s("city"), s("zip")
        if virtual:
            place = "Virtual"
        else:
            parts = [s("venue"), s("address"), (f"{city}, CO" if city else "") + (f" {zipc}" if zipc else "")]
            place = " · ".join(p for p in parts if p.strip()) or "No address listed"
        team = s("team") if s("team") in valid else UNASSIGNED["code"]
        base = {
            "event_id": eid, "title": s("title") or "Untitled event", "activity": s("activity"),
            "format": "Virtual" if virtual else "In person", "visibility": s("visibility") or "Private",
            "address_visibility": "", "tags": [t for t in ev.get("tags") or [] if isinstance(t, str) and t.strip()],
            "location": place, "venue": s("venue"), "address": s("address"), "city": city, "state": "" if virtual else "CO",
            "zip": zipc, "lat": None, "lng": None, "hd": None, "virtual_url": s("virtual_url"),
            "host_org": s("host_org"), "api_contact": {"name": "", "email": "", "phone": ""},
            "volunteer_hosted": False, "auto_cohosts": s("cohosts"), "url": s("url"),
            "auto_team": team, "auto_why": "Added by hand", "source": "Added by hand",
            "auto_contact": s("contact"),
            "auto_campaigns": [c for c in ev.get("campaigns") or [] if isinstance(c, str)],
        }
        for sh in ev.get("shifts") or []:
            try:
                start = datetime.datetime.strptime(f"{sh['date']} {sh['start']}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                end = datetime.datetime.strptime(f"{sh['date']} {sh['end']}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            except Exception:
                print(f"WARNING: skipping a shift of hand-added event {eid} with a bad date or time: {sh}")
                continue
            if end.timestamp() < now:
                continue
            rows.append(dict(
                base, key=f"t{sh.get('id')}", timeslot_id=sh.get("id"),
                start=int(start.timestamp()), end=int(end.timestamp()),
                date=start.strftime("%Y-%m-%d"), day=start.strftime("%a"),
                time=f"{clock(start)} – {clock(end)}", all_day=False, full=False,
            ))
    return rows


def apply_edits(rows, edits, contact_options):
    """Layer saved edits on top: a shift's own edit beats one made for the whole event."""
    valid = {t["code"] for t in TEAMS} | {UNASSIGNED["code"]}
    people = {c.get("name"): c for c in contact_options or [] if isinstance(c, dict) and c.get("name")}
    for r in rows:
        merged = dict(edits.get(f"e{r['event_id']}") or {})
        merged.update(edits.get(r["key"]) or {})
        team = merged.get("team")
        r["team"] = team if team in valid else r["auto_team"]
        r["team_edited"] = r["team"] != r["auto_team"]
        camps = merged["campaigns"] if isinstance(merged.get("campaigns"), list) else r.get("auto_campaigns") or []
        r["campaigns"] = [c for c in camps if isinstance(c, str)]
        r["cohosts"] = merged["cohosts"] if isinstance(merged.get("cohosts"), str) else r["auto_cohosts"]
        api = r["api_contact"]
        r.setdefault("auto_contact", api["name"])
        name = merged["contact"] if isinstance(merged.get("contact"), str) else r["auto_contact"]
        if name in people:
            p = people[name]
            email, phone = str(p.get("email") or ""), str(p.get("phone") or "")
        elif name and name == api["name"]:
            email, phone = api["email"], api["phone"]
        else:
            email = phone = ""
        r["contact_name"], r["contact_email"], r["contact_phone"] = name, email, phone
    return rows


CSV_COLS = [
    ("Team", lambda r, T: r["team"]),
    ("Organizer", lambda r, T: T.get(r["team"], {}).get("organizer", "")),
    ("Date", lambda r, T: r["date"]),
    ("Day", lambda r, T: r["day"]),
    ("Time", lambda r, T: r["time"]),
    ("Event name", lambda r, T: r["title"]),
    ("Activity", lambda r, T: r["activity"]),
    ("Event type", lambda r, T: r["format"]),
    ("Visibility", lambda r, T: r["visibility"]),
    ("Tags", lambda r, T: "; ".join(r["tags"])),
    ("Location", lambda r, T: r["location"]),
    ("City", lambda r, T: r["city"]),
    ("ZIP", lambda r, T: r["zip"]),
    ("Virtual link", lambda r, T: r["virtual_url"]),
    ("Host organization", lambda r, T: r["host_org"]),
    ("Contact name", lambda r, T: r["contact_name"]),
    ("Contact email", lambda r, T: r["contact_email"]),
    ("Contact phone", lambda r, T: r["contact_phone"]),
    ("Co-hosts", lambda r, T: r["cohosts"]),
    ("Collaborating campaigns", lambda r, T: "; ".join(r["campaigns"])),
    ("Shift full", lambda r, T: "Yes" if r["full"] else ""),
    ("Team set by", lambda r, T: "Changed by hand" if r["team_edited"] else r["auto_why"]),
    ("Source", lambda r, T: r["source"]),
    ("Link", lambda r, T: r["url"]),
    ("Event ID", lambda r, T: r["event_id"]),
    ("Shift ID", lambda r, T: r["timeslot_id"]),
]


def main():
    src = None
    if "--from-file" in sys.argv:
        src = sys.argv[sys.argv.index("--from-file") + 1]
    shapes = load_shapes(os.path.join(ROOT, "data.json"))
    with open(os.path.join(ROOT, "scripts", "co_zip_centroids.json")) as f:
        zips = json.load(f)
    edits_path = os.path.join(ROOT, "event-edits.json")
    saved = {}
    if os.path.exists(edits_path):
        try:
            with open(edits_path) as f:
                saved = json.load(f)
        except Exception as err:  # a bad edits file must not stop the hourly refresh
            print(f"WARNING: could not read event-edits.json ({err}); continuing without edits")

    if src:
        with open(src) as f:
            raw = json.load(f)
        events = raw.get("data", raw) if isinstance(raw, dict) else raw
    else:
        events = fetch_all()

    now = time.time()
    rows = build_rows(events, shapes, zips, now) + build_manual_rows(saved.get("custom_events"), now)
    rows.sort(key=lambda r: (r["start"], r["title"], str(r["timeslot_id"])))
    rows = apply_edits(rows, saved.get("edits") or {}, saved.get("contact_options"))
    teams = TEAMS + [UNASSIGNED]
    out = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "org": ORG_ID,
        "org_name": ORG_NAME,
        "has_contacts": bool(API_KEY),
        "teams": teams,
        "events": len({r["event_id"] for r in rows}),
        "rows": rows,
    }
    with open(os.path.join(ROOT, "event-list.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
    by_code = {t["code"]: t for t in teams}
    with open(os.path.join(ROOT, "event-list.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([c for c, _ in CSV_COLS])
        for r in rows:
            w.writerow([fn(r, by_code) for _, fn in CSV_COLS])
    counts = {}
    for r in rows:
        counts[r["team"]] = counts.get(r["team"], 0) + 1
    print(f"Saved {len(rows)} shifts from {out['events']} events" + ("" if API_KEY else " (no API key: contact info and private events not included)"))
    print("By team: " + ", ".join(f"{k}={counts[k]}" for k in sorted(counts)))


if __name__ == "__main__":
    main()
