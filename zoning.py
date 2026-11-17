"""
Zoning check: what a site is zoned, and whether that zoning generally
allows the chosen business type.

There's no single zoning map for the Midwest - every city keeps its own,
in its own format - so this covers the cities whose zoning maps can be
looked up live:
    Indianapolis (all of Marion County) - City of Indianapolis GIS
    Carmel, IN                          - City of Carmel GIS
    Chicago                             - City of Chicago Data Portal
Everywhere else, the result says zoning isn't available yet.

The answer is based on each district's general purpose (business,
office, residential, ...), not the city's full table of permitted uses,
so it's a first screen - not a substitute for asking the planning office.
Zoning is shown next to the score; it doesn't change the Fit Score.
"""
import math
import re
from functools import lru_cache

import requests

INDY_ZONING_URL = "https://gis.indy.gov/server/rest/services/MapIndy/Zoning/MapServer/6/query"
CARMEL_ZONING_URL = "https://carmelgis.carmel.in.gov/server/rest/services/DOCS/Carmel_Zoning/MapServer/1/query"
CHICAGO_ZONING_URL = "https://data.cityofchicago.org/resource/dj47-wfun.json"

COVERED_AREAS = "Indianapolis (Marion County), Carmel and Chicago"

# Census address points sit on the street's centerline, and some zoning maps
# leave streets out. If the exact point isn't inside a district, use the
# nearest district within this distance (the lots along the street).
NEARBY_METERS = 60

# What each kind of district means for each business type:
#   "allowed"     - the district is meant for this kind of business
#   "check"       - possible, but often needs special approval or follows custom rules
#   "not_allowed" - the district generally doesn't allow it (a rezoning or variance would be needed)
ALL_TYPES = ("coffee_shop", "restaurant", "salon", "boutique", "gym", "personal_trainer", "doctor")
KIND_RULES = {
    "commercial": {t: "allowed" for t in ALL_TYPES},
    "mixed": {t: "allowed" for t in ALL_TYPES},
    "office": {**{t: "check" for t in ALL_TYPES}, "doctor": "allowed", "personal_trainer": "allowed"},
    "medical": {**{t: "check" for t in ALL_TYPES}, "doctor": "allowed"},
    "industrial": {**{t: "not_allowed" for t in ALL_TYPES}, "gym": "check", "personal_trainer": "check"},
    "planned": {t: "check" for t in ALL_TYPES},
    "special": {t: "check" for t in ALL_TYPES},
    "residential": {t: "not_allowed" for t in ALL_TYPES},
    "park": {t: "not_allowed" for t in ALL_TYPES},
}

KIND_LABELS = {
    "commercial": "business/commercial", "mixed": "mixed-use or downtown", "office": "office",
    "medical": "hospital/medical", "industrial": "industrial", "planned": "planned development",
    "special": "special-purpose", "residential": "residential", "park": "park",
}

BUSINESS_NAMES = {
    "coffee_shop": "coffee shop", "restaurant": "restaurant", "salon": "salon", "boutique": "boutique",
    "gym": "gym", "personal_trainer": "personal training studio", "doctor": "doctor's office",
}


# --- Indianapolis / Marion County ---
# (prefix, kind, description), checked in order
INDY_DISTRICTS = [
    ("CBD", "mixed", "Central Business District (downtown)"),
    ("CS", "special", "Special Commercial (custom rules for this site)"),
    ("C1", "office", "Office-Buffer Commercial"),
    ("C3", "commercial", "Neighborhood Commercial"),
    ("C4", "commercial", "Community-Regional Commercial"),
    ("C5", "commercial", "General Commercial"),
    ("C7", "commercial", "High-Intensity Commercial"),
    ("MU", "mixed", "Mixed-Use"),
    ("UQ", "mixed", "University Quarter"),
    ("HD", "medical", "Hospital District"),
    ("I", "industrial", "Industrial"),
    ("DA", "residential", "Dwelling Agriculture (rural residential)"),
    ("DP", "planned", "Planned Unit Development"),
    ("D", "residential", "Dwelling (residential)"),
    ("PK", "park", "Park"),
    ("SU", "special", "Special Use (churches, schools, public uses)"),
    ("SZ", "special", "Special Zoning District"),
    ("HP", "special", "Historic Preservation District"),
]


