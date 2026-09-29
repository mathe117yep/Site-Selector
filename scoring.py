import math


# --- Demand sub-score ---
#
# Demand is built from four neighborhood signals, each scaled to 0-100:
#   density  - how many people live nearby (people per square mile)
#   income   - tract median income compared to the county's
#   young    - share of residents aged 20-39, compared to the county's
#   seniors  - share of residents aged 65+, compared to the county's
#
# Each business type weights those signals differently. These weights are
# starting assumptions, not research-backed numbers - tune them as you learn.
DEMAND_WEIGHTS = {
    "coffee_shop":      {"density": 0.40, "income": 0.30, "young": 0.30, "seniors": 0.00},
    "restaurant":       {"density": 0.50, "income": 0.30, "young": 0.20, "seniors": 0.00},
    "salon":            {"density": 0.45, "income": 0.40, "young": 0.15, "seniors": 0.00},
    "boutique":         {"density": 0.30, "income": 0.55, "young": 0.15, "seniors": 0.00},
    "gym":              {"density": 0.40, "income": 0.20, "young": 0.40, "seniors": 0.00},
    "personal_trainer": {"density": 0.20, "income": 0.60, "young": 0.20, "seniors": 0.00},
    "doctor":           {"density": 0.40, "income": 0.20, "young": 0.00, "seniors": 0.40},
}

# Lets users type business types naturally ("Coffee Shop", "hair salon", ...)
BUSINESS_TYPE_ALIASES = {
    "coffee shop": "coffee_shop", "coffee": "coffee_shop", "cafe": "coffee_shop",
    "restaurant": "restaurant",
    "salon": "salon", "hair salon": "salon", "nail salon": "salon", "barber": "salon", "barbershop": "salon",
    "boutique": "boutique", "clothing store": "boutique",
    "gym": "gym", "fitness": "gym", "fitness center": "gym",
    "personal trainer": "personal_trainer", "trainer": "personal_trainer",
    "doctor": "doctor", "medical": "doctor", "clinic": "doctor", "small practice doctor": "doctor",
}

# A tract with this many people per square mile (or more) gets a full 100
# for density. For reference, Indianapolis as a whole is around 2,300.
FULL_DENSITY_PER_SQ_MILE = 6000


def normalize_business_type(business_type):
    """Turns user input into one of our DEMAND_WEIGHTS keys, or None if unsupported."""
    cleaned = business_type.lower().strip().replace("-", " ").replace("_", " ")
    return BUSINESS_TYPE_ALIASES.get(cleaned)


def _clamp(value):
    return max(0.0, min(100.0, value))


def _compare_to_county(tract_value, county_value):
    """
    Scores a tract relative to its county: same as the county = 50,
    half the county or less = 0, 1.5x the county or more = 100.
    Missing data scores a neutral 50.
    """
    if tract_value is None or not county_value:
        return 50.0
    ratio = tract_value / county_value
    return _clamp((ratio - 0.5) * 100)


def calculate_demand_score(business_type, tract, county, tract_land_sq_miles):
    """
    business_type: a DEMAND_WEIGHTS key (use normalize_business_type first)
    tract, county: demographics dicts from pipeline.get_tract_demographics /
                   get_county_demographics
    """
    density = tract["population"] / tract_land_sq_miles if tract_land_sq_miles else 0

    signals = {
        "density": _clamp(density / FULL_DENSITY_PER_SQ_MILE * 100),
        "income": _compare_to_county(tract["median_income"], county["median_income"]),
        "young": _compare_to_county(tract["share_age_20_39"], county["share_age_20_39"]),
        "seniors": _compare_to_county(tract["share_age_65_plus"], county["share_age_65_plus"]),
    }

    weights = DEMAND_WEIGHTS[business_type]
    score = sum(weights[name] * signals[name] for name in weights)

    return {
        "demand": round(score, 1),
        "signals": {name: round(value, 1) for name, value in signals.items()},
        "people_per_sq_mile": round(density)
    }


