"""
Builds midwest_cities.json: the cities offered in the heat map's menus.

Run from the project folder:   python tools/build_city_list.py
Rule: every incorporated city (not Census-designated places) with at least
MIN_POPULATION residents, and at least the MIN_PER_STATE largest in each
state. Population is from the American Community Survey 2020-2024. Each city
is checked against Census TIGERweb, which supplies its outline for the map.
"""
import json
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import ACS_URL, TIGERWEB_URL, PLACES_LAYER, _get_census_key  # noqa: E402

MIN_POPULATION = 30_000
MIN_PER_STATE = 5
OUT = Path(__file__).resolve().parent.parent / "midwest_cities.json"

MIDWEST = {
    "17": "Illinois", "18": "Indiana", "19": "Iowa", "20": "Kansas", "26": "Michigan", "27": "Minnesota",
    "29": "Missouri", "31": "Nebraska", "38": "North Dakota", "39": "Ohio", "46": "South Dakota", "55": "Wisconsin",
}

# "Indianapolis city (balance), Indiana" -> "Indianapolis"
SUFFIX = re.compile(r"\s+(city|village|town|borough|municipality|charter township|township)(\s+\(balance\))?$")


def display_name(census_name, state_name):
    name = census_name.removesuffix(f", {state_name}")
    return SUFFIX.sub("", name).strip()


def incorporated_place_ids(state_fips):
    """GEOIDs of every incorporated place in the state that TIGERweb can outline."""
    ids, offset = set(), 0
    while True:
        r = requests.get(f"{TIGERWEB_URL}/{PLACES_LAYER}/query", params={
            "where": f"STATE='{state_fips}'", "outFields": "GEOID", "returnGeometry": "false",
            "resultOffset": offset, "resultRecordCount": 1000, "f": "json"}, timeout=60)
        r.raise_for_status()
        data = r.json()
        ids |= {f["attributes"]["GEOID"] for f in data["features"]}
        if not data.get("exceededTransferLimit"):
            return ids
        offset += len(data["features"])


cities = {}
for state_fips, state_name in MIDWEST.items():
    r = requests.get(ACS_URL, params={"get": "NAME,B01003_001E", "for": "place:*", "in": f"state:{state_fips}",
                                      "key": _get_census_key()}, timeout=60)
    r.raise_for_status()
    header, *rows = r.json()
    valid = incorporated_place_ids(state_fips)
    places = []
    for row in rows:
        rec = dict(zip(header, row))
        geoid = state_fips + rec["place"]
        if geoid not in valid:
            continue  # a Census-designated place, not a city with its own limits
        places.append({"name": display_name(rec["NAME"], state_name), "id": geoid, "population": int(rec["B01003_001E"])})
    places.sort(key=lambda p: p["population"], reverse=True)
    chosen = [p for i, p in enumerate(places) if p["population"] >= MIN_POPULATION or i < MIN_PER_STATE]
    # two places with the same everyday name in one state (rare) get their Census name back
    names = [p["name"] for p in chosen]
    for p in chosen:
        if names.count(p["name"]) > 1:
            p["name"] = p["name"] + f" ({p['id']})"
    cities[state_name] = chosen
    print(f"{state_name:13} {len(chosen):3} cities, largest: {', '.join(p['name'] for p in chosen[:4])}")

OUT.write_text(json.dumps(cities, indent=1), encoding="utf-8")
print("wrote", OUT, "-", sum(len(v) for v in cities.values()), "cities")
