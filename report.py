"""
Builds a printable PDF from saved searches (see static/saved.html).

One search -> that site's report. Several -> a comparison page first, then
each site's report. A saved search is the JSON the page stored when the
user pressed "Save this search": the site's score results and, optionally,
the heat map that was on screen.
"""
import io
import math
import os
import unicodedata
from datetime import datetime
from xml.sax.saxutils import escape

import reportlab
from reportlab.graphics.shapes import Circle, Drawing, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# Fonts that ship with reportlab, so the PDF looks the same on any server
_FONT_DIR = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
pdfmetrics.registerFont(TTFont("Vera", os.path.join(_FONT_DIR, "Vera.ttf")))
pdfmetrics.registerFont(TTFont("Vera-Bold", os.path.join(_FONT_DIR, "VeraBd.ttf")))
pdfmetrics.registerFontFamily("Vera", normal="Vera", bold="Vera-Bold", italic="Vera", boldItalic="Vera-Bold")

INK = colors.HexColor("#1d2330")
MUTED = colors.HexColor("#5d6677")
ACCENT = colors.HexColor("#2f5bd3")
RULE = colors.HexColor("#d5dae3")
HEAD_FILL = colors.HexColor("#eef2fb")
BEST_FILL = colors.HexColor("#e5f4e4")
STRONG = colors.HexColor("#1f8a4c")
MODERATE = colors.HexColor("#b7791f")
WEAK = colors.HexColor("#c53030")
COMPETITOR = colors.HexColor("#d03b3b")
HELPFUL = colors.HexColor("#0ca30c")
REMOVED = colors.HexColor("#8a8f98")
HEAT = [colors.HexColor(c) for c in ("#cde2fb", "#9ec5f4", "#5598e7", "#256abf", "#0d366b")]

WIDTH = letter[0] - 1.5 * inch

body = ParagraphStyle("body", fontName="Vera", fontSize=9.5, leading=13, textColor=INK, spaceAfter=4)
small = ParagraphStyle("small", parent=body, fontSize=8, leading=10.5, spaceAfter=0)
small_bold = ParagraphStyle("small_bold", parent=small, fontName="Vera-Bold")
muted = ParagraphStyle("muted", parent=body, fontSize=8.5, leading=11.5, textColor=MUTED)
title = ParagraphStyle("title", fontName="Vera-Bold", fontSize=19, leading=23, textColor=INK, spaceAfter=2)
h2 = ParagraphStyle("h2", fontName="Vera-Bold", fontSize=12, leading=15, textColor=ACCENT, spaceBefore=10, spaceAfter=4,
                    keepWithNext=True)

PARTS = [("demand", "Demand"), ("competition", "Competition"), ("traffic", "Traffic & access"),
         ("land_use", "Nearby businesses"), ("safety", "Safety")]

PLACE_LABELS = {
    "cafe": "cafés", "restaurant": "restaurants", "bar": "bars", "salon": "salons", "clothing": "clothing stores",
    "gym": "gyms", "doctor": "doctors' offices", "pharmacy": "pharmacies", "hospital": "hospitals",
    "other_medical": "medical offices", "college": "colleges", "school": "schools", "library": "libraries",
    "entertainment": "theaters / cinemas", "hotel": "hotels", "park": "parks", "office": "offices", "retail": "shops",
}


# ---------- small helpers ----------

def clean(text):
    """Safe for the PDF: no markup, and characters the font can't draw are simplified."""
    text = "" if text is None else str(text)
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        text = unicodedata.normalize("NFKD", text).encode("cp1252", "ignore").decode("cp1252")
    return escape(text)


def P(text, style=body):
    return Paragraph(text, style)


def num(value, digits=0):
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}"


def money(value):
    return "n/a" if value is None else f"${value:,.0f}"


def rating_color(score):
    if score is None:
        return MUTED
    return STRONG if score >= 75 else MODERATE if score >= 50 else WEAK


ROAD_WORDS = {"AV": "Ave", "AVE": "Ave", "ST": "St", "RD": "Rd", "BLVD": "Blvd", "DR": "Dr", "PKWY": "Pkwy",
              "HWY": "Hwy", "LN": "Ln", "CT": "Ct", "PL": "Pl", "CIR": "Cir", "PIKE": "Pike", "WAY": "Way"}
KEEP_UPPER = {"US", "SR", "N", "S", "E", "W", "NE", "NW", "SE", "SW"}