# --- Competition and Land Use sub-scores ---
#
# Both come from the same list of nearby places (see pipeline.get_nearby_places).
# Places closer to the site count more: a place right next door counts fully,
# and its weight fades to zero at the edge of the search radius.
#
# Like the Demand weights, every number below is a starting assumption to tune.

HALF_MILE = 805   # meters
ONE_MILE = 1609   # meters

# How far customers typically travel. Walk-in businesses draw from a small
# area; destination businesses draw from farther away.
SEARCH_RADIUS = {
    "coffee_shop": HALF_MILE,
    "salon": HALF_MILE,
    "boutique": HALF_MILE,
    "restaurant": ONE_MILE,
    "gym": ONE_MILE,
    "personal_trainer": ONE_MILE,
    "doctor": ONE_MILE,
}

# Which nearby place categories compete with each business type (chains included)
COMPETITOR_CATEGORIES = {
    "coffee_shop": ["cafe"],
    "restaurant": ["restaurant"],
    "salon": ["salon"],
    "boutique": ["clothing"],
    "gym": ["gym"],
    "personal_trainer": ["gym"],
    "doctor": ["doctor"],
}

# The same competitors, as official industry codes (NAICS) for the Census
# ZIP code business counts. Note: "snack and nonalcoholic beverage bars"
# also includes donut, ice cream and smoothie shops, and gyms and personal
# trainers are filed under the same code.
COMPETITOR_NAICS = {
    "coffee_shop": ("722515",),                    # snack & nonalcoholic beverage bars
    "restaurant": ("722511", "722513"),            # full-service, limited-service
    "salon": ("812111", "812112", "812113"),       # barber shops, beauty salons, nail salons
    "boutique": ("448120", "448140", "448150", "448190", "448210", "448310"),  # clothing, shoes, jewelry
    "gym": ("713940",),                            # fitness & recreational sports centers
    "personal_trainer": ("713940",),
    "doctor": ("621111",),                         # offices of physicians
}

# How much competition knocks the score down to 50. For example, coffee
# shops hit 50 with about 3 cafes right next door (or more spread farther
# out). Restaurants cluster naturally, so they tolerate more neighbors.
COMPETITION_HALF_POINT = {
    "coffee_shop": 3,
    "restaurant": 10,
    "salon": 4,
    "boutique": 4,
    "gym": 3,
    "personal_trainer": 4,
    "doctor": 5,
}

# Nearby places that bring in customers, and how much each one helps.
COMPLEMENT_WEIGHTS = {
    "coffee_shop": {"office": 1, "college": 2, "school": 0.5, "library": 1, "gym": 1,
                    "hotel": 1, "retail": 0.5, "clothing": 0.5},
    "restaurant": {"office": 1, "entertainment": 2, "hotel": 2, "bar": 1, "college": 1,
                   "retail": 0.5, "clothing": 0.5},
    "salon": {"retail": 1, "clothing": 1, "cafe": 0.5, "gym": 0.5},
    "boutique": {"cafe": 1, "restaurant": 1, "salon": 1, "retail": 0.5, "hotel": 0.5},
    "gym": {"office": 1, "college": 1, "park": 0.5, "cafe": 0.5, "retail": 0.5},
    "personal_trainer": {"park": 2, "office": 1, "retail": 0.5},
    "doctor": {"pharmacy": 3, "hospital": 3, "other_medical": 1, "office": 0.5},
}

# Weighted complement total that earns a Land Use score of 50
LAND_USE_HALF_POINT = 20


def _closeness(distance_meters, radius_meters):
    """1.0 right at the site, fading to 0.0 at the edge of the radius."""
    return max(0.0, 1 - distance_meters / radius_meters)


def estimate_competitors_from_zip(business_type, zip_count, zip_land_sq_miles):
    """
    Estimates how many of a ZIP code's competitors fall inside our search
    circle, assuming they're spread evenly across the ZIP. Rough, but it
    works everywhere - which OpenStreetMap doesn't.
    """
    if zip_count is None or not zip_land_sq_miles:
        return None
    radius_miles = SEARCH_RADIUS[business_type] / ONE_MILE
    circle_sq_miles = math.pi * radius_miles ** 2
    share_of_zip = min(1.0, circle_sq_miles / zip_land_sq_miles)
    return zip_count * share_of_zip


