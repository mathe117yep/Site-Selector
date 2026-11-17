"""
Zoning check: what a site is zoned, and whether that zoning generally
allows the chosen business type.

There's no single zoning map for the Midwest - every city keeps its own,
in its own format - so this covers the cities whose official zoning maps
can be looked up live (see SOURCES below; 21 areas in 7 states).
Everywhere else, the result says zoning isn't available yet.

How a district is judged: each city's district is sorted into a general
kind (business, mixed-use/downtown, office, residential, industrial, ...),
from the city's own district description where it has one, or from
rules for its district codes. The answer is based on that general
purpose, not the city's full table of permitted uses, so it's a first
screen - not a substitute for asking the planning office. Zoning is
shown next to the score; it doesn't change the Fit Score.
"""
import math
import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import requests

# Census address points sit on the street's centerline, and some zoning maps
# leave streets out. If the exact point isn't inside a district, use the
# nearest district within this distance (the lots along the street).
NEARBY_METERS = 60

# What each kind of district means for each business type:
#   "allowed"     - the district is meant for this kind of business
#   "check"       - possible, but often needs special approval or follows custom rules
#   "not_allowed" - the district generally doesn't allow it (a rezoning or variance would be needed)
ALL_TYPES = ("coffee_shop", "restaurant", "salon", "boutique", "gym", "personal_trainer", "doctor")
KIND_RULES = {
    "commercial": {t: "allowed" for t in ALL_TYPES},
    "mixed": {t: "allowed" for t in ALL_TYPES},
    "office": {**{t: "check" for t in ALL_TYPES}, "doctor": "allowed", "personal_trainer": "allowed"},
    "medical": {**{t: "check" for t in ALL_TYPES}, "doctor": "allowed"},
    "industrial": {**{t: "not_allowed" for t in ALL_TYPES}, "gym": "check", "personal_trainer": "check"},
    "planned": {t: "check" for t in ALL_TYPES},
    "special": {t: "check" for t in ALL_TYPES},
    "residential": {t: "not_allowed" for t in ALL_TYPES},
    "park": {t: "not_allowed" for t in ALL_TYPES},
}

KIND_LABELS = {
    "commercial": "business/commercial", "mixed": "mixed-use or downtown", "office": "office",
    "medical": "hospital/medical", "industrial": "industrial", "planned": "planned development",
    "special": "special-purpose", "residential": "residential", "park": "park",
}

BUSINESS_NAMES = {
    "coffee_shop": "coffee shop", "restaurant": "restaurant", "salon": "salon", "boutique": "boutique",
    "gym": "gym", "personal_trainer": "personal training studio", "doctor": "doctor's office",
}

NONE = None  # a "district" that isn't zoning (water, another town's jurisdiction, data errors)


# --- Sorting a district into a kind ---

def kind_from_text(text):
    """
    The kind of district, from a description like "General Business" or
    "Single-Family Residential". Returns None if the text says it isn't a
    zoning district at all.
    """
    t = (text or "").lower()
    if re.search(r"jurisdiction|^water$|unknown|problem has been identified|territorial", t):
        return NONE
    if re.search(r"manufactured|mobile home", t):
        return "residential"  # manufactured-home parks are housing, not factories or parks
    if "research park" in t:
        return "office"
    if "planned" in t or re.search(r"\bpud\b", t):
        return "planned"
    if "parking" in t:
        return "special"
    if re.search(r"hospital|healthcare|medical", t):
        return "medical"
    if re.search(r"downtown|central business|mixed|town center|village center|main street|urban core|"
                 r"neighborhood center|city center", t):
        return "special" if re.search(r"\bresidential\b|student housing", t) else "mixed"
    if re.search(r"industr|manufactur|warehous|quarry|excavation|production", t):
        return "industrial"
    if "office" in t:
        return "office"
    if re.search(r"commercial|business|retail|shopping", t):
        return "commercial"
    if re.search(r"\bpark|open space|recreation|flood", t):
        return "park"
    if re.search(r"residen|family|dwelling|estate|home|agricult|rural|neighborhood", t):
        return "residential"
    return "special"


