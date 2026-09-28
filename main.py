from fastapi import FastAPI
from pydantic import BaseModel
from scoring import calculate_fit_score

app = FastAPI()


# This defines exactly what a request to our app must look like.
# If someone sends bad data (missing a field, wrong type), FastAPI
# will automatically reject it with a clear error before it ever
# reaches our code.
class ScoreRequest(BaseModel):
    address: str
    business_type: str


@app.get("/")
def read_root():
    return {"message": "Site Selector API is running"}


@app.post("/score")
def score_site(request: ScoreRequest):
    # For this deliverable, we're using mocked sub-scores instead of
    # live data pulls (Census, OSM, Places, DOT). That's the next
    # milestone. For now, every address gets the same test values so
    # we can prove the full input -> calculation -> output flow works.
    demand = 72
    competition = 55
    traffic = 80
    land_use = 65

    result = calculate_fit_score(demand, competition, traffic, land_use)

    return {
        "address": request.address,
        "business_type": request.business_type,
        **result
    }
