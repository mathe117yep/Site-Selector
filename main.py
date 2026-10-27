from pathlib import Path
from typing import Annotated

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from scoring import (calculate_fit_score, calculate_demand_score, calculate_competition_score,
                     calculate_land_use_score, calculate_traffic_score, calculate_area_land_use_score,
                     normalize_business_type, normalize_fit_weights, DEMAND_WEIGHTS, ONE_MILE, COMPETITOR_NAICS, AREA_NAICS,
                     COMPETITOR_CATEGORIES, COMPLEMENT_WEIGHTS, SEARCH_RADIUS,
                     FRONTAGE_RADIUS, SETBACK_RADIUS, ACCESS_RADIUS)
from pipeline import (geocode_address, locate_point, get_tract_demographics, get_state_demographics,
                      get_nearby_places, get_zip_business_count, get_zip_business_counts, get_nearby_roads,
                      INDIANA_CITIES)
from areas import score_city

app = FastAPI()

STATIC_FOLDER = Path(__file__).parent / "static"

# Images the page uses (the SiteScout logo icon and the browser-tab icon)
app.mount("/static", StaticFiles(directory=STATIC_FOLDER), name="static")

# Fewer mapped places than this within a mile triggers a data-quality warning
LOW_COVERAGE_PLACE_COUNT = 30

# A map click this close to a counted road counts as being "on" that road
ON_ROAD_RADIUS = 40  # meters

# Site scores cover the 12 Midwest states (Census Bureau definition), keyed
# by state FIPS code. Each has 2024 federal traffic data and was tested with
# a downtown address. Other places get a clear message instead of an
# untested score. (The neighborhood heat map's city list is Indiana's.)
SUPPORTED_STATES = {
    "17": "Illinois", "18": "Indiana", "19": "Iowa", "20": "Kansas", "26": "Michigan", "27": "Minnesota",
    "29": "Missouri", "31": "Nebraska", "38": "North Dakota", "39": "Ohio", "46": "South Dakota", "55": "Wisconsin",
}

# Shortest and longest address text worth sending to the Census geocoder
MIN_ADDRESS_LENGTH = 5
MAX_ADDRESS_LENGTH = 200


# These define exactly what a request to our app must look like.
# If someone sends bad data (missing a field, wrong type), FastAPI
# will automatically reject it with a clear error before it ever
# reaches our code.
class FitWeights(BaseModel):
    # "Advanced filters" on the website. Any numbers from 0 to 100 - they
    # don't have to add up to 100, because they're scaled to shares.
    demand: float = Field(30, ge=0, le=100)
    competition: float = Field(25, ge=0, le=100)
    traffic: float = Field(25, ge=0, le=100)
    land_use: float = Field(20, ge=0, le=100)


# An optional 0-100 weight in a web address, e.g. /area_scores?...&demand_weight=40
OptionalWeight = Annotated[float | None, Query(ge=0, le=100)]


class ScoreRequest(BaseModel):
    address: str
    business_type: str
    weights: FitWeights | None = None  # leave out to use the Charter weights
    # IDs of nearby businesses the visitor marked "not a competitor" on the map
    removed_places: list[str] = Field(default_factory=list, max_length=500)


class PointScoreRequest(BaseModel):
    lat: float
    lon: float
    business_type: str
    weights: FitWeights | None = None
    removed_places: list[str] = Field(default_factory=list, max_length=500)


@app.get("/")
def home_page():
    # The web interface (a single page in the static folder). "no-cache"
    # makes browsers check for a newer version every time, so people see
    # updates right away instead of an old saved copy.
    return FileResponse(STATIC_FOLDER / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health_check():
    # Hosting services ping this to check the app is up
    return {"message": "SiteScout API is running"}


def _weights_or_error(weights):
    """FitWeights (or None) -> a plain dict for the scoring code, checked."""
    if weights is None:
        return None
    as_dict = weights.model_dump()
    try:
        normalize_fit_weights(as_dict)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return as_dict


def _supported_state_or_error(location, what):
    if location["state"] not in SUPPORTED_STATES:
        raise HTTPException(
            status_code=422,
            detail=f"{what} is outside the Midwest. SiteScout covers the 12 Midwest states: "
                   f"{', '.join(sorted(SUPPORTED_STATES.values()))}."
        )


def _business_type_or_error(raw_business_type):
    business_type = normalize_business_type(raw_business_type)
    if business_type is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported business type. Try one of: {', '.join(DEMAND_WEIGHTS)}"
        )
    return business_type