def _tidy(text):
    """'GENERAL  BUSINESS ' -> 'General business'; leaves mixed-case text alone."""
    text = " ".join(str(text or "").split()).strip(" '")
    if text.lower() in ("", "none", "null"):
        return ""
    return text.capitalize() if text.isupper() else text


# Rules for district codes: (regex matched against the code, kind, description).
# A description of None means "use the city's own description". Checked in
# order before the keyword sorting above.
RULES = {
    "Indianapolis": [
        (r"CBD", "mixed", "Central Business District (downtown)"),
        (r"CS", "special", "Special Commercial (custom rules for this site)"),
        (r"C-?1", "office", "Office-Buffer Commercial"),
        (r"C-?3", "commercial", "Neighborhood Commercial"),
        (r"C-?4", "commercial", "Community-Regional Commercial"),
        (r"C-?5", "commercial", "General Commercial"),
        (r"C-?7", "commercial", "High-Intensity Commercial"),
        (r"MU", "mixed", "Mixed-Use"),
        (r"UQ", "mixed", "University Quarter"),
        (r"HD", "medical", "Hospital District"),
        (r"I", "industrial", "Industrial"),
        (r"DA", "residential", "Dwelling Agriculture (rural residential)"),
        (r"DP", "planned", "Planned Unit Development"),
        (r"D", "residential", "Dwelling (residential)"),
        (r"PK", "park", "Park"),
        (r"SU", "special", "Special Use (churches, schools, public uses)"),
        (r"SZ", "special", "Special Zoning District"),
        (r"HP", "special", "Historic Preservation District"),
    ],
    "Carmel": [
        (r"B-5$", "office", None), (r"MC$", "office", "Meridian Corridor (offices)"),
        (r"(C-1|C-2|UC)$", "mixed", None), (r"I-1$", "special", None), (r"R-5$", "special", None),
        (r"UR$", "residential", "Urban Residential"), (r"PUD$", "planned", None), (r"P-1$", "park", None),
        (r"B-", "commercial", None), (r"M-", "industrial", None), (r"(R|S)-", "residential", None),
    ],
    "Chicago": [
        (r"DC", "mixed", "Downtown Core"), (r"DX", "mixed", "Downtown Mixed-Use"),
        (r"DS", "commercial", "Downtown Service"), (r"DR", "special", "Downtown Residential (limited business)"),
        (r"B", "commercial", "Business (neighborhood shopping)"), (r"C", "commercial", "Commercial"),
        (r"PMD", "industrial", "Planned Manufacturing District"),
        (r"PD", "planned", "Planned Development (custom rules for this site)"),
        (r"M", "industrial", "Manufacturing"), (r"POS", "park", "Parks and Open Space"),
        (r"RS", "residential", "Residential Single-Unit"), (r"RT", "residential", "Residential Two-Flat and Townhouse"),
        (r"RM", "residential", "Residential Multi-Unit"), (r"T", "special", "Transportation"),
    ],
    "Fishers": [  # the map lists codes only
        (r"PUD", "planned", "Planned Unit Development"), (r"AG", "residential", "Agricultural"),
        (r"(DC|TC|VC)$", "mixed", "Downtown / town or village center"), (r"TCR", "special", "Town Center Residential"),
        (r"HC", "commercial", "Highway Commercial"), (r"C\d", "commercial", "Commercial"),
        (r"(I|M)\d", "industrial", "Industrial"), (r"R\d", "residential", "Residential"),
    ],
    "Noblesville": [  # codes only; overlays like "/GUO" are removed first
        (r"(DT|DMU)$", "mixed", "Downtown / downtown mixed-use"), (r"(GB|LB)$", "commercial", None),
        (r"B-PD|PB$", "commercial", "Planned Business"), (r"I-", "industrial", "Industrial"),
        (r"(R\d|R/|RMH)", "residential", "Residential"), (r"WATER", NONE, None),
        (r".*PD$", "planned", "Planned Development"),
    ],
    "Tippecanoe County": [  # Lafayette, West Lafayette and the county (Unified Zoning Ordinance)
        (r"(OTTERBEIN|SHADELAND)$", NONE, None),
        (r"CBW?$", "mixed", "Central Business"), (r"(GB|HB)$", "commercial", "General / Highway Business"),
        (r"NBU?$", "commercial", "Neighborhood Business"), (r"MRU?$", "medical", "Medical Related"),
        (r"OR$", "office", "Office Research"), (r"PDMX$", "planned", "Planned Development, Mixed Use"),
        (r"PD", "planned", "Planned Development"), (r"I\d", "industrial", "Industrial"),
        (r"FP$", "park", "Flood Plain"), (r"(A|AA|AW)$", "residential", "Agricultural"),
        (r"(R|RE)", "residential", "Residential"),
    ],
    "Fort Wayne / Allen County": [
        (r"C1$", "office", None), (r"(DC|DE|UC)$", "mixed", None), (r"BTI$", "industrial", None),
        (r"C\d|SC|NC", "commercial", None),
    ],
    "Bloomington": [
        (r"MS$", "special", None), (r"ME$", "special", None), (r"MI$", "special", None),
    ],
    "South Bend": [
        (r"UF$", "special", "Urban Neighborhood Flex"), (r"U$", "special", "University"),
    ],
    "Evansville / Vanderburgh County": [  # codes only
        (r"(UNK|OTH)$", NONE, None), (r"AIR$", "special", "Airport"), (r"A$", "residential", "Agricultural"),
        (r"CO-", "office", "Commercial-Office"), (r"C-", "commercial", "Commercial"),
        (r"M-", "industrial", "Industrial"), (r"PUD", "planned", "Planned Unit Development"),
        (r"W-I", "industrial", "Waterfront Industrial"), (r"W-R", "residential", "Waterfront Residential"),
        (r"R-", "residential", "Residential"),
    ],
    "Columbus": [
        (r"(NE|NG)$", "special", None), (r"L?I$", "special", "Institutional"),
        (r"L?UCRPD", "office", "Research Park (offices)"),
    ],
    "Detroit": [
        (r"SD5", "special", None), (r"PC$", "special", None), (r"TM$|W1$|MKT$", "industrial", None),
    ],
    "Westfield": [
        (r"M-GP", "special", None),
    ],
    "Milwaukee": [
        (r"X$", NONE, None), (r"RO", "office", "Residential and Office"),
        (r"C9H", "industrial", None), (r"IC$", "commercial", None), (r"IM$", "industrial", None),
    ],
    "Minneapolis": [
        (r"PR1", "special", None), (r"RM1|RM3|TR1", "special", None), (r"RM2", "office", None),
        (r"UN", "residential", None),
    ],
    "Saint Paul": [
        (r"B[45]$", "mixed", None), (r"CA$", "special", None), (r"F[1-4]$", "special", None),
        (r"F[56]$", "mixed", None), (r"T1", "special", None), (r"T[234]", "mixed", None),
        (r"IT", "special", None), (r"VP", "special", None),
    ],
    "Kansas City": [
        (r"(DC|DX)", "mixed", "Downtown"), (r"DR", "special", "Downtown Residential"),
        (r"KCIA", "special", "Airport"), (r"(MPD|UR)", "planned", "Planned district"),
        (r"O", "office", "Office"), (r"(B\d|SC)", "commercial", "Business / commercial"),
        (r"M\d", "industrial", "Manufacturing"), (r"(R|AG)", "residential", "Residential / agricultural"),
    ],
    "St. Louis": [
        (r"[ABCDE]$", "residential", "Residential (single-, two- or multi-family dwelling)"),
        (r"F$", "commercial", "Neighborhood Commercial"), (r"G$", "commercial", "Local Commercial and Office"),
        (r"H$", "commercial", "Area Commercial"), (r"I$", "mixed", "Central Business"),
        (r"J$", "industrial", "Industrial"), (r"K$", "special", "Unrestricted"),
        (r"L$", "special", "Jefferson Memorial"), (r".*", NONE, None),
    ],
    "Cincinnati": [  # codes only; overlays like "-T" or "-MH" are removed first
        (r"PD", "planned", "Planned Development"), (r"DD", "mixed", "Downtown Development"),
        (r"(CC|CG|CN)", "commercial", "Commercial"), (r"RF-C", "commercial", "Riverfront Commercial"),
        (r"RF-M|UM|T5F|T5MS", "mixed", "Mixed-use / main street"), (r"RF-R", "residential", "Riverfront Residential"),
        (r"(OG|OL)", "office", "Office"), (r"IR", "special", "Institutional-Residential"), (r"(ME|MG|ML|MA)", "industrial", "Manufacturing / industrial"),
        (r"PR", "park", "Parks and Recreation"), (r"T5N", "special", "Urban neighborhood (form-based)"),
        (r"(RM|RMX|SF|T3|T4)", "residential", "Residential"),
    ],
    "Cuyahoga County (Cleveland area)": [],
    "Toledo": [
        (r"CM$", "mixed", None),
    ],
    "Wichita": [  # codes only
        (r"AFB$", "special", "Air Force Base"), (r"U$", "special", "University"),
        (r"CBD$", "mixed", "Central Business District"), (r"(GC|LC|NR)$", "commercial", "Commercial / neighborhood retail"),
        (r"(GO|NO)$", "office", "Office"), (r"OW$", "special", "Office-Warehouse"),
        (r"(GI|LI|IP)", "industrial", "Industrial"), (r"PUD", "planned", "Planned Unit Development"),
        (r"(B|MF-\d+|MH|RR|SF-\d+|TF-\d+)$", "residential", "Residential"),
    ],
    "Madison": [  # codes only
        (r"(DC|LMX|NMX|RMX|UMX|TSS)$", "mixed", "Downtown / mixed-use"), (r"(CC|CC-T)$", "commercial", "Commercial"),
        (r"(EC|SE|SEC|TE|UOR)$", "office", "Employment / office"), (r"(IG|IL)$", "industrial", "Industrial"),
        (r"DR\d", "special", "Downtown Residential"), (r"(AP|CI|MC|THV)$", "special", None),
        (r"CN$", "park", "Conservancy"), (r"PR$", "park", "Parks and Recreation"), (r"PD$", "planned", "Planned Development"),
        (r"(A|TEMP A)$", "residential", "Agricultural"), (r"(PMHP|SR-|TR-)", "residential", "Residential"),
    ],
    "Lincoln": [  # codes only
        (r"B-4$", "mixed", "Lincoln Center Business (downtown)"), (r"(B|H)-?\d?", "commercial", "Business / highway commercial"),
        (r"O-\d", "office", "Office"), (r"I", "industrial", "Industrial"), (r"P$", "special", "Public"),
        (r"R-T$", "special", "Residential Transition"), (r"AG", "residential", "Agricultural"), (r"R", "residential", "Residential"),
    ],
    "Omaha": [
        (r"CBD", "mixed", "Central Business District"), (r"DS$", "commercial", "Downtown Service"),
        (r"(CC|CH|GC|LC|NBD)$", "commercial", "Commercial"), (r"(GO|LO)$", "office", "Office"),
        (r"(GI|HI|LI)$", "industrial", "Industrial"), (r"MU$|TOD-\d-MX", "mixed", "Mixed Use"),
        (r"TOD-\d-(MUR|MNR)", "special", "Transit-oriented, residential mixed"),
        (r"(R\d|RR|MH|AG|TOD-4-SFA)", "residential", "Residential / agricultural"),
        (r"DR$", "special", "Development Reserve"),
    ],
}