def road(name):
    """'KEYSTONE AV' -> 'Keystone Ave', '38TH ST' -> '38th St' (same as the website)."""
    words = []
    for w in (name or "").split():
        if w in ROAD_WORDS:
            words.append(ROAD_WORDS[w])
        elif w in KEEP_UPPER:
            words.append(w)
        elif w[0].isalpha():
            words.append(w[0] + w[1:].lower())
        else:
            words.append(w.lower())
    return " ".join(words)


def weights_text(result):
    w = result.get("weights") or {}
    names = {"demand": "Demand", "competition": "Competition", "traffic": "Traffic", "land_use": "Nearby businesses",
             "safety": "Safety"}
    parts = [f"{names[k]} {w[k]:g}%" for k in names if w.get(k)]
    return ("Custom weights: " if result.get("custom_weights") else "Charter weights: ") + ", ".join(parts)


def grid(header, rows, widths, bold_first_col=False, highlight=None):
    """A simple table. highlight: set of (row, col) cells to shade as 'best'."""
    data = [[P(f"<b>{h}</b>", small) for h in header]]
    for row in rows:
        data.append([cell if not isinstance(cell, str) else P(cell, small) for cell in row])
    t = Table(data, colWidths=[w * WIDTH for w in widths], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_FILL),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, ACCENT),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for r, c in (highlight or ()):
        style.append(("BACKGROUND", (c, r), (c, r), BEST_FILL))
    t.setStyle(TableStyle(style))
    return t


def score_bars(result):
    """The sub-score bars, drawn like the website's."""
    weights = result.get("weights") or {}
    breakdown = result.get("breakdown") or {}
    shown = [(k, label) for k, label in PARTS if k != "safety" or weights.get("safety")]
    row_h = 22
    d = Drawing(WIDTH, row_h * len(shown) + 4)
    label_w, bar_x, bar_w = 170, 175, WIDTH - 175 - 70
    for i, (key, label) in enumerate(shown):
        y = d.height - (i + 1) * row_h + 6
        value = breakdown.get(key)
        d.add(String(0, y + 3, f"{label} ({weights.get(key, 0):g}%)", fontName="Vera", fontSize=8.5, fillColor=INK))
        d.add(Rect(bar_x, y + 1, bar_w, 9, fillColor=colors.HexColor("#e9ecf1"), strokeColor=None, rx=4, ry=4))
        if value is not None:
            d.add(Rect(bar_x, y + 1, max(2, bar_w * min(100, value) / 100), 9, fillColor=rating_color(value),
                       strokeColor=None, rx=4, ry=4))
        d.add(String(WIDTH - 2, y + 3, num(value, 1) if value is not None else "n/a", fontName="Vera-Bold", fontSize=9,
                     fillColor=INK, textAnchor="end"))
    return d


# ---------- maps (drawn shapes; no street map underneath) ----------

def _projector(lat0, lon0, scale):
    k = math.cos(math.radians(lat0))
    return lambda lat, lon: ((lon - lon0) * k * scale, (lat - lat0) * scale)


def site_map(result, size=3.3 * inch):
    """The 1-mile circle around the site, with its competitor and helpful-business pins."""
    nb = result.get("nearby_businesses") or {}
    lat0, lon0 = result["lat"], result["lon"]
    mile_deg = 1609 / 110_574  # one mile in degrees of latitude
    scale = (size / 2 - 10) / mile_deg
    to_xy = _projector(lat0, lon0, scale)
    d = Drawing(size, size)
    cx, cy = size / 2, size / 2
    d.add(Rect(0, 0, size, size, fillColor=colors.HexColor("#f6f7f9"), strokeColor=RULE))
    d.add(Circle(cx, cy, mile_deg * scale, fillColor=colors.HexColor("#eef2fb"), strokeColor=ACCENT, strokeWidth=1.2))
    radius = nb.get("scoring_radius_miles") or 1
    if radius < 1:
        c = Circle(cx, cy, mile_deg * scale * radius, fillColor=None, strokeColor=ACCENT, strokeWidth=0.8)
        c.strokeDashArray = [3, 3]
        d.add(c)
    for places, color in ((nb.get("removed") or [], REMOVED), (nb.get("synergistic") or [], HELPFUL),
                          (nb.get("competitors") or [], COMPETITOR)):
        for p in places:
            x, y = to_xy(p["lat"], p["lon"])
            if (x * x + y * y) ** 0.5 <= mile_deg * scale + 4:
                d.add(Circle(cx + x, cy + y, 2.6, fillColor=color, strokeColor=colors.white, strokeWidth=0.5))
    # the site itself
    d.add(Polygon([cx, cy + 7, cx + 6, cy - 4, cx - 6, cy - 4], fillColor=ACCENT, strokeColor=colors.white, strokeWidth=0.8))
    d.add(String(6, 6, "Circle = 1 mile", fontName="Vera", fontSize=7, fillColor=MUTED))
    return d