def _indy(lat, lon):
    attrs, nearby = _arcgis_lookup(INDY_ZONING_URL, lat, lon, "CODE1,LABEL")
    if attrs is None:
        return None
    code = str(attrs.get("CODE1") or attrs.get("LABEL") or "")
    for prefix, kind, description in INDY_DISTRICTS:
        if code.startswith(prefix):
            return {"district": _pretty_indy(code), "description": description, "kind": kind, "nearby": nearby}
    return {"district": code or "Unknown", "description": "Unrecognized district", "kind": "special", "nearby": nearby}


def _pretty_indy(code):
    """CBD1 -> CBD-1, C4 -> C-4, D5II -> D-5II"""
    match = re.match(r"^([A-Z]+)(\d.*)$", code)
    return f"{match.group(1)}-{match.group(2)}" if match else code


# --- Carmel ---
CARMEL_KINDS = {
    "B-1": "commercial", "B-2": "commercial", "B-3": "commercial", "B-5": "office", "B-6": "commercial",
    "B-7": "commercial", "B-8": "commercial", "C-1": "mixed", "C-2": "mixed", "UC": "mixed",
    "MC": "office", "I-1": "special", "M-3": "industrial", "P-1": "park", "PUD": "planned",
    "R-5": "special", "R-1": "residential", "R-2": "residential", "R-3": "residential", "R-4": "residential",
    "S-1": "residential", "S-2": "residential", "UR": "residential",
}


def _carmel(lat, lon):
    attrs, nearby = _arcgis_lookup(CARMEL_ZONING_URL, lat, lon, "ZONE_ID,ZONE_DES")
    if attrs is None:
        return None
    code = (attrs.get("ZONE_ID") or "").strip()
    description = (attrs.get("ZONE_DES") or "").strip().capitalize() or "Carmel zoning district"
    return {"district": code or "Unknown", "description": description, "kind": CARMEL_KINDS.get(code, "special"),
            "nearby": nearby}


# --- Chicago ---
# (prefix, kind, description), checked in order
CHICAGO_DISTRICTS = [
    ("DC", "mixed", "Downtown Core"),
    ("DX", "mixed", "Downtown Mixed-Use"),
    ("DS", "commercial", "Downtown Service"),
    ("DR", "special", "Downtown Residential (limited business)"),
    ("B", "commercial", "Business (neighborhood shopping)"),
    ("C", "commercial", "Commercial"),
    ("PMD", "industrial", "Planned Manufacturing District"),
    ("PD", "planned", "Planned Development (custom rules for this site)"),
    ("M", "industrial", "Manufacturing"),
    ("POS", "park", "Parks and Open Space"),
    ("RS", "residential", "Residential Single-Unit"),
    ("RT", "residential", "Residential Two-Flat and Townhouse"),
    ("RM", "residential", "Residential Multi-Unit"),
    ("T", "special", "Transportation"),
]


def _chicago(lat, lon):
    response = requests.get(CHICAGO_ZONING_URL, timeout=15, params={
        "$where": f"intersects(the_geom, 'POINT ({lon} {lat})')", "$select": "zone_class", "$limit": 1,
    })
    response.raise_for_status()
    rows = response.json()
    nearby = False
    if not rows:
        response = requests.get(CHICAGO_ZONING_URL, timeout=15, params={
            "$where": f"within_circle(the_geom, {lat}, {lon}, {NEARBY_METERS})", "$select": "zone_class, the_geom", "$limit": 20,
        })
        response.raise_for_status()
        rows = response.json()
        if not rows:
            return None
        rows = [min(rows, key=lambda row: min(
            (_distance_to_rings(lat, lon, polygon) for polygon in (row.get("the_geom") or {}).get("coordinates", [])),
            default=float("inf")))]
        nearby = True
    code = rows[0].get("zone_class") or ""
    for prefix, kind, description in CHICAGO_DISTRICTS:
        if code.startswith(prefix):
            return {"district": code, "description": description, "kind": kind, "nearby": nearby}
    return {"district": code or "Unknown", "description": "Unrecognized district", "kind": "special", "nearby": nearby}