# Overlay or suffix parts removed from a code before matching its rules
CODE_CLEANUP = {
    "Noblesville": r"/.*$",
    "Cincinnati": r"-(B|MH|T|O|P|M)(-.*)?$",
    "Kansas City": r"/.*$",
}


def classify(area, code, description):
    """(kind, description) for a city's district, or None if it isn't zoning."""
    raw = str(code or "").strip()
    cleaned = re.sub(CODE_CLEANUP.get(area, r"$^"), "", raw.upper())
    for pattern, kind, rule_description in RULES.get(area, []):
        if re.match(pattern, cleaned):
            if kind is NONE:
                return None
            return kind, rule_description or _tidy(description) or f"District {raw}"
    if not description:
        return ("special", f"District {raw}") if raw else None
    kind = kind_from_text(description)
    return (kind, _tidy(description)) if kind else None


# --- Looking a point up on each city's map ---
#
# Each source: city/area name, label, where it applies (state FIPS, county FIPS),
# and how to read it. "fields" is (code field, description fields...).

SOURCES = [
    {"area": "Indianapolis", "label": "City of Indianapolis GIS (MapIndy zoning)", "counties": [("18", "097")],
     "url": "https://gis.indy.gov/server/rest/services/MapIndy/Zoning/MapServer/6", "fields": ("CODE1",)},
    {"area": "Carmel", "label": "City of Carmel GIS", "counties": [("18", "057")],
     "url": "https://carmelgis.carmel.in.gov/server/rest/services/DOCS/Carmel_Zoning/MapServer/1", "fields": ("ZONE_ID", "ZONE_DES")},
    {"area": "Fishers", "label": "City of Fishers GIS", "counties": [("18", "057")],
     "url": "https://maps.fishers.in.us/arcgis/rest/services/Zoning/MapServer/2", "fields": ("Zoning",)},
    {"area": "Westfield", "label": "City of Westfield GIS", "counties": [("18", "057")],
     "url": "https://maps.westfield.in.gov/arcgis/rest/services/CommunityDev/Zoning/MapServer/0", "fields": ("Zoning", "Descript")},
    {"area": "Noblesville", "label": "City of Noblesville GIS", "counties": [("18", "057")],
     "url": "https://nobgis.cityofnoblesville.org/arcgis/rest/services/CityData/Zoning_Map_Pro/MapServer/8", "fields": ("ZONING",)},
    {"area": "Tippecanoe County", "label": "Tippecanoe County Area Plan Commission GIS", "counties": [("18", "157")],
     "url": "https://maps.tippecanoe.in.gov/server/rest/services/ZoningOnly/MapServer/2", "fields": ("RefName",)},
    {"area": "Fort Wayne / Allen County", "label": "Allen County GIS (iMap)", "counties": [("18", "003")],
     "url": "https://gis1.acimap.us/imapweb/rest/services/QueryLayers/QueryLayers/MapServer/9",
     "fields": ("GISPublished.SDE.Zoning_Polygons.ZONING_CLASS", "GISPublished.SDE.Zoning_Class_Descriptions.Class_Description")},
    {"area": "Bloomington", "label": "City of Bloomington GIS", "counties": [("18", "105")],
     "url": "https://bloomington.in.gov/arcgis-server/rest/services/DataPortal/DataPortal_PlanningZoning/MapServer/1",
     "fields": ("geodb.sde.ZoningDistricts.zone_code", "geodb.sde.ZoningDistricts.zone_description")},
    {"area": "South Bend", "label": "City of South Bend GIS", "counties": [("18", "141")],
     "url": "https://gis.southbendin.gov/arcgis/rest/services/LandRecords/City_of_South_Bend_Zoning_Public/MapServer/0",
     "fields": ("SB_ZONE_CODE", "SB_ZONE_DESC")},
    {"area": "Evansville / Vanderburgh County", "label": "Evansville-Vanderburgh Area Plan Commission GIS", "counties": [("18", "163")],
     "url": "https://maps.evansvillegis.com/arcgis_server/rest/services/APC/ADDTL_LAYERS/MapServer/5", "fields": ("Zoning_District",)},
    {"area": "Columbus", "label": "City of Columbus Building and Zoning Services", "counties": [("39", "049")],
     "url": "https://gis.columbus.gov/arcgis/rest/services/Applications/Zoning/MapServer/31",
     "fields": ("CLASSIFICATION", "GENERAL_ZONING_CATEGORY"), "where": "ZONING_STATUS = 'PASSED'"},
    {"area": "Detroit", "label": "City of Detroit Open Data", "counties": [("26", "163")],
     "url": "https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest/services/Zoning_1/FeatureServer/0",
     "fields": ("ZONING_REV", "ZDESCR_N")},
    {"area": "Milwaukee", "label": "City of Milwaukee (Map Milwaukee)", "counties": [("55", "079")],
     "url": "https://milwaukeemaps.milwaukee.gov/arcgis/rest/services/planning/zoning/MapServer/11",
     "fields": ("Zoning", "ZoningCategory", "ZoningType")},
    {"area": "Minneapolis", "label": "City of Minneapolis Open Data", "counties": [("27", "053")],
     "url": "https://services.arcgis.com/afSMGVsC7QlRK1kZ/ArcGIS/rest/services/Planning_Primary_Zoning/FeatureServer/0",
     "fields": ("Land_Use_Code", "Land_Use")},
    {"area": "Saint Paul", "label": "City of Saint Paul GIS", "counties": [("27", "123")], "web_mercator": True,
     "url": "https://services1.arcgis.com/9meaaHE3uiba0zr8/arcgis/rest/services/Zoning/FeatureServer/6",
     "fields": ("Zoning", "Zoning_Name")},
    {"area": "Kansas City", "label": "City of Kansas City, MO GIS", "counties": [("29", "095"), ("29", "047"), ("29", "165")],
     "url": "https://mapd.kcmo.org/kcgis/rest/services/maps/Zoning/MapServer/0", "fields": ("CLASSIFICATION", "LANDUSE")},
    {"area": "St. Louis", "label": "City of St. Louis Planning (PlanSTL)", "counties": [("29", "510")],
     "url": "https://services6.arcgis.com/HZXbCkpCSqbGd0vK/ArcGIS/rest/services/PlanSTL_PLANET/FeatureServer/26",
     "fields": ("ZONING1",)},
    {"area": "Cincinnati", "label": "CAGIS (Cincinnati Area GIS)", "counties": [("39", "061")],
     "url": "https://cagisonline.hamilton-co.org/arcgis/rest/services/Countywide_Layers/Zoning/MapServer/4",
     "fields": ("ZONING",)},
    {"area": "Cuyahoga County (Cleveland area)", "label": "Cuyahoga County Planning Commission", "counties": [("39", "035")],
     "url": "https://gis.cuyahogacounty.gov/server/rest/services/CCPC/CCPC_Zoning/MapServer/1",
     "fields": ("MUNI_ZONE_", "GENERALIZE", "ZONE_DESI")},
    {"area": "Omaha", "label": "Douglas County / City of Omaha GIS", "counties": [("31", "055")],
     "url": "https://dcgis.org/server/rest/services/Hosted/Zoning_Omaha_view/FeatureServer/0", "fields": ("zoneclass",)},
    {"area": "Toledo", "label": "Toledo-Lucas County Plan Commissions", "counties": [("39", "095")],
     "url": "https://gis.toledo.oh.gov/arcgis/rest/services/Public/PlanningComAppUNC10419/MapServer/4",
     "fields": ("Zoning", "Description")},
    {"area": "Wichita", "label": "City of Wichita GIS", "counties": [("20", "173")],
     "url": "https://gismaps.wichita.gov/ageweb/rest/services/COWGIS/Plan_Zoning/MapServer/7", "fields": ("ZONING",)},
    {"area": "Madison", "label": "City of Madison GIS", "counties": [("55", "025")],
     "url": "https://maps.cityofmadison.com/arcgis/rest/services/Planning/Zoning/MapServer/1", "fields": ("ZONING_CODE",)},
    {"area": "Lincoln", "label": "Lincoln-Lancaster County Planning", "counties": [("31", "109")],
     "url": "https://gis.lincoln.ne.gov/public/rest/services/Planning/DevRevZoningandRegulations/MapServer/1",
     "fields": ("ZONE",), "where": "JURISDICTION IN ('Lincoln', 'County')"},
    {"area": "Chicago", "label": "City of Chicago Data Portal (Zoning Districts)", "counties": [("17", "031")], "socrata": True,
     "url": "https://data.cityofchicago.org/resource/dj47-wfun.json", "fields": ("zone_class",)},
]

