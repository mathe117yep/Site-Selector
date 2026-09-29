import json
import math
import os
import time
from functools import lru_cache

import requests


def geocode_address(address):
    """
    Converts a street address into latitude/longitude coordinates using
    the U.S. Census Bureau's free geocoding service (no API key needed).

    We use the "geographies" version of the geocoder so that, along with
    the coordinates, we also get the Census Tract the address sits in.
    The tract is what we need to look up neighborhood demographics.

    Returns a dict like:
        {"lat": 39.768, "lon": -86.158, "matched_address": "...",
         "street": "200 E WASHINGTON ST",
         "state": "18", "county": "097", "tract": "354201",
         "tract_land_sq_miles": 0.36, "zip_code": "46204", "zip_land_sq_miles": 1.9}

    Returns None if the address couldn't be matched.
    """
    url = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"

    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "layers": GEOCODER_LAYERS,
        "format": "json"
    }

    response = requests.get(url, params=params, timeout=15)  # don't hang forever if Census is slow
    response.raise_for_status()  # crashes loudly if the request itself failed

    data = response.json()
    matches = data.get("result", {}).get("addressMatches", [])

    if not matches:
        return None

    best_match = matches[0]
    coordinates = best_match["coordinates"]
    matched_address = best_match["matchedAddress"]
    return _build_location(coordinates["y"], coordinates["x"], best_match["geographies"],
                           label=matched_address, street=matched_address.split(",")[0])


def locate_point(lat, lon):
    """
    Same as geocode_address, but for a spot clicked on the map instead of a
    typed address. There's no street address, so "street" is None.
    Returns None if the point isn't inside a U.S. census tract (e.g. a lake).
    """
    url = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
    params = {
        "x": lon,
        "y": lat,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "layers": GEOCODER_LAYERS,
        "format": "json"
    }
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()

    geographies = response.json().get("result", {}).get("geographies", {})
    if not geographies.get("Census Tracts"):
        return None
    return _build_location(lat, lon, geographies, label=f"Map point ({lat:.5f}, {lon:.5f})", street=None)


GEOCODER_LAYERS = "Census Tracts,2020 Census ZIP Code Tabulation Areas"


def _build_location(lat, lon, geographies, label, street):
    """Pulls the tract and ZIP info we need out of a Census geocoder answer."""
    tract = geographies["Census Tracts"][0]
    # A few addresses (e.g. some PO boxes) have no ZIP area - that's OK
    zip_areas = geographies.get("2020 Census ZIP Code Tabulation Areas") or [None]
    zip_area = zip_areas[0]

    return {
        "lat": lat,
        "lon": lon,
        "matched_address": label,
        "street": street,
        "state": tract["STATE"],
        "county": tract["COUNTY"],
        "tract": tract["TRACT"],
        # AREALAND is in square meters; 2,589,988 sq meters = 1 sq mile
        "tract_land_sq_miles": tract["AREALAND"] / 2_589_988,
        "zip_code": zip_area["ZCTA5"] if zip_area else None,
        "zip_land_sq_miles": zip_area["AREALAND"] / 2_589_988 if zip_area else None
    }


# --- Census demographics (American Community Survey 5-year estimates) ---

ACS_YEAR = 2024
ACS_URL = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"

# ACS variable codes. B01001 is "Sex by Age": each code is one age bucket
# for men (_008 to _025) or women (_032 to _049).
POPULATION = "B01003_001E"
MEDIAN_INCOME = "B19013_001E"
AGE_20_TO_39 = [f"B01001_{n:03d}E" for n in [*range(8, 14), *range(32, 38)]]
AGE_65_PLUS = [f"B01001_{n:03d}E" for n in [*range(20, 26), *range(44, 50)]]


def _get_census_key():
    key = os.environ.get("CENSUS_API_KEY")

    # A PowerShell window opened before `setx` won't have the key in its
    # environment, so on Windows also check where setx saved it.
    if not key and os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as env:
                key = winreg.QueryValueEx(env, "CENSUS_API_KEY")[0]
        except OSError:
            pass

    if not key:
        raise RuntimeError(
            "CENSUS_API_KEY is not set. Get a free key at "
            "https://api.census.gov/data/key_signup.html and set it as an environment variable."
        )
    return key


