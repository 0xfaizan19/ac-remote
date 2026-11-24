"""Set the town used for the outside temperature shown in the app.

Usage:  .venv/bin/python set_location.py "Mississauga, Ontario"
        .venv/bin/python set_location.py --data-dir data-sim "Toronto"

Looks the town up with Open-Meteo's free geocoding service (no account) and saves it in
data/config.json. The running server picks it up within a minute, with no restart needed.
Text after a comma (province, state or country) picks between towns with the same name.
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

args = sys.argv[1:]
data_dir = "data"
if args[:1] == ["--data-dir"]:
    data_dir, args = args[1], args[2:]
query = " ".join(args).strip()
if not query:
    sys.exit(__doc__)
config = Path(__file__).parent / data_dir / "config.json"

name, _, hint = (p.strip() for p in query.partition(","))
url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
    {"name": name, "count": 10, "language": "en", "format": "json"})
with urllib.request.urlopen(url, timeout=20) as r:
    results = json.load(r).get("results") or []


PROVINCES = {"qc": "quebec", "on": "ontario", "bc": "british columbia", "ab": "alberta", "mb": "manitoba",
             "sk": "saskatchewan", "ns": "nova scotia", "nb": "new brunswick", "nl": "newfoundland and labrador",
             "pe": "prince edward island", "pei": "prince edward island", "yt": "yukon",
             "nt": "northwest territories", "nu": "nunavut"}


def matches(r):
    h = PROVINCES.get(hint.lower(), hint.lower())
    fields = [str(r.get(k, "")).lower() for k in ("admin1", "admin2", "country", "country_code")]
    return any(f == h or (len(h) >= 2 and f.startswith(h)) for f in fields)


if hint:
    results = [r for r in results if matches(r)] or results
if not results:
    sys.exit(f"Couldn't find '{query}'. Try the nearest bigger town, e.g. \"Toronto, Ontario\".")
r = results[0]
place = ", ".join(x for x in (r["name"], r.get("admin1"), r.get("country")) if x)
cfg = json.loads(config.read_text()) if config.exists() else {}
cfg["location"] = {"name": place, "lat": round(r["latitude"], 2), "lon": round(r["longitude"], 2)}
tmp = config.with_suffix(".tmp")
tmp.write_text(json.dumps(cfg, indent=1))
tmp.replace(config)
print(f"Outside temperature will be for: {place} ({cfg['location']['lat']}, {cfg['location']['lon']})")