def heat_map(heatmap, site, size=5.4 * inch):
    """Neighborhoods colored by score (five groups, ranked within the city), top 5 numbered."""
    feats = heatmap.get("features") or []
    points = [pt for f in feats for ring in _rings(f.get("geometry")) for pt in ring]
    if not points:
        return None
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    lat0, lon0 = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
    k = math.cos(math.radians(lat0))
    span = max((max(lons) - min(lons)) * k, max(lats) - min(lats)) or 0.01
    scale = (size - 20) / span
    to_xy = _projector(lat0, lon0, scale)
    d = Drawing(size, size)
    cx, cy = size / 2, size / 2
    d.add(Rect(0, 0, size, size, fillColor=colors.white, strokeColor=RULE))

    scores = sorted(f["fit_score"] for f in feats if f.get("fit_score") is not None)
    breaks = [scores[min(len(scores) - 1, int(q * len(scores)))] for q in (0.2, 0.4, 0.6, 0.8)] if scores else []
    top = {s["name"] for s in heatmap.get("suggestions") or []}
    for f in feats:
        score = f.get("fit_score")
        group = sum(score >= b for b in breaks) if score is not None else 0
        for ring in _rings(f.get("geometry")):
            pts = []
            for lon, lat in ring:
                x, y = to_xy(lat, lon)
                pts += [cx + x, cy + y]
            d.add(Polygon(pts, fillColor=HEAT[group], strokeColor=INK if f.get("name") in top else colors.white,
                          strokeWidth=1.2 if f.get("name") in top else 0.3))
    for s in heatmap.get("suggestions") or []:
        x, y = to_xy(s["lat"], s["lon"])
        d.add(Circle(cx + x, cy + y, 7, fillColor=INK, strokeColor=colors.white, strokeWidth=1))
        d.add(String(cx + x, cy + y - 3, str(s["rank"]), fontName="Vera-Bold", fontSize=7.5, fillColor=colors.white,
                     textAnchor="middle"))
    if site and site.get("lat") is not None:
        x, y = to_xy(site["lat"], site["lon"])
        if abs(x) < size / 2 and abs(y) < size / 2:
            d.add(Polygon([cx + x, cy + y + 8, cx + x + 7, cy + y - 5, cx + x - 7, cy + y - 5], fillColor=ACCENT,
                          strokeColor=colors.white, strokeWidth=1))
    return d, breaks, (scores[0] if scores else None), (scores[-1] if scores else None)


def _rings(geometry):
    if not geometry:
        return []
    if geometry.get("type") == "Polygon":
        return geometry["coordinates"]
    if geometry.get("type") == "MultiPolygon":
        return [ring for poly in geometry["coordinates"] for ring in poly]
    return []


def legend(entries):
    """A one-row key: [(color, label), ...]."""
    d = Drawing(WIDTH, 14)
    x = 0
    for color, label in entries:
        d.add(Rect(x, 3, 9, 8, fillColor=color, strokeColor=None))
        d.add(String(x + 13, 4, label, fontName="Vera", fontSize=7.5, fillColor=MUTED))
        x += 18 + pdfmetrics.stringWidth(label, "Vera", 7.5) + 12
    return d


# ---------- pages ----------