def _fetch_acs(geo_params):
    """
    Asks the Census API for our variables for one or more geographies.
    Returns a list of (row, demographics) pairs - row is the raw answer,
    which includes geography codes like "tract".
    """
    variables = [POPULATION, MEDIAN_INCOME, *AGE_20_TO_39, *AGE_65_PLUS]
    params = {"get": ",".join(variables), "key": _get_census_key(), **geo_params}

    response = requests.get(ACS_URL, params=params, timeout=30)
    response.raise_for_status()

    # The API answers with a table: first row is headers, then one row per place
    headers, *rows = response.json()
    return [(dict(zip(headers, values)), _summarize_acs_row(dict(zip(headers, values)))) for values in rows]


def _summarize_acs_row(row):
    population = int(row[POPULATION])
    median_income = int(row[MEDIAN_INCOME])
    young_adults = sum(int(row[v]) for v in AGE_20_TO_39)
    seniors = sum(int(row[v]) for v in AGE_65_PLUS)

    return {
        "population": population,
        # Census uses big negative numbers (like -666666666) to mean "no data"
        "median_income": median_income if median_income > 0 else None,
        "share_age_20_39": young_adults / population if population else None,
        "share_age_65_plus": seniors / population if population else None
    }


def get_tract_demographics(state, county, tract):
    return _fetch_acs({"for": f"tract:{tract}", "in": f"state:{state} county:{county}"})[0][1]


@lru_cache(maxsize=256)  # counties don't change, so only ask Census once per county
def get_county_demographics(state, county):
    return _fetch_acs({"for": f"county:{county}", "in": f"state:{state}"})[0][1]


@lru_cache(maxsize=64)
def get_all_tract_demographics(state, county):
    """Every tract in a county in one request: {"354201": {...demographics}, ...}"""
    rows = _fetch_acs({"for": "tract:*", "in": f"state:{state} county:{county}"})
    return {row["tract"]: demographics for row, demographics in rows}


# --- Business counts by ZIP code (Census County Business Patterns) ---
#
# Official count of businesses of each type in every ZIP code. It's
# complete everywhere (unlike OpenStreetMap), but only tells us the ZIP,
# not exact locations. Only counts businesses with paid employees, so a
# solo trainer or one-chair salon won't be in it.

CBP_YEAR = 2023
CBP_URL = f"https://api.census.gov/data/{CBP_YEAR}/cbp"


@lru_cache(maxsize=256)
def get_zip_business_count(zip_code, naics_codes):
    """
    Counts businesses in a ZIP code across the given industry codes
    (NAICS codes, e.g. ("722515",) for coffee shops and snack bars).
    naics_codes must be a tuple so the result can be cached.
    """
    params = {
        "get": "ESTAB",
        "for": f"zip code:{zip_code}",
        "NAICS2017": list(naics_codes),  # requests sends one NAICS2017= per code
        "key": _get_census_key()
    }
    response = requests.get(CBP_URL, params=params, timeout=15)
    response.raise_for_status()

    # Census replies with an empty body (status 204) when the ZIP has none
    # of these businesses, and simply leaves out industries with zero.
    if response.status_code == 204 or not response.text.strip():
        return 0

    headers, *rows = response.json()
    estab_column = headers.index("ESTAB")
    return sum(int(row[estab_column]) for row in rows)


@lru_cache(maxsize=64)
def get_zip_business_counts(zip_codes, naics_codes):
    """
    Business counts for many ZIP codes and industry codes in one request.
    zip_codes and naics_codes must be tuples (so results can be cached).
    Returns {"46204": {"722515": 36, "54": 412, ...}, ...}. Industries a
    ZIP has none of are simply missing - treat missing as 0.
    """
    counts = {zip_code: {} for zip_code in zip_codes}
    # The Census API limits how long a request can be, so ask in batches
    for start in range(0, len(zip_codes), 50):
        batch = zip_codes[start:start + 50]
        params = {
            "get": "ESTAB",
            "for": "zip code:" + ",".join(batch),
            "NAICS2017": list(naics_codes),
            "key": _get_census_key()
        }
        response = requests.get(CBP_URL, params=params, timeout=30)
        response.raise_for_status()
        if response.status_code == 204 or not response.text.strip():
            continue
        headers, *rows = response.json()
        for row in rows:
            record = dict(zip(headers, row))
            counts[record["zip code"]][record["NAICS2017"]] = int(record["ESTAB"])
    return counts


# --- City and neighborhood boundaries (Census TIGERweb maps) ---

