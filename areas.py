"""
Scores every neighborhood (census tract) in a city for the map's heat map.

Pulling a city's data is the slow part (up to a minute the first time), so
it's cached per city. After that, switching business types only re-runs
the math, which takes a moment.
"""
import math
from functools import lru_cache

from pipeline import (CITY_BY_ID, get_city_areas, get_all_tract_demographics, get_state_demographics, get_named_neighborhoods,
                      get_zip_business_counts, get_roads_in_area, point_in_rings)
from scoring import calculate_area_scores, AREA_NAICS, AREA_TRAFFIC_RADIUS

TOP_SUGGESTIONS = 5

# Tracts with almost nobody living in them (parks, airports, rail yards)
# get misleading demographics, so they're shown but never suggested.
MIN_POPULATION_TO_SUGGEST = 300

# Suggested neighborhoods' centers must be at least this far apart
MIN_SUGGESTION_SPACING = 1600  # meters, about a mile

# Labels for neighborhoods. A tract takes the name of the nearest named
# neighborhood (OpenStreetMap) within this distance of its center...
NAME_EXACT_DISTANCE = 1200   # meters -> "Broad Ripple"
NAME_NEAR_DISTANCE = 3000    # meters -> "Near Broad Ripple"
# ...otherwise it's labeled by its busiest road ("Near Keystone Ave").

ROAD_WORDS = {"AV": "Ave", "AVE": "Ave", "ST": "St", "RD": "Rd", "BLVD": "Blvd", "DR": "Dr", "PKWY": "Pkwy",
              "HWY": "Hwy", "LN": "Ln", "CT": "Ct", "PL": "Pl", "CIR": "Cir", "PIKE": "Pike", "WAY": "Way"}
KEEP_UPPER = {"US", "SR", "N", "S", "E", "W", "NE", "NW", "SE", "SW"}


def _road_label(road):
    """'KEYSTONE AV' -> 'Keystone Ave', '38TH ST' -> '38th St', 'US 31' stays 'US 31'."""
    words = []
    for w in road.split():
        if w in ROAD_WORDS:
            words.append(ROAD_WORDS[w])
        elif w in KEEP_UPPER or not w[0].isalpha():
            words.append(w if w in KEEP_UPPER else w.lower())
        else:
            words.append(w[0] + w[1:].lower())
    return " ".join(words)


def _area_label(lat, lon, named, roads, tract_name):
    """The everyday name for a tract: a neighborhood, else its main road, else the tract number."""
    if named:
        name, n_lat, n_lon = min(named, key=lambda n: _distance_to_path(lat, lon, [(n[2], n[1])]))
        distance = _distance_to_path(lat, lon, [(n_lon, n_lat)])
        if distance <= NAME_EXACT_DISTANCE:
            return name
        if distance <= NAME_NEAR_DISTANCE:
            return f"Near {name}"
    if roads:
        return f"Near {_road_label(roads[0]['road'])}"
    return tract_name

METERS_PER_DEGREE_LAT = 110_574


def _feature_rings(feature):
    geometry = feature["geometry"]
    if geometry["type"] == "Polygon":
        return geometry["coordinates"]
    return [ring for polygon in geometry["coordinates"] for ring in polygon]