def comparison_page(searches):
    story = [P("Site comparison", title),
             P(f"{len(searches)} sites · created {datetime.now():%B %d, %Y}", muted), Spacer(1, 6)]
    rows, values = [], []
    for i, s in enumerate(searches, start=1):
        r = s["result"]
        b = r.get("breakdown") or {}
        rows.append([f"<b>{i}. {clean(s.get('name'))}</b><br/>{clean(s.get('business_label'))}",
                     f"<b>{num(r.get('fit_score'), 1)}</b><br/>{clean(r.get('tier'))}"]
                    + [num(b.get(k), 1) if b.get(k) is not None else "n/a" for k, _ in PARTS])
        values.append([r.get("fit_score")] + [b.get(k) for k, _ in PARTS])
    # shade the best value in each score column
    best = set()
    for col in range(len(values[0])):
        column = [v[col] for v in values]
        if any(x is not None for x in column):
            top = max(x for x in column if x is not None)
            best |= {(row + 1, col + 1) for row, x in enumerate(column) if x == top}
    story.append(grid(["Site", "Fit Score"] + [label for _, label in PARTS], rows,
                      [0.27, 0.12, 0.12, 0.13, 0.12, 0.13, 0.11], highlight=best))
    story.append(Spacer(1, 4))
    story.append(P("Shaded cells are the best score in each column. Higher is always better; for Competition, higher "
                   "means fewer competitors. Safety is city-wide crime and counts in the Fit Score only when its weight is above 0%.", muted))

    # bar chart of Fit Scores
    story.append(P("Fit Score by site", h2))
    row_h = 20
    d = Drawing(WIDTH, row_h * len(searches) + 6)
    bar_x, bar_w = 210, WIDTH - 210 - 40
    for i, s in enumerate(searches):
        y = d.height - (i + 1) * row_h + 4
        fit = s["result"].get("fit_score") or 0
        d.add(String(0, y + 3, f"{i + 1}. {clean(s.get('name'))[:38]}", fontName="Vera", fontSize=8.5, fillColor=INK))
        d.add(Rect(bar_x, y + 1, bar_w, 10, fillColor=colors.HexColor("#e9ecf1"), strokeColor=None, rx=4, ry=4))
        d.add(Rect(bar_x, y + 1, max(2, bar_w * min(100, fit) / 100), 10, fillColor=rating_color(fit), strokeColor=None, rx=4, ry=4))
        d.add(String(WIDTH - 2, y + 3, num(fit, 1), fontName="Vera-Bold", fontSize=9, fillColor=INK, textAnchor="end"))
    story.append(d)

    notes = []
    if len({s.get("business_type") for s in searches}) > 1:
        notes.append("These sites were scored for different business types, so their scores measure different things.")
    if len({weights_text(s["result"]) for s in searches}) > 1:
        notes.append("These sites were scored with different weights; compare sub-scores rather than Fit Scores.")
    if notes:
        story.append(Spacer(1, 6))
        story += [P("<b>Note:</b> " + n, body) for n in notes]
    story.append(Spacer(1, 6))
    story.append(P("Ratings: 75 or more is a Strong Fit, 50 to 74.9 Moderate, below 50 Weak. A score compares sites on "
                   "the measured factors; it does not predict success.", muted))
    return story