COVERED_AREAS = ", ".join(s["area"] for s in SOURCES)


def covered_areas_text():
    names = [s["area"] for s in SOURCES]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _web_mercator(lat, lon):
    x = lon * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) / (math.pi / 180) * 20037508.34 / 180
    return x, y


def _distance_to_rings(lat, lon, rings):
    """Approximate meters from a point to the nearest edge of a polygon's rings (lon/lat)."""
    scale_x = 111_320 * math.cos(math.radians(lat))
    best = float("inf")
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            ax, ay = (x1 - lon) * scale_x, (y1 - lat) * 111_320
            bx, by = (x2 - lon) * scale_x, (y2 - lat) * 111_320
            dx, dy = bx - ax, by - ay
            t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
            best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def _arcgis_query(source, lat, lon, nearby):
    """Features at (or, with nearby=True, within NEARBY_METERS of) the point: [(attributes, meters away)]."""
    if source.get("web_mercator"):
        x, y = _web_mercator(lat, lon)
        point = {"geometry": f"{x},{y}", "inSR": 102100}
    else:
        point = {"geometry": f"{lon},{lat}", "inSR": 4326}
    params = {**point, "geometryType": "esriGeometryPoint", "spatialRel": "esriSpatialRelIntersects",
              "outFields": ",".join(source["fields"]), "returnGeometry": "false", "f": "json"}
    if source.get("where"):
        params["where"] = source["where"]
    if nearby:
        params.update(distance=NEARBY_METERS, units="esriSRUnit_Meter", returnGeometry="true", outSR=4326)
    response = requests.get(source["url"] + "/query", timeout=15, params=params)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise ValueError(data["error"])
    return [(f["attributes"], _distance_to_rings(lat, lon, (f.get("geometry") or {}).get("rings", [])) if nearby else 0.0)
            for f in data.get("features", [])]