@app.post("/score")
def score_site(request: ScoreRequest):
    business_type = _business_type_or_error(request.business_type)

    address = " ".join(request.address.split())  # trim and collapse extra spaces
    if len(address) < MIN_ADDRESS_LENGTH:
        raise HTTPException(status_code=422, detail="Enter a street address, city and state, e.g. 200 E Washington St, Indianapolis, IN.")
    if len(address) > MAX_ADDRESS_LENGTH:
        raise HTTPException(status_code=422, detail=f"That address is too long. Enter just the street address, city and state (under {MAX_ADDRESS_LENGTH} characters).")

    # Turn the address into coordinates + census tract. Every live data
    # pull (Census, OSM, etc.) needs this, so it happens first.
    try:
        location = geocode_address(address)
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="The Census address service didn't respond. Please try again.")
    if location is None:
        raise HTTPException(status_code=422, detail="Could not match that address. Try including city and state.")
    _supported_state_or_error(location, "That address")

    result = score_location(location, business_type, _weights_or_error(request.weights), request.removed_places)
    return {"address": request.address, **result}


@app.post("/score_point")
def score_point(request: PointScoreRequest):
    """Scores a spot clicked on the map."""
    business_type = _business_type_or_error(request.business_type)
    if not (-90 <= request.lat <= 90 and -180 <= request.lon <= 180):
        raise HTTPException(status_code=422, detail="That isn't a valid map location. Tap a spot on the map in the Midwest.")
    try:
        location = locate_point(request.lat, request.lon)
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="The Census location service didn't respond. Please try again.")
    if location is None:
        raise HTTPException(status_code=422, detail="That spot isn't in a U.S. census tract. Try clicking on land.")
    _supported_state_or_error(location, "That spot")
    return score_location(location, business_type, _weights_or_error(request.weights), request.removed_places)


@app.get("/cities")
def list_cities():
    """Cities offered in the map's "Find best areas" menu."""
    return sorted(INDIANA_CITIES)


@app.get("/area_scores")
def area_scores(city: str, business_type: str,
                demand_weight: OptionalWeight = None, competition_weight: OptionalWeight = None,
                traffic_weight: OptionalWeight = None, land_use_weight: OptionalWeight = None):
    """
    Scores every neighborhood in a city, for the heat map. The four
    *_weight values are optional "Advanced filters"; give all four or none.
    """
    business_type = _business_type_or_error(business_type)
    given = [demand_weight, competition_weight, traffic_weight, land_use_weight]
    weights = None
    if any(w is not None for w in given):
        if any(w is None for w in given):
            raise HTTPException(status_code=422, detail="Give all four weights, or none.")
        weights = _weights_or_error(FitWeights(demand=demand_weight, competition=competition_weight,
                                               traffic=traffic_weight, land_use=land_use_weight))
    if city not in INDIANA_CITIES:
        raise HTTPException(status_code=422, detail=f"Unknown city. Try one of: {', '.join(sorted(INDIANA_CITIES))}")
    try:
        return score_city(city, business_type, weights)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error))
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="A Census or traffic data service didn't respond. Please try again in a minute.")


def _nearby_businesses_for_map(places, business_type, removed_places=()):
    """
    Competitors and customer-bringing businesses within 1 mile, for the
    red and green pins on the map. Places farther than the business type's
    scoring radius (half a mile for walk-in businesses) are still shown,
    marked as not counted in the score. Places the visitor removed are
    listed separately (gray pins) so they can be put back.
    """
    scoring_radius = SEARCH_RADIUS[business_type]

    def pin(place):
        return {
            "id": place["id"],
            "name": place["name"],
            "category": place["category"],
            "lat": place["lat"],
            "lon": place["lon"],
            "address": place["address"],
            "distance_miles": round(place["distance_meters"] / ONE_MILE, 2),
            "counts_in_score": place["distance_meters"] <= scoring_radius,
        }

    within_mile = sorted((p for p in places or [] if p["distance_meters"] <= ONE_MILE),
                         key=lambda p: p["distance_meters"])
    return {
        "available": places is not None,  # False when no map data source answered
        "radius_miles": 1.0,
        "scoring_radius_miles": round(scoring_radius / ONE_MILE, 2),
        "competitors": [pin(p) for p in within_mile if p["category"] in COMPETITOR_CATEGORIES[business_type]],
        "synergistic": [pin(p) for p in within_mile if p["category"] in COMPLEMENT_WEIGHTS[business_type]],
        "removed": [pin(p) for p in sorted(removed_places, key=lambda p: p["distance_meters"])
                    if p["distance_meters"] <= ONE_MILE],
    }