def site_pages(search, index=None):
    r = search["result"]
    story = []
    heading = (f"{index}. " if index else "") + clean(search.get("name"))
    story.append(P(heading, title))
    story.append(P(f"{clean(r.get('matched_address'))} · {clean(search.get('business_label'))} · saved "
                   f"{clean(search.get('saved_label'))}", muted))
    story.append(P(clean(weights_text(r)), muted))

    fit = r.get("fit_score")
    head = Table([[P(f"<font size='30' color='{rating_color(fit).hexval()}'><b>{num(fit, 1)}</b></font>", body),
                   P(f"<font size='14' color='{rating_color(fit).hexval()}'><b>{clean(r.get('tier'))}</b></font>"
                     f"<br/><font color='#5d6677'>Fit Score, 0 to 100</font>", body)]],
                  colWidths=[1.25 * inch, WIDTH - 1.25 * inch])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [head, Spacer(1, 4), score_bars(r)]

    for w in r.get("warnings") or []:
        story.append(P("<b>Note:</b> " + clean(w), muted))

    # details, side by side with the site map
    d = r.get("demand_details") or {}
    c = r.get("competition_details") or {}
    t = r.get("traffic_details") or {}
    lu = r.get("land_use_details") or {}
    cr = r.get("crime_details") or {}
    state = d.get("state_name") or "state"
    lines = []
    lines.append(P("<b>Who lives here</b>", small_bold))
    lines.append(P(f"{num(d.get('people_per_sq_mile'))} residents per square mile · median household income "
                   f"{money(d.get('tract_median_income'))} ({clean(state)}: {money(d.get('state_median_income'))})", small))
    lines.append(Spacer(1, 5))
    lines.append(P("<b>Competition</b>", small_bold))
    nearest = c.get("nearest_competitors") or []
    lines.append(P(f"{c.get('openstreetmap_competitors_in_radius', 0)} competitors mapped in the search area; Census count "
                   f"for ZIP {clean(c.get('zip_code'))}: {num(c.get('census_competitors_in_zip'))}"
                   + (f"; you removed {c['competitors_removed_by_you']}" if c.get("competitors_removed_by_you") else ""), small))
    if nearest:
        lines.append(P("Nearest: " + "; ".join(f"{clean(n['name'])} ({n['distance_miles']} mi)" for n in nearest[:5]), small))
    lines.append(Spacer(1, 5))
    lines.append(P("<b>Traffic</b>", small_bold))
    fb = t.get("frontage_based_on") or {}
    if fb.get("road"):
        lines.append(P(f"Street at site: {clean(road(fb['road']['road']))}, {num(fb['road']['vehicles_per_day'])} vehicles/day "
                       f"({clean(fb.get('reason'))})", small))
    busiest = t.get("busiest_roads_nearby") or []
    if busiest:
        lines.append(P("Nearby: " + "; ".join(f"{clean(road(x['road']))} {num(x['vehicles_per_day'])}/day" for x in busiest[:3]), small))
    lines.append(Spacer(1, 5))
    lines.append(P("<b>Nearby businesses that bring customers</b>", small_bold))
    counts = lu.get("complementary_places_nearby") or {}
    lines.append(P(", ".join(f"{v} {PLACE_LABELS.get(k, k)}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
                   or "Census ZIP code estimates (no map list for this spot)", small))
    if lu.get("helpful_removed_by_you"):
        lines.append(P(f"You removed {lu['helpful_removed_by_you']} from this score.", small))
    lines.append(Spacer(1, 5))
    lines.append(P("<b>Crime (city-wide)</b>", small_bold))
    if cr.get("available"):
        lines.append(P(f"{clean(cr.get('agency'))}, {cr.get('year')}: violent {num(cr.get('violent'))} per 100,000 "
                       f"({clean(state)} {num(cr.get('state_violent'))}); property {num(cr.get('property'))} "
                       f"({clean(state)} {num(cr.get('state_property'))}). Safety score {num(cr.get('safety_score'), 1)}.", small))
    else:
        lines.append(P("Not available. " + clean(cr.get("reason")), small))

    map_box = [site_map(r), Spacer(1, 3),
               legend([(COMPETITOR, "Competitor"), (HELPFUL, "Brings customers"), (REMOVED, "Removed"), (ACCENT, "Site")])]
    details = Table([[lines, map_box]], colWidths=[WIDTH - 3.45 * inch, 3.45 * inch])
    details.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                 ("RIGHTPADDING", (0, 0), (0, 0), 12)]))
    story += [P("Details", h2), details]

    hm = search.get("heatmap")
    if hm and hm.get("features"):
        drawn = heat_map(hm, r)
        if drawn:
            drawing, breaks, low, high = drawn
            story.append(PageBreak())
            story.append(P(f"Best areas for a {clean(search.get('business_label', '').lower())} in {clean(hm.get('city'))}", h2))
            story.append(P("Each neighborhood (census tract) is colored by its estimated Fit Score, ranked within the city: "
                           "darkest is the top 20%. Numbers mark the top 5 suggestions; the blue triangle is the scored site.", muted))
            story.append(drawing)
            edges = [low] + breaks + [high]
            story.append(legend([(HEAT[i], f"{num(edges[i])}-{num(edges[i + 1])}") for i in range(4, -1, -1)]))
            rows = [[str(s["rank"]), f"<b>{clean(s.get('label') or s.get('name'))}</b><br/>{clean(s.get('name'))}",
                     num(s.get("fit_score"), 1)] for s in hm.get("suggestions") or []]
            if rows:
                story.append(Spacer(1, 6))
                story.append(grid(["#", "Neighborhood", "Score"], rows, [0.06, 0.78, 0.16]))
            hc = hm.get("crime") or {}
            if hc.get("available"):
                story.append(P(f"City-wide crime ({clean(hc.get('agency'))}, {hc.get('year')}): violent {num(hc.get('violent'))} "
                               f"per 100,000, property {num(hc.get('property'))}.", muted))
            story.append(P("Neighborhood scores are estimates from Census data for the whole neighborhood and its ZIP code.", muted))
    return story


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(MUTED)
    canvas.setFont("Vera-Bold", 7.5)
    canvas.drawString(0.75 * inch, 0.5 * inch, "SiteScout")
    canvas.setFont("Vera", 7.5)
    canvas.drawString(0.75 * inch + 42, 0.5 * inch, "· A decision aid, not a revenue forecast.")
    canvas.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch, f"Page {doc.page}")
    canvas.setFont("Vera", 6.5)
    canvas.drawString(0.75 * inch, 0.36 * inch, "Data: U.S. Census Bureau, Federal Highway Administration, FBI Crime Data "
                      "Explorer, and OpenStreetMap contributors (via Geoapify).")
    canvas.restoreState()


def build_report(searches):
    """searches: list of saved-search dicts. Returns the PDF as bytes."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.7 * inch, bottomMargin=0.75 * inch, title="SiteScout report", author="SiteScout")
    story = []
    if len(searches) > 1:
        story += comparison_page(searches)
        for i, s in enumerate(searches, start=1):
            story.append(PageBreak())
            story += site_pages(s, i)
    else:
        story += site_pages(searches[0])
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()