def _socrata_query(source, lat, lon, nearby):
    code_field = source["fields"][0]
    where = (f"within_circle(the_geom, {lat}, {lon}, {NEARBY_METERS})" if nearby
             else f"intersects(the_geom, 'POINT ({lon} {lat})')")
    response = requests.get(source["url"], timeout=15, params={
        "$where": where, "$select": f"{code_field}" + (", the_geom" if nearby else ""), "$limit": 20})
    response.raise_for_status()
    rows = response.json()
    found = []
    for row in rows:
        meters = 0.0
        if nearby:
            meters = min((_distance_to_rings(lat, lon, polygon)
                          for polygon in (row.get("the_geom") or {}).get("coordinates", [])), default=float("inf"))
        found.append(({code_field: row.get(code_field)}, meters))
    return found


def _district(source, attributes):
    """The classified district from one map feature, or None if it isn't zoning."""
    code = attributes.get(source["fields"][0])
    description = " ".join(str(attributes.get(f)) for f in source["fields"][1:] if attributes.get(f) not in (None, "", " "))
    if source["area"] == "Cuyahoga County (Cleveland area)":
        description = str(attributes.get("GENERALIZE") or "")  # the county's simple category: Commercial, Single-Family...
        zone_name = _tidy(attributes.get("ZONE_DESI"))
        result = classify(source["area"], code, description)
        return result and (result[0], zone_name or result[1])
    return classify(source["area"], code, description)