def calculate_competition_score(business_type, places, zip_count=None, zip_land_sq_miles=None):
    """
    Scores competition from OpenStreetMap's exact locations, cross-checked
    against the Census ZIP code count. Whichever source shows MORE
    competition wins, because the usual error is missing businesses
    (OpenStreetMap gaps), not extra ones.
    """
    radius = SEARCH_RADIUS[business_type]
    competitors = [p for p in places
                   if p["category"] in COMPETITOR_CATEGORIES[business_type]
                   and p["distance_meters"] <= radius]
    competitors.sort(key=lambda p: p["distance_meters"])

    osm_pressure = sum(_closeness(p["distance_meters"], radius) for p in competitors)

    # We don't know where the ZIP's businesses are, just how many. Spread
    # evenly over a circle, their average closeness works out to 1/3.
    zip_estimate = estimate_competitors_from_zip(business_type, zip_count, zip_land_sq_miles)
    zip_pressure = zip_estimate / 3 if zip_estimate is not None else 0

    if zip_pressure > osm_pressure:
        pressure, source = zip_pressure, "Census ZIP code estimate"
    else:
        pressure, source = osm_pressure, "OpenStreetMap"

    # No competitors = 100, half point = 50, and it keeps falling from there
    score = 100 / (1 + pressure / COMPETITION_HALF_POINT[business_type])

    return {
        "competition": round(score, 1),
        "source": source,
        "competitor_count": len(competitors),
        "zip_estimate_in_radius": round(zip_estimate, 1) if zip_estimate is not None else None,
        "nearest_competitors": [
            {"name": p["name"], "distance_miles": round(p["distance_meters"] / ONE_MILE, 2)}
            for p in competitors[:5]
        ]
    }


def calculate_land_use_score(business_type, places):
    radius = SEARCH_RADIUS[business_type]
    weights = COMPLEMENT_WEIGHTS[business_type]

    total = 0.0
    counts = {}
    for place in places:
        if place["category"] in weights and place["distance_meters"] <= radius:
            total += weights[place["category"]] * _closeness(place["distance_meters"], radius)
            counts[place["category"]] = counts.get(place["category"], 0) + 1

    # Nothing nearby = 0, half point = 50, approaching 100 as it gets busier
    score = 100 * total / (total + LAND_USE_HALF_POINT)

    return {
        "land_use": round(score, 1),
        "complementary_places_nearby": counts
    }


# --- Traffic/Access sub-score ---
#
# Two things matter: how busy the street the site sits on is (drive-by
# visibility), and how busy the main roads around it are (easy to reach).
# Freeways are already filtered out in pipeline.get_nearby_roads.

FRONTAGE_RADIUS = 150   # meters - about a block: "the street the site is on"
SETBACK_RADIUS = 300    # meters - about two blocks: set back or around the corner
ACCESS_RADIUS = 400     # meters - "main roads a few blocks away"

# How much drive-by credit a road gets, depending on how the site relates to it
ON_SITE_STREET_CREDIT = 1.0    # the business's own street
CROSS_STREET_CREDIT = 0.75     # a different road within a block (corner / near intersection)
SETBACK_CREDIT = 0.4           # busiest road within two blocks (e.g. mall ring road off a main road)

STREET_SUFFIXES = {"ST", "STREET", "AVE", "AV", "AVENUE", "RD", "ROAD", "BLVD", "DR", "DRIVE", "LN",
                   "CT", "PL", "WAY", "PKWY", "HWY", "CIR", "TER", "TRL", "PIKE"}
DIRECTIONS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}

# Vehicles per day that score 0 and 100. Between them the score rises on a
# log scale, so going from 1,000 to 5,000 cars matters about as much as
# going from 6,000 to 30,000 (the first bump is the one that matters most).
QUIET_STREET_VEHICLES = 1_000
BUSY_STREET_VEHICLES = 30_000

