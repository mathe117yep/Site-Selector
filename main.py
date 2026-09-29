from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from scoring import (calculate_fit_score, calculate_demand_score, calculate_competition_score,
                     calculate_land_use_score, calculate_traffic_score, calculate_area_land_use_score,
                     normalize_business_type, DEMAND_WEIGHTS, ONE_MILE, COMPETITOR_NAICS, AREA_NAICS,
                     FRONTAGE_RADIUS, SETBACK_RADIUS, ACCESS_RADIUS)
from pipeline import (geocode_address, locate_point, get_tract_demographics, get_county_demographics,
                      get_nearby_places, get_zip_business_count, get_zip_business_counts, get_nearby_roads,
                      INDIANA_CITIES)
from areas import score_city

app = FastAPI()

STATIC_FOLDER = Path(__file__).parent / "static"

# Fewer mapped places than this within a mile triggers a data-quality warning
LOW_COVERAGE_PLACE_COUNT = 30

# A map click this close to a counted road counts as being "on" that road
ON_ROAD_RADIUS = 40  # meters


# These define exactly what a request to our app must look like.
# If someone sends bad data (missing a field, wrong type), FastAPI
# will automatically reject it with a clear error before it ever
# reaches our code.
class ScoreRequest(BaseModel):
    address: str
    business_type: str


class PointScoreRequest(BaseModel):
    lat: float
    lon: float
    business_type: str


@app.get("/")
def home_page():
    # The web interface (a single page in the static folder). "no-cache"
    # makes browsers check for a newer version every time, so people see
    # updates right away instead of an old saved copy.
    return FileResponse(STATIC_FOLDER / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health_check():
    # Hosting services ping this to check the app is up
    return {"message": "Site Selector API is running"}


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

    # Turn the address into coordinates + census tract. Every live data
    # pull (Census, OSM, etc.) needs this, so it happens first.
    try:
        location = geocode_address(request.address)
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="The Census address service didn't respond. Please try again.")
    if location is None:
        raise HTTPException(status_code=422, detail="Could not match that address. Try including city and state.")

    result = score_location(location, business_type)
    return {"address": request.address, **result}


@app.post("/score_point")
def score_point(request: PointScoreRequest):
    """Scores a spot clicked on the map."""
    business_type = _business_type_or_error(request.business_type)
    try:
        location = locate_point(request.lat, request.lon)
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="The Census location service didn't respond. Please try again.")
    if location is None:
        raise HTTPException(status_code=422, detail="That spot isn't in a U.S. census tract. Try clicking on land.")
    return score_location(location, business_type)


@app.get("/cities")
def list_cities():
    """Cities offered in the map's "Find best areas" menu."""
    return sorted(INDIANA_CITIES)


@app.get("/area_scores")
def area_scores(city: str, business_type: str):
    """Scores every neighborhood in a city, for the heat map."""
    business_type = _business_type_or_error(business_type)
    if city not in INDIANA_CITIES:
        raise HTTPException(status_code=422, detail=f"Unknown city. Try one of: {', '.join(sorted(INDIANA_CITIES))}")
    try:
        return score_city(city, business_type)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error))
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="A Census or traffic data service didn't respond. Please try again in a minute.")


def score_location(location, business_type):
    """
    The full site score for one spot. `location` comes from geocode_address
    (typed address) or locate_point (map click).
    """
    warnings = []

    # Pull live data - Census demographics for the tract vs. its county,
    # the official count of competitors in the ZIP code, and traffic counts.
    try:
        tract = get_tract_demographics(location["state"], location["county"], location["tract"])
        county = get_county_demographics(location["state"], location["county"])
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

    # Nearby places from OpenStreetMap. Its free servers are often busy; if
    # so, fall back to Census ZIP code estimates instead of failing.
    try:
        places = get_nearby_places(location["lat"], location["lon"], ONE_MILE)
    except RuntimeError:
        places = None

    # Turn the raw data into 0-100 sub-scores
    demand_result = calculate_demand_score(business_type, tract, county, location["tract_land_sq_miles"])
    competition_result = calculate_competition_score(
        business_type, places or [], zip_count, location["zip_land_sq_miles"]
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
            "OpenStreetMap was too busy to answer, so Competition and Land Use are estimates "
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
        demand_result["demand"], competition_result["competition"], traffic_result["traffic"], land_use
    )

    return {
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
            "county_median_income": county["median_income"]
        },
        "competition_details": {
            "score_based_on": competition_result["source"],
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
            "complementary_places_nearby": complementary_places
        }
    }