TIGERWEB_URL = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_Current/MapServer"
PLACES_LAYER = 28      # incorporated cities and towns
TRACTS_LAYER = 8       # census tracts (our "neighborhoods")
ZIP_AREAS_LAYER = 2    # ZIP code areas

# Cities offered in the map's "Find best areas" menu. The value is the
# name Census uses; Indianapolis is listed as "city (balance)" because it
# shares a government with Marion County.
INDIANA_CITIES = {
    "Indianapolis": "Indianapolis city (balance)",
    "Anderson": "Anderson", "Bloomington": "Bloomington", "Carmel": "Carmel",
    "Columbus": "Columbus", "Elkhart": "Elkhart", "Evansville": "Evansville",
    "Fishers": "Fishers", "Fort Wayne": "Fort Wayne", "Gary": "Gary",
    "Greenwood": "Greenwood", "Hammond": "Hammond", "Jeffersonville": "Jeffersonville",
    "Kokomo": "Kokomo", "Lafayette": "Lafayette", "Lawrence": "Lawrence",
    "Mishawaka": "Mishawaka", "Muncie": "Muncie", "New Albany": "New Albany",
    "Noblesville": "Noblesville", "Plainfield": "Plainfield", "Portage": "Portage",
    "Richmond": "Richmond", "South Bend": "South Bend", "Terre Haute": "Terre Haute",
    "West Lafayette": "West Lafayette", "Westfield": "Westfield", "Zionsville": "Zionsville",
}


def _tigerweb_query(layer, **params):
    params.setdefault("f", "json")
    # POST, because city outlines can be too long to fit in a web address
    response = requests.post(f"{TIGERWEB_URL}/{layer}/query", data=params, timeout=60)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(f"Census map service error: {data['error'].get('message')}")
    return data


def point_in_rings(lon, lat, rings):
    """
    True if a point is inside a shape made of rings (lists of [lon, lat]).
    Uses the classic "ray casting" trick: draw a line from the point to the
    right and count how many edges it crosses - odd means inside. This also
    handles holes and shapes made of several separate pieces.
    """
    inside = False
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            if (y1 > lat) != (y2 > lat):
                crossing_x = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
                if lon < crossing_x:
                    inside = not inside
    return inside


def _geojson_rings(geometry):
    """Flattens a GeoJSON Polygon or MultiPolygon into one list of rings."""
    if geometry["type"] == "Polygon":
        return geometry["coordinates"]
    return [ring for polygon in geometry["coordinates"] for ring in polygon]


@lru_cache(maxsize=64)
def get_city_areas(city):
    """
    Loads a city's outline, its census tracts, and the ZIP code areas that
    cover it. Returns:
        {"tracts": [GeoJSON features, each with properties STATE, COUNTY,
                    TRACT, NAME, AREALAND, and center "lat"/"lon"],
         "zip_areas": [{"zip_code", "land_sq_miles", "rings"}, ...]}
    """
    census_name = INDIANA_CITIES[city]
    place = _tigerweb_query(
        PLACES_LAYER, where=f"STATE='18' AND BASENAME='{census_name}'",
        outFields="NAME", returnGeometry="true", outSR=4326,
        maxAllowableOffset=0.0005  # simplify outlines to ~50 m detail - plenty for a map
    )
    if not place["features"]:
        raise RuntimeError(f"Couldn't find the city limits for {city}.")
    city_rings = place["features"][0]["geometry"]["rings"]
    city_shape = json.dumps({"rings": city_rings, "spatialReference": {"wkid": 4326}})
    shape_search = {"geometry": city_shape, "geometryType": "esriGeometryPolygon", "inSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects", "outSR": 4326, "returnGeometry": "true"}

    tract_data = _tigerweb_query(
        TRACTS_LAYER, outFields="STATE,COUNTY,TRACT,NAME,AREALAND,INTPTLAT,INTPTLON",
        maxAllowableOffset=0.0003, f="geojson", **shape_search
    )
    # "Intersects" also catches tracts that just touch the city edge. Keep
    # only tracts whose center point is inside the city limits.
    tracts = []
    for feature in tract_data["features"]:
        props = feature["properties"]
        props["lat"], props["lon"] = float(props.pop("INTPTLAT")), float(props.pop("INTPTLON"))
        if point_in_rings(props["lon"], props["lat"], city_rings):
            tracts.append(feature)

    zip_data = _tigerweb_query(
        ZIP_AREAS_LAYER, outFields="ZCTA5,AREALAND", maxAllowableOffset=0.0005, f="geojson", **shape_search
    )
    zip_areas = [{
        "zip_code": feature["properties"]["ZCTA5"],
        "land_sq_miles": feature["properties"]["AREALAND"] / 2_589_988,
        "rings": _geojson_rings(feature["geometry"])
    } for feature in zip_data["features"]]

    return {"tracts": tracts, "zip_areas": zip_areas}