FRONTAGE_WEIGHT = 0.7
ACCESS_WEIGHT = 0.3


def _vehicles_to_score(vehicles_per_day):
    if vehicles_per_day <= QUIET_STREET_VEHICLES:
        return 0.0
    position = (math.log10(vehicles_per_day) - math.log10(QUIET_STREET_VEHICLES)) / (
        math.log10(BUSY_STREET_VEHICLES) - math.log10(QUIET_STREET_VEHICLES)
    )
    return _clamp(position * 100)


def street_core_name(street):
    """
    Boils a street name down to its core so different spellings match:
    "200 E WASHINGTON ST" and "WASHINGTON ST" both become "WASHINGTON".
    """
    words = street.upper().replace(".", "").split()
    words = [w for w in words if not w.isdigit() and w not in DIRECTIONS and w not in STREET_SUFFIXES]
    return " ".join(words)


def calculate_traffic_score(site_street, frontage_roads, setback_roads, access_roads):
    """
    site_street: the street part of the matched address, e.g. "200 E WASHINGTON ST"
    frontage_roads / setback_roads / access_roads: lists from
        pipeline.get_nearby_roads for FRONTAGE_RADIUS, SETBACK_RADIUS and
        ACCESS_RADIUS, busiest first.

    Frontage credit goes to the best of: the site's own street (full
    credit), another road within a block (partial), or the busiest road
    within two blocks (less). Quiet local streets usually have no traffic
    count at all, so a site with nothing counted nearby scores 0.
    """
    site_core = street_core_name(site_street)
    options = []  # (score, explanation)
    for road in frontage_roads:
        on_site = street_core_name(road["road"]) == site_core
        credit = ON_SITE_STREET_CREDIT if on_site else CROSS_STREET_CREDIT
        label = "site's own street" if on_site else "cross street within a block"
        options.append((credit * _vehicles_to_score(road["vehicles_per_day"]), label, road))
    if setback_roads:
        road = setback_roads[0]
        options.append((SETBACK_CREDIT * _vehicles_to_score(road["vehicles_per_day"]),
                        "busy road within two blocks", road))

    if options:
        frontage_score, frontage_reason, frontage_road = max(options, key=lambda o: o[0])
    else:
        frontage_score, frontage_reason, frontage_road = 0.0, "no counted road within two blocks", None

    access_vehicles = access_roads[0]["vehicles_per_day"] if access_roads else 0
    access_score = _vehicles_to_score(access_vehicles)
    score = FRONTAGE_WEIGHT * frontage_score + ACCESS_WEIGHT * access_score

    return {
        "traffic": round(score, 1),
        "frontage_score": round(frontage_score, 1),
        "frontage_based_on": {"reason": frontage_reason, "road": frontage_road},
        "access_score": round(access_score, 1),
    }


# --- Area (neighborhood) scoring for the map's heat map ---
#
# Scoring a whole city address-by-address would take far too long, so the
# heat map scores each census tract ("neighborhood") from data we can pull
# for the whole city at once:
#   Demand       - the tract's own Census demographics (same as a site score)
#   Competition  - Census business counts for the ZIP the tract sits in
#   Land Use     - the same ZIP counts, for businesses that bring customers
#   Traffic      - the busiest regular road near the tract's center
# ZIP counts are complete everywhere (unlike OpenStreetMap), but they're
# spread evenly over the ZIP, so they can't tell one block from the next.
# That's fine for "which neighborhoods to look at"; clicking a spot on the
# map then gives the detailed site score.

AREA_TRAFFIC_RADIUS = 800  # meters, about half a mile from the tract's center