class _RoadIndex:
    """
    Finds the busiest roads within a radius of a point, quickly.

    Checking every road for every neighborhood would be slow (thousands x
    hundreds), so roads are filed into a grid of ~1 km squares first. Each
    road is filed under every square it passes within `radius` of, so a
    lookup only has to check the roads in one square.
    """
    CELL = 0.01  # degrees, roughly 1 km

    def __init__(self, roads, radius_meters):
        self.radius = radius_meters
        self.cells = {}
        pad_lat = radius_meters / METERS_PER_DEGREE_LAT
        for road in roads:
            lons = [p[0] for p in road["points"]]
            lats = [p[1] for p in road["points"]]
            pad_lon = pad_lat / math.cos(math.radians(lats[0]))
            for cx in range(self._cell(min(lons) - pad_lon), self._cell(max(lons) + pad_lon) + 1):
                for cy in range(self._cell(min(lats) - pad_lat), self._cell(max(lats) + pad_lat) + 1):
                    self.cells.setdefault((cx, cy), []).append(road)

    def _cell(self, degrees):
        return math.floor(degrees / self.CELL)

    def busiest_near(self, lat, lon):
        """Same shape as pipeline.get_nearby_roads: busiest first, one per road name."""
        busiest = {}
        for road in self.cells.get((self._cell(lon), self._cell(lat)), []):
            if _distance_to_path(lat, lon, road["points"]) <= self.radius:
                name = road["road"]
                busiest[name] = max(busiest.get(name, 0), road["vehicles_per_day"])
        found = [{"road": name, "vehicles_per_day": v} for name, v in busiest.items()]
        return sorted(found, key=lambda r: r["vehicles_per_day"], reverse=True)


def _distance_to_path(lat, lon, points):
    """
    Shortest distance in meters from a point to a road's line. Over a few
    km the Earth is flat enough to treat degrees as a flat grid (scaled so
    east-west and north-south meters match).
    """
    meters_per_degree_lon = METERS_PER_DEGREE_LAT * math.cos(math.radians(lat))

    def to_meters(p):
        return (p[0] - lon) * meters_per_degree_lon, (p[1] - lat) * METERS_PER_DEGREE_LAT

    best = float("inf")
    flat = [to_meters(p) for p in points]
    for (x1, y1), (x2, y2) in zip(flat, flat[1:] or flat):
        dx, dy = x2 - x1, y2 - y1
        length_squared = dx * dx + dy * dy
        # How far along the segment the closest point is (0 = start, 1 = end)
        t = 0.0 if length_squared == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / length_squared))
        best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return best


@lru_cache(maxsize=16)
def _load_city(city_id):
    """Everything about a city that doesn't depend on the business type."""
    areas = get_city_areas(city_id)
    tracts = areas["tracts"]

    # Demographics: one Census request per county the city touches, plus
    # the statewide numbers every tract is compared against
    counties = {(t["properties"]["STATE"], t["properties"]["COUNTY"]) for t in tracts}
    tract_demographics = {}
    state_demographics = {}
    for state, county in counties:
        state_demographics[state] = get_state_demographics(state)
        for tract_code, demographics in get_all_tract_demographics(state, county).items():
            tract_demographics[(state, county, tract_code)] = demographics

    # Which ZIP each tract's center falls in
    tract_zips = {}
    for tract in tracts:
        props = tract["properties"]
        tract_zips[(props["COUNTY"], props["TRACT"])] = next(
            (z for z in areas["zip_areas"] if point_in_rings(props["lon"], props["lat"], z["rings"])), None
        )

    # Traffic near each tract's center. Asking the federal traffic server
    # about each tract separately means hundreds of requests (and it starts
    # refusing), so download every counted road in the city once and
    # measure distances here instead.
    all_points = [point for t in tracts for ring in _feature_rings(t) for point in ring]
    pad = 0.01  # ~1 km, so roads just outside the city edge still count
    west, south = min(p[0] for p in all_points) - pad, min(p[1] for p in all_points) - pad
    east, north = max(p[0] for p in all_points) + pad, max(p[1] for p in all_points) + pad
    roads = get_roads_in_area(west, south, east, north, tracts[0]["properties"]["STATE"])
    road_index = _RoadIndex(roads, AREA_TRAFFIC_RADIUS)
    tract_roads = {
        (t["properties"]["COUNTY"], t["properties"]["TRACT"]):
            road_index.busiest_near(t["properties"]["lat"], t["properties"]["lon"])
        for t in tracts
    }

    zip_codes = tuple(sorted({z["zip_code"] for z in areas["zip_areas"]}))
    zip_counts = get_zip_business_counts(zip_codes, AREA_NAICS)

    # Everyday names for each tract ("Broad Ripple" instead of "Census Tract 3207")
    try:
        named = get_named_neighborhoods(west, south, east, north)
    except RuntimeError:
        named = []  # no names today - labels fall back to road names
    tract_labels = {
        (t["properties"]["COUNTY"], t["properties"]["TRACT"]): _area_label(
            t["properties"]["lat"], t["properties"]["lon"], named,
            tract_roads[(t["properties"]["COUNTY"], t["properties"]["TRACT"])], t["properties"]["NAME"])
        for t in tracts
    }

    return {
        "tracts": tracts,
        "tract_demographics": tract_demographics,
        "state_demographics": state_demographics,
        "tract_zips": tract_zips,
        "tract_roads": tract_roads,
        "zip_counts": zip_counts,
        "tract_labels": tract_labels,
    }