# --- Traffic counts (Federal Highway Administration, HPMS) ---
#
# Every state reports yearly traffic counts to the feds, who publish them
# as one map service per state per year. AADT = "annual average daily
# traffic", the number of vehicles passing on a typical day. (INDOT's own
# public layers stopped updating in 2014, so we use the federal copy.)

HPMS_YEAR = 2024
HPMS_URL = "https://geo.dot.gov/server/rest/services/Hosted/HPMS_FULL_{state}_{year}/FeatureServer/0/query"

STATE_ABBREVIATIONS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
    "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
    "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
    "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY",
}

# Road classes 1 and 2 are interstates and freeways. Cars on them can't
# stop at a storefront, so we leave them out.
FREEWAY_CLASSES = (1, 2)


@lru_cache(maxsize=256)
def get_nearby_roads(lat, lon, radius_meters, state_fips):
    """
    Finds regular (non-freeway) roads within radius_meters of a point that
    have a traffic count. Returns the busiest first, one entry per road:
        [{"road": "WASHINGTON ST", "vehicles_per_day": 27610}, ...]
    """
    url = HPMS_URL.format(state=STATE_ABBREVIATIONS[state_fips], year=HPMS_YEAR)
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,  # our coordinates are plain latitude/longitude
        "distance": radius_meters,
        "units": "esriSRUnit_Meter",
        "where": f"aadt > 0 AND f_system NOT IN {FREEWAY_CLASSES}",
        "outFields": "routename,aadt",
        "returnGeometry": "false",
        "f": "json"
    }
    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(f"Traffic data service error: {data['error'].get('message')}")

    # A road is split into many short segments - keep the busiest per road
    busiest = {}
    for feature in data["features"]:
        road = feature["attributes"]["routename"] or "(unnamed road)"
        busiest[road] = max(busiest.get(road, 0), feature["attributes"]["aadt"])

    roads = [{"road": road, "vehicles_per_day": aadt} for road, aadt in busiest.items()]
    return sorted(roads, key=lambda r: r["vehicles_per_day"], reverse=True)


@lru_cache(maxsize=16)
def get_roads_in_area(west, south, east, north, state_fips):
    """
    Downloads every regular (non-freeway) counted road inside a box, with
    its shape - used for whole-city heat maps, where asking about each
    neighborhood separately would mean hundreds of requests. Returns:
        [{"road": "38TH ST", "vehicles_per_day": 32519,
          "points": [(lon, lat), (lon, lat), ...]}, ...]
    """
    url = HPMS_URL.format(state=STATE_ABBREVIATIONS[state_fips], year=HPMS_YEAR)
    roads = []
    offset = 0
    while True:
        params = {
            "geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "where": f"aadt > 0 AND f_system NOT IN {FREEWAY_CLASSES}",
            "outFields": "routename,aadt",
            "returnGeometry": "true",
            "outSR": 4326,
            "maxAllowableOffset": 0.0002,  # simplify shapes to ~20 m detail
            "resultOffset": offset,
            "resultRecordCount": 2000,     # the server's page size limit
            "f": "json"
        }
        response = requests.get(url, params=params, timeout=60)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"Traffic data service error: {data['error'].get('message')}")

        for feature in data["features"]:
            for path in feature.get("geometry", {}).get("paths", []):
                roads.append({
                    "road": feature["attributes"]["routename"] or "(unnamed road)",
                    "vehicles_per_day": feature["attributes"]["aadt"],
                    "points": [tuple(point) for point in path],
                })

        # The server hands results out in pages; keep going until the last one
        if not data.get("exceededTransferLimit"):
            return roads
        offset += len(data["features"])


# --- Nearby places from OpenStreetMap (free, no key needed) ---

# Overpass is OpenStreetMap's search service. The main server is often
# overloaded, so we try a few public copies of it in order.
OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Most seconds to spend trying OpenStreetMap servers before giving up. A
# healthy server answers in 2-15 seconds; waiting longer mostly means
# waiting on busy ones.
OVERPASS_TIME_BUDGET = 25

