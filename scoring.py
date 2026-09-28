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