def score_location(location, business_type, weights=None, removed_ids=()):
    """
    The full site score for one spot. `location` comes from geocode_address
    (typed address) or locate_point (map click). `weights` optionally
    replaces the Charter's Fit Score weights. `removed_ids` are nearby
    businesses the visitor marked "not a competitor"; they're left out.
    """
    warnings = []

    # Pull live data - Census demographics for the tract vs. its state,
    # the official count of competitors in the ZIP code, and traffic counts.
    try:
        tract = get_tract_demographics(location["state"], location["county"], location["tract"])
        state = get_state_demographics(location["state"])
        zip_count = None
        if location["zip_code"]:
            zip_count = get_zip_business_count(location["zip_code"], COMPETITOR_NAICS[business_type])
        frontage_roads = get_nearby_roads(location["lat"], location["lon"], FRONTAGE_RADIUS, location["state"])
        setback_roads = get_nearby_roads(location["lat"], location["lon"], SETBACK_RADIUS, location["state"])
        access_roads = get_nearby_roads(location["lat"], location["lon"], ACCESS_RADIUS, location["state"])
        site_street = location["street"]
        if site_street is None:
            # A map click has no street address - treat the road it's
            # right on (if any) as the site's street.
            on_road = get_nearby_roads(location["lat"], location["lon"], ON_ROAD_RADIUS, location["state"])
            site_street = on_road[0]["road"] if on_road else ""
    except RuntimeError as error:  # e.g. missing CENSUS_API_KEY
        raise HTTPException(status_code=503, detail=str(error))
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="A Census or traffic data service didn't respond. Please try again in a minute.")

    # Nearby places (OpenStreetMap data, via Geoapify or the public
    # servers). If every source fails, fall back to Census ZIP code
    # estimates instead of failing.
    try:
        nearby = get_nearby_places(location["lat"], location["lon"], ONE_MILE)
        places, places_source = nearby["places"], nearby["source"]
    except RuntimeError:
        places, places_source = None, "Census ZIP code estimate"

    # Leave out businesses the visitor removed. (New lists - `places` is
    # cached and shared, so it must not be changed in place.)
    removed_ids = set(removed_ids)
    removed_places = [p for p in places or [] if p["id"] in removed_ids]
    if places is not None:
        places = [p for p in places if p["id"] not in removed_ids]
    removed_competitors = sum(
        1 for p in removed_places
        if p["category"] in COMPETITOR_CATEGORIES[business_type]
        and p["distance_meters"] <= SEARCH_RADIUS[business_type]
    )

    # Turn the raw data into 0-100 sub-scores
    demand_result = calculate_demand_score(business_type, tract, state, location["tract_land_sq_miles"])
    competition_result = calculate_competition_score(
        business_type, places or [], zip_count, location["zip_land_sq_miles"], removed_competitors
    )
    traffic_result = calculate_traffic_score(site_street, frontage_roads, setback_roads, access_roads)

    if places is not None:
        land_use_result = calculate_land_use_score(business_type, places)
        land_use = land_use_result["land_use"]
        complementary_places = land_use_result["complementary_places_nearby"]
        # OpenStreetMap coverage varies a lot by neighborhood. Very few
        # mapped places usually means missing data, not an empty area.
        if len(places) < LOW_COVERAGE_PLACE_COUNT:
            warnings.append(
                f"Only {len(places)} places are mapped in OpenStreetMap within a mile of this spot. "
                "Data is likely incomplete here, so Land Use is probably understated. "
                "(Competition is cross-checked against Census ZIP code counts.)"
            )
    else:
        warnings.append(
            "The map data services were too busy to answer, so Competition and Land Use are estimates "
            "from Census ZIP code counts. Try again in a minute for exact nearby businesses."
        )
        complementary_places = {}
        land_use = 50.0
        if location["zip_code"]:
            try:
                zip_counts = get_zip_business_counts((location["zip_code"],), AREA_NAICS)[location["zip_code"]]
                land_use = calculate_area_land_use_score(business_type, zip_counts, location["zip_land_sq_miles"])
            except requests.RequestException:
                pass  # keep the neutral 50

    result = calculate_fit_score(
        demand_result["demand"], competition_result["competition"], traffic_result["traffic"], land_use,
        weights
    )

    return {
        "nearby_businesses": _nearby_businesses_for_map(places, business_type, removed_places),
        "warnings": warnings,
        "matched_address": location["matched_address"],
        "lat": location["lat"],
        "lon": location["lon"],
        "census_tract": location["state"] + location["county"] + location["tract"],
        "business_type": business_type,
        **result,
        "demand_details": {
            "signals": demand_result["signals"],
            "people_per_sq_mile": demand_result["people_per_sq_mile"],
            "tract_median_income": tract["median_income"],
            "state_median_income": state["median_income"],
            "state_name": SUPPORTED_STATES.get(location["state"], "the state")
        },
        "competition_details": {
            "score_based_on": competition_result["source"],
            "competitors_removed_by_you": removed_competitors,
            "openstreetmap_competitors_in_radius": competition_result["competitor_count"],
            "census_competitors_in_zip": zip_count,
            "census_estimate_in_radius": competition_result["zip_estimate_in_radius"],
            "zip_code": location["zip_code"],
            "nearest_competitors": competition_result["nearest_competitors"]
        },
        "traffic_details": {
            "frontage_score": traffic_result["frontage_score"],
            "frontage_based_on": traffic_result["frontage_based_on"],
            "access_score": traffic_result["access_score"],
            "busiest_roads_nearby": access_roads[:3]
        },
        "land_use_details": {
            "complementary_places_nearby": complementary_places,
            "nearby_places_source": places_source
        }
    }