def _pretty_code(area, code):
    code = str(code or "").strip()
    if area == "Indianapolis":  # CBD1 -> CBD-1, C4 -> C-4
        match = re.match(r"^([A-Z]+)(\d.*)$", code)
        return f"{match.group(1)}-{match.group(2)}" if match else code
    return code


def _query(source, lat, lon, nearby):
    query = _socrata_query if source.get("socrata") else _arcgis_query
    results = []
    for attributes, meters in query(source, lat, lon, nearby):
        district = _district(source, attributes)
        if district:
            results.append((meters, source, attributes.get(source["fields"][0]), district))
    return results


@lru_cache(maxsize=512)
def _lookup(state, county, lat, lon):
    """
    The district at a point, trying every map that covers its county (all
    at once). Returns (source, code, (kind, description), nearby) or None.
    Raises requests.RequestException if no covering map answered.
    """
    sources = [s for s in SOURCES if (state, county) in s["counties"]]
    for nearby in (False, True):
        errors = 0
        found = []
        with ThreadPoolExecutor(max_workers=len(sources)) as pool:
            futures = [pool.submit(_query, s, lat, lon, nearby) for s in sources]
            for future in futures:
                try:
                    found += future.result()
                except (requests.RequestException, ValueError, KeyError):
                    errors += 1
        if found:
            # an exact hit from the first map in SOURCES order, or else the nearest district
            meters, source, code, district = min(found, key=lambda f: (f[0], SOURCES.index(f[1])))
            return source, code, district, nearby
        if errors == len(sources):
            raise requests.RequestException("No zoning map answered.")
    return None