AMENITIES_WE_CARE_ABOUT = (
    "cafe|restaurant|fast_food|bar|pub|doctors|clinic|dentist|pharmacy|hospital"
    "|school|college|university|library|theatre|cinema"
)


def _categorize(tags):
    """
    Sorts an OpenStreetMap place into one of our own simple categories
    (like "cafe" or "office"), based on its OSM tags. Returns None for
    places we don't use.
    """
    amenity = tags.get("amenity")
    shop = tags.get("shop")
    leisure = tags.get("leisure")
    healthcare = tags.get("healthcare")

    if amenity == "cafe" or shop == "coffee":
        return "cafe"
    if amenity in ("restaurant", "fast_food"):
        return "restaurant"
    if amenity in ("bar", "pub"):
        return "bar"
    if shop in ("hairdresser", "beauty", "nails", "cosmetics"):
        return "salon"
    if shop in ("clothes", "boutique", "shoes", "fashion_accessories", "jewelry", "bag"):
        return "clothing"
    if leisure in ("fitness_centre", "sports_centre"):
        return "gym"
    if amenity in ("doctors", "clinic") or healthcare in ("doctor", "clinic"):
        return "doctor"
    if amenity == "pharmacy" or shop == "chemist" or healthcare == "pharmacy":
        return "pharmacy"
    if amenity == "hospital" or healthcare == "hospital":
        return "hospital"
    if amenity == "dentist" or healthcare:
        return "other_medical"
    if amenity in ("college", "university"):
        return "college"
    if amenity == "school":
        return "school"
    if amenity == "library":
        return "library"
    if amenity in ("theatre", "cinema"):
        return "entertainment"
    if tags.get("tourism") == "hotel":
        return "hotel"
    if leisure == "park":
        return "park"
    if "office" in tags:
        return "office"
    if shop and shop != "vacant":
        return "retail"
    return None


def _distance_meters(lat1, lon1, lat2, lon2):
    """Straight-line distance between two points on Earth (haversine formula)."""
    earth_radius = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * earth_radius * math.asin(math.sqrt(a))


@lru_cache(maxsize=256)  # scoring the same spot twice shouldn't hit OSM twice
def get_nearby_places(lat, lon, radius_meters):
    """
    Finds businesses and other places within radius_meters of a point.

    Returns a list of dicts like:
        {"category": "cafe", "name": "Hubbard & Cravens", "distance_meters": 412}
    """
    around = f"(around:{radius_meters},{lat},{lon})"
    query = f"""
        [out:json][timeout:25];
        (
          nwr{around}[amenity~"^({AMENITIES_WE_CARE_ABOUT})$"];
          nwr{around}[shop];
          nwr{around}[leisure~"^(fitness_centre|sports_centre|park)$"];
          nwr{around}[office];
          nwr{around}[tourism=hotel];
          nwr{around}[healthcare];
        );
        out center tags;
    """

    elements = None
    deadline = time.monotonic() + OVERPASS_TIME_BUDGET
    for server in OVERPASS_SERVERS:
        seconds_left = deadline - time.monotonic()
        if seconds_left < 3:
            break  # out of time - the caller falls back to Census estimates
        try:
            response = requests.post(server, data={"data": query}, timeout=(5, seconds_left),
                                     headers={"User-Agent": "SiteSelectorProject/0.1"})
            response.raise_for_status()
            elements = response.json()["elements"]
            break
        except (requests.RequestException, ValueError, KeyError):
            continue  # this server is busy or broken - try the next one

    if elements is None:
        raise RuntimeError("OpenStreetMap servers are busy right now. Please try again in a minute.")

    places = []
    for element in elements:
        category = _categorize(element.get("tags", {}))
        if category is None:
            continue
        # Single points have lat/lon directly; buildings and parks have a "center"
        point = element if "lat" in element else element.get("center")
        if point is None:
            continue
        places.append({
            "category": category,
            "name": element["tags"].get("name", "(unnamed)"),
            "distance_meters": round(_distance_meters(lat, lon, point["lat"], point["lon"]))
        })

    return places


if __name__ == "__main__":
    # Quick manual test - try a real Indianapolis address
    location = geocode_address("200 E Washington St, Indianapolis, IN")
    print(location)
    print(get_tract_demographics(location["state"], location["county"], location["tract"]))
    print(get_county_demographics(location["state"], location["county"]))
