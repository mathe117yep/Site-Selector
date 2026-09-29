import math
import os
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
         "state": "18", "county": "097", "tract": "354201",
         "tract_land_sq_miles": 0.36, "zip_code": "46204", "zip_land_sq_miles": 1.9}

    Returns None if the address couldn't be matched.
    """
    url = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"

    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "layers": "Census Tracts,2020 Census ZIP Code Tabulation Areas",
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
    tract = best_match["geographies"]["Census Tracts"][0]
    # A few addresses (e.g. some PO boxes) have no ZIP area - that's OK
    zip_areas = best_match["geographies"].get("2020 Census ZIP Code Tabulation Areas") or [None]
    zip_area = zip_areas[0]

    return {
        "lat": coordinates["y"],
        "lon": coordinates["x"],
        "matched_address": best_match["matchedAddress"],
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
    """Asks the Census API for our variables for one geography (a tract or a county)."""
    variables = [POPULATION, MEDIAN_INCOME, *AGE_20_TO_39, *AGE_65_PLUS]
    params = {"get": ",".join(variables), "key": _get_census_key(), **geo_params}

    response = requests.get(ACS_URL, params=params, timeout=15)
    response.raise_for_status()

    # The API answers with a table: first row is headers, second row is values
    headers, values = response.json()
    row = dict(zip(headers, values))

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
    return _fetch_acs({"for": f"tract:{tract}", "in": f"state:{state} county:{county}"})


@lru_cache(maxsize=256)  # counties don't change, so only ask Census once per county
def get_county_demographics(state, county):
    return _fetch_acs({"for": f"county:{county}", "in": f"state:{state}"})


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


# --- Nearby places from OpenStreetMap (free, no key needed) ---

# Overpass is OpenStreetMap's search service. The main server is often
# overloaded, so we try a few public copies of it in order.
OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

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
    for server in OVERPASS_SERVERS:
        try:
            response = requests.post(server, data={"data": query}, timeout=30,
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