def get_zoning(location, business_type):
    """
    The zoning check for one site. Always returns a dict; "available" is
    False (with a "reason") outside the covered areas or if the city's
    map service doesn't answer.
    """
    key = (location["state"], location["county"])
    if not any(key in s["counties"] for s in SOURCES):
        return {"available": False, "reason": f"Zoning isn't available for this area yet. SiteScout checks zoning in {covered_areas_text()}."}
    try:
        found = _lookup(*key, round(location["lat"], 6), round(location["lon"], 6))
    except requests.RequestException:
        return {"available": False, "reason": "The city's zoning map didn't respond. Try again in a minute."}
    if found is None:
        return {"available": False, "reason": f"This spot isn't on a covered zoning map. SiteScout checks zoning in {covered_areas_text()}."}

    source, code, (kind, description), nearby = found
    city = source["area"]
    district = _pretty_code(city, code)
    status = KIND_RULES[kind][business_type]
    business = BUSINESS_NAMES[business_type]
    kind_label = KIND_LABELS[kind]
    planning_office = re.split(r" / | \(", city)[0]
    if status == "allowed":
        explanation = f"{district} is a {kind_label} district, which is generally meant for businesses like a {business}."
    elif status == "check":
        explanation = (f"{district} is a {kind_label} district. A {business} may be allowed only with special approval "
                       f"or under rules written for this site, so check with the {planning_office} planning office.")
    else:
        explanation = (f"{district} is a {kind_label} district, which generally doesn't allow a {business}. "
                       f"Opening here would likely need a rezoning or variance.")
    return {
        "available": True,
        "city": city,
        "district": district,
        "description": description,
        "kind": kind,
        "status": status,
        "explanation": explanation,
        "source": source["label"],
        "nearest_lot": nearby,
        "disclaimer": ("The address point sits on the street, so this is the zoning of the nearest lot. " if nearby else "")
                      + "Based on the district's general purpose, not the city's full list of permitted uses. "
                        "Confirm with the planning office before signing a lease.",
    }
