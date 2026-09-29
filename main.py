from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from scoring import (calculate_fit_score, calculate_demand_score, calculate_competition_score,
                     calculate_land_use_score, calculate_traffic_score, normalize_business_type,
                     DEMAND_WEIGHTS, ONE_MILE, COMPETITOR_NAICS, FRONTAGE_RADIUS, SETBACK_RADIUS,
                     ACCESS_RADIUS)
from pipeline import (geocode_address, get_tract_demographics, get_county_demographics, get_nearby_places,
                      get_zip_business_count, get_nearby_roads)

app = FastAPI()

STATIC_FOLDER = Path(__file__).parent / "static"

# Fewer mapped places than this within a mile triggers a data-quality warning
LOW_COVERAGE_PLACE_COUNT = 30


# This defines exactly what a request to our app must look like.
# If someone sends bad data (missing a field, wrong type), FastAPI
# will automatically reject it with a clear error before it ever
# reaches our code.
class ScoreRequest(BaseModel):
    address: str
    business_type: str


@app.get("/")
def home_page():
    # The web interface (a single page in the static folder)
    return FileResponse(STATIC_FOLDER / "index.html")


@app.get("/health")
def health_check():
    # Hosting services ping this to check the app is up
    return {"message": "Site Selector API is running"}


@app.post("/score")
def score_site(request: ScoreRequest):
    # Step 0: make sure we know how to score this kind of business
    business_type = normalize_business_type(request.business_type)
    if business_type is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported business type. Try one of: {', '.join(DEMAND_WEIGHTS)}"
        )

    # Step 1: turn the address into coordinates + census tract. Every live
    # data pull (Census, OSM, etc.) needs this, so it happens first.
    location = geocode_address(request.address)
    if location is None:
        raise HTTPException(status_code=422, detail="Could not match that address. Try including city and state.")

    # Step 2: pull live data - Census demographics for the tract vs. its
    # county, nearby places from OpenStreetMap, and the official count of
    # competitors in the ZIP code. We always search OSM a full mile so the
    # result can be reused; each score narrows it down itself.
    try:
        tract = get_tract_demographics(location["state"], location["county"], location["tract"])
        county = get_county_demographics(location["state"], location["county"])
        places = get_nearby_places(location["lat"], location["lon"], ONE_MILE)
        zip_count = None
        if location["zip_code"]:
            zip_count = get_zip_business_count(location["zip_code"], COMPETITOR_NAICS[business_type])
        frontage_roads = get_nearby_roads(location["lat"], location["lon"], FRONTAGE_RADIUS, location["state"])
        setback_roads = get_nearby_roads(location["lat"], location["lon"], SETBACK_RADIUS, location["state"])
        access_roads = get_nearby_roads(location["lat"], location["lon"], ACCESS_RADIUS, location["state"])
    except RuntimeError as error:  # e.g. missing CENSUS_API_KEY, or OSM too busy
        raise HTTPException(status_code=503, detail=str(error))
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="A Census data service didn't respond. Please try again in a minute.")

    # Step 3: turn the raw data into 0-100 sub-scores
    demand_result = calculate_demand_score(business_type, tract, county, location["tract_land_sq_miles"])
    competition_result = calculate_competition_score(
        business_type, places, zip_count, location["zip_land_sq_miles"]
    )
    land_use_result = calculate_land_use_score(business_type, places)
    site_street = location["matched_address"].split(",")[0]  # "200 E WASHINGTON ST"
    traffic_result = calculate_traffic_score(site_street, frontage_roads, setback_roads, access_roads)

    result = calculate_fit_score(
        demand_result["demand"], competition_result["competition"],
        traffic_result["traffic"], land_use_result["land_use"]
    )

    # OpenStreetMap coverage varies a lot by neighborhood. Very few mapped
    # places usually means missing data, not an empty area. Competition is
    # backed up by Census ZIP counts, but Land Use isn't yet - say so.
    warnings = []
    if len(places) < LOW_COVERAGE_PLACE_COUNT:
        warnings.append(
            f"Only {len(places)} places are mapped in OpenStreetMap within a mile of this address. "
            "Data is likely incomplete here, so Land Use is probably understated. "
            "(Competition is cross-checked against Census ZIP code counts.)"
        )

    return {
        "warnings": warnings,
        "address": request.address,
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
            "complementary_places_nearby": land_use_result["complementary_places_nearby"]
        }
    }