def score_city(city_id, business_type, weights=None):
    """
    Returns a GeoJSON FeatureCollection (one feature per tract, ready for the
    map) with each tract's scores in its properties, plus "suggestions":
    the top neighborhoods, best first. `weights` optionally replaces the
    Charter's Fit Score weights (see scoring.normalize_fit_weights).
    """
    data = _load_city(city_id)
    features = []

    for tract in data["tracts"]:
        props = tract["properties"]
        state, county, tract_code = props["STATE"], props["COUNTY"], props["TRACT"]
        demographics = data["tract_demographics"].get((state, county, tract_code))
        if demographics is None:
            continue  # no Census data for this tract (rare) - leave it off the map

        zip_area = data["tract_zips"][(county, tract_code)]
        roads = data["tract_roads"][(county, tract_code)]
        scores = calculate_area_scores(
            business_type,
            demographics,
            data["state_demographics"][state],
            props["AREALAND"] / 2_589_988,
            data["zip_counts"].get(zip_area["zip_code"], {}) if zip_area else None,
            zip_area["land_sq_miles"] if zip_area else None,
            roads,
            weights,
        )

        features.append({
            "type": "Feature",
            "geometry": tract["geometry"],
            "properties": {
                "name": props["NAME"],                                    # "Census Tract 3207"
                "label": data["tract_labels"][(county, tract_code)],      # "Broad Ripple"
                "lat": props["lat"],
                "lon": props["lon"],
                "zip_code": zip_area["zip_code"] if zip_area else None,
                "population": demographics["population"],
                "median_income": demographics["median_income"],
                "busiest_road": roads[0] if roads else None,
                **scores,
            },
        })

    candidates = [f for f in features if f["properties"]["population"] >= MIN_POPULATION_TO_SUGGEST]
    candidates.sort(key=lambda f: f["properties"]["fit_score"], reverse=True)

    # Take the best neighborhoods, but skip any that sit right next to one
    # already picked - five suggestions on the same few blocks isn't useful.
    picked = []
    for candidate in candidates:
        c = candidate["properties"]
        too_close = any(
            _distance_to_path(c["lat"], c["lon"], [(p["properties"]["lon"], p["properties"]["lat"])])
            < MIN_SUGGESTION_SPACING for p in picked
        )
        if not too_close:
            picked.append(candidate)
        if len(picked) == TOP_SUGGESTIONS:
            break

    suggestions = [{
        "rank": rank,
        "name": f["properties"]["name"],
        "label": f["properties"]["label"],
        "lat": f["properties"]["lat"],
        "lon": f["properties"]["lon"],
        "fit_score": f["properties"]["fit_score"],
        "breakdown": f["properties"]["breakdown"],
        "busiest_road": f["properties"]["busiest_road"],
    } for rank, f in enumerate(picked, start=1)]

    return {
        "type": "FeatureCollection",
        "city": CITY_BY_ID.get(city_id, city_id),
        "business_type": business_type,
        "features": features,
        "suggestions": suggestions,
    }