# Industry codes for each "helpful neighbor" category in COMPLEMENT_WEIGHTS.
# "retail" is special: all stores (44-45) minus clothing and pharmacies,
# which are counted separately.
CATEGORY_NAICS = {
    "office": ("52", "54", "55"),              # finance, professional services, company offices
    "college": ("611310",),
    "school": ("611110",),
    "library": ("519120",),
    "gym": ("713940",),
    "hotel": ("721110",),
    "clothing": ("448",),
    "entertainment": ("512131", "7111"),       # movie theaters, performing arts
    "bar": ("722410",),
    "cafe": ("722515",),
    "restaurant": ("722511", "722513"),
    "salon": ("812111", "812112", "812113"),
    "pharmacy": ("446110",),
    "hospital": ("622",),
    "other_medical": ("6212", "6213", "6214"),  # dentists, therapists, outpatient centers
    "park": (),                                 # parks aren't businesses, so no count here
}
ALL_RETAIL = "44-45"

# Every industry code the area scores need, for one Census request
AREA_NAICS = tuple(sorted(
    {code for codes in CATEGORY_NAICS.values() for code in codes}
    | {code for codes in COMPETITOR_NAICS.values() for code in codes}
    | {ALL_RETAIL}
))


def _zip_category_count(category, zip_counts):
    if category == "retail":
        other_stores = sum(zip_counts.get(code, 0) for code in CATEGORY_NAICS["clothing"] + CATEGORY_NAICS["pharmacy"])
        return max(0, zip_counts.get(ALL_RETAIL, 0) - other_stores)
    return sum(zip_counts.get(code, 0) for code in CATEGORY_NAICS[category])


def calculate_area_land_use_score(business_type, zip_counts, zip_land_sq_miles):
    """Land Use from ZIP counts: like calculate_land_use_score, but estimated."""
    total = 0.0
    for category, weight in COMPLEMENT_WEIGHTS[business_type].items():
        estimate = estimate_competitors_from_zip(
            business_type, _zip_category_count(category, zip_counts), zip_land_sq_miles
        ) or 0
        # Spread evenly over the search circle, average closeness is 1/3
        total += weight * estimate / 3
    return round(100 * total / (total + LAND_USE_HALF_POINT), 1)


def calculate_area_traffic_score(roads):
    """roads: pipeline.get_nearby_roads result for AREA_TRAFFIC_RADIUS, busiest first."""
    return round(_vehicles_to_score(roads[0]["vehicles_per_day"]) if roads else 0.0, 1)


def calculate_area_scores(business_type, tract, county, tract_land_sq_miles,
                          zip_counts, zip_land_sq_miles, roads):
    """
    All four sub-scores plus the overall score for one neighborhood.
    zip_counts is None if we couldn't tell which ZIP the tract is in; then
    Competition and Land Use fall back to a neutral 50.
    """
    demand = calculate_demand_score(business_type, tract, county, tract_land_sq_miles)["demand"]

    if zip_counts is None:
        competition, land_use = 50.0, 50.0
    else:
        competitor_count = sum(zip_counts.get(code, 0) for code in COMPETITOR_NAICS[business_type])
        competition = calculate_competition_score(
            business_type, [], competitor_count, zip_land_sq_miles
        )["competition"]
        land_use = calculate_area_land_use_score(business_type, zip_counts, zip_land_sq_miles)

    traffic = calculate_area_traffic_score(roads)
    return calculate_fit_score(demand, competition, traffic, land_use)


def calculate_fit_score(demand, competition, traffic, land_use):
    """
    Calculates the composite Fit Score for a candidate site, using the
    weighted formula from the project Charter:

        Fit Score = (0.30 * Demand) + (0.25 * Competition)
                  + (0.25 * Traffic/Access) + (0.20 * Complementary Land Use)

    Each input should be a 0-100 sub-score.
    """
    fit_score = (0.30 * demand) + (0.25 * competition) + (0.25 * traffic) + (0.20 * land_use)

    if fit_score >= 75:
        tier = "Strong Fit"
    elif fit_score >= 50:
        tier = "Moderate Fit"
    else:
        tier = "Weak Fit"

    return {
        "fit_score": round(fit_score, 1),
        "tier": tier,
        "breakdown": {
            "demand": demand,
            "competition": competition,
            "traffic": traffic,
            "land_use": land_use
        }
    }


if __name__ == "__main__":
    # Test it with made-up numbers for a sample address
    result = calculate_fit_score(demand=72, competition=55, traffic=80, land_use=65)
    print(result)