def _arcgis_point(lat, lon, fields):
    return {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outFields": fields, "returnGeometry": "false", "f": "json"}


def _arcgis_lookup(url, lat, lon, fields):
    """
    The attributes of the district containing the point, or else of the
    nearest district within NEARBY_METERS. Returns (attributes, nearby), or
    (None, False) if nothing is close.
    """
    response = requests.get(url, timeout=15, params=_arcgis_point(lat, lon, fields))
    response.raise_for_status()
    features = response.json().get("features", [])
    if features:
        return features[0]["attributes"], False
    response = requests.get(url, timeout=15, params={
        **_arcgis_point(lat, lon, fields), "distance": NEARBY_METERS, "units": "esriSRUnit_Meter",
        "returnGeometry": "true", "outSR": 4326,
    })
    response.raise_for_status()
    features = response.json().get("features", [])
    if not features:
        return None, False
    nearest = min(features, key=lambda f: _distance_to_rings(lat, lon, f.get("geometry", {}).get("rings", [])))
    return nearest["attributes"], True


def _distance_to_rings(lat, lon, rings):
    """Approximate meters from a point to the nearest edge of a polygon's rings."""
    scale_x = 111_320 * math.cos(math.radians(lat))
    best = float("inf")
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            ax, ay = (x1 - lon) * scale_x, (y1 - lat) * 111_320
            bx, by = (x2 - lon) * scale_x, (y2 - lat) * 111_320
            dx, dy = bx - ax, by - ay
            t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
            best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


# Which lookup covers a location: (state FIPS, county FIPS) -> (city, source, lookup)
SOURCES = {
    ("18", "097"): ("Indianapolis", "City of Indianapolis GIS (MapIndy zoning)", _indy),
    ("18", "057"): ("Carmel", "City of Carmel GIS", _carmel),        # Hamilton County: only Carmel is covered
    ("17", "031"): ("Chicago", "City of Chicago Data Portal (Zoning Districts)", _chicago),  # Cook County: only Chicago
}


@lru_cache(maxsize=512)
def _lookup(state, county, lat, lon):
    city, source, lookup = SOURCES[(state, county)]
    return city, source, lookup(lat, lon)


def get_zoning(location, business_type):
    """
    The zoning check for one site. Always returns a dict; "available" is
    False (with a "reason") outside the covered cities or if the city's
    map service doesn't answer.
    """
    key = (location["state"], location["county"])
    if key not in SOURCES:
        return {"available": False, "reason": f"Zoning isn't available for this area yet. SiteScout checks zoning in {COVERED_AREAS}."}
    try:
        city, source, found = _lookup(*key, round(location["lat"], 6), round(location["lon"], 6))
    except (requests.RequestException, ValueError, KeyError):
        return {"available": False, "reason": "The city's zoning map didn't respond. Try again in a minute."}
    if found is None:
        return {"available": False, "reason": f"This spot isn't on a covered zoning map. SiteScout checks zoning in {COVERED_AREAS}."}

    status = KIND_RULES[found["kind"]][business_type]
    business = BUSINESS_NAMES[business_type]
    kind_label = KIND_LABELS[found["kind"]]
    district = found["district"]
    if status == "allowed":
        explanation = f"{district} is a {kind_label} district, which is generally meant for businesses like a {business}."
    elif status == "check":
        explanation = (f"{district} is a {kind_label} district. A {business} may be allowed only with special approval "
                       f"or under rules written for this site, so check with the {city} planning office.")
    else:
        explanation = (f"{district} is a {kind_label} district, which generally doesn't allow a {business}. "
                       f"Opening here would likely need a rezoning or variance.")
    return {
        "available": True,
        "city": city,
        "district": district,
        "description": found["description"],
        "kind": found["kind"],
        "status": status,
        "explanation": explanation,
        "source": source,
        "nearest_lot": found.get("nearby", False),
        "disclaimer": ("The address point sits on the street, so this is the zoning of the nearest lot. "
                       if found.get("nearby") else "")
                      + "Based on the district's general purpose, not the city's full list of permitted uses. "
                        "Confirm with the planning office before signing a lease.",
    }
