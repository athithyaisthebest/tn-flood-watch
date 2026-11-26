"""Shared helpers: district shapes and turning gridded rainfall into district numbers."""

import json
import os
import re

import numpy as np
from shapely.geometry import Point, shape
from shapely.prepared import prep

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
HIST = os.path.join(ROOT, "data_hist")
BBOX = (76.0, 7.8, 80.6, 13.8)  # west, south, east, north around Tamil Nadu


# Hill catchments outside a district whose rain floods it (summarised as "<District> upstream").
# Theni: the Periyar plateau / Cardamom Hills (Kerala), whose water reaches the Vaigai via Mullaperiyar.
UPSTREAM = {
    "Theni": (77.0, 9.45, 77.35, 10.15),
    "Tirunelveli": (77.15, 8.45, 77.4, 8.9),    # Western Ghats above Papanasam / Manimuthar
    "Kanyakumari": (77.1, 8.2, 77.35, 8.55),    # hills above Pechiparai / Perunchani
    "Coimbatore": (76.55, 10.25, 76.95, 10.6),  # Anamalai / Valparai hills draining to the Aliyar and Chalakudy
}


def base_district(name):
    return name[:-len(" upstream")] if name.endswith(" upstream") else name


def districts():
    """name -> shapely geometry (the 38 districts plus '<District> upstream' hill catchments)"""
    from shapely.geometry import box
    with open(os.path.join(DATA, "districts.json")) as f:
        g = json.load(f)
    out = {ft["properties"]["name"]: shape(ft["geometry"]) for ft in g["features"]}
    for n, b in UPSTREAM.items():
        out[f"{n} upstream"] = box(*b)
    return out


def district_cells(lats, lons):
    """For a regular grid, which (i_lat, i_lon) cells belong to each district.
    A cell belongs if its centre is inside; small districts with no centre inside
    get the cells touching them, or failing that the nearest cell."""
    lats, lons = np.asarray(lats), np.asarray(lons)
    dlat = abs(lats[1] - lats[0]) if len(lats) > 1 else 0.1
    dlon = abs(lons[1] - lons[0]) if len(lons) > 1 else 0.1
    out = {}
    for name, geom in districts().items():
        pg = prep(geom)
        cells = [(i, j) for i, la in enumerate(lats) for j, lo in enumerate(lons)
                 if pg.contains(Point(lo, la))]
        if not cells:
            cells = [(i, j) for i, la in enumerate(lats) for j, lo in enumerate(lons)
                     if geom.intersects(Point(lo, la).buffer(max(dlat, dlon) / 2, cap_style=3))]
        if not cells:
            c = geom.representative_point()
            i = int(np.argmin(abs(lats - c.y)))
            j = int(np.argmin(abs(lons - c.x)))
            cells = [(i, j)]
        out[name] = cells
    return out


def summarise(grid2d, cells):
    """grid2d[lat, lon] -> {district: (mean, max)} ignoring missing values."""
    res = {}
    for name, cs in cells.items():
        vals = np.array([grid2d[i, j] for i, j in cs], dtype=float)
        vals = vals[np.isfinite(vals) & (vals >= 0)]
        res[name] = (float(vals.mean()), float(vals.max())) if len(vals) else (None, None)
    return res


ALIASES = {
    "kanniyakumari": "Kanyakumari", "the nilgiris": "Nilgiris", "nilgiris": "Nilgiris",
    "villupuram": "Viluppuram", "viluppuram": "Viluppuram", "tiruvallur": "Thiruvallur",
    "thiruvallur": "Thiruvallur", "thiruchirappalli": "Tiruchirappalli", "tiruchirapalli": "Tiruchirappalli",
    "trichy": "Tiruchirappalli", "tuticorin": "Thoothukudi", "thoothukudi": "Thoothukudi",
    "kanchipuram": "Kancheepuram", "kancheepuram": "Kancheepuram", "sivagangai": "Sivaganga",
    "sivaganga": "Sivaganga", "tiruvarur": "Thiruvarur", "thiruvarur": "Thiruvarur",
    "tirupattur": "Tirupathur", "tirupathur": "Tirupathur", "thiruvannamalai": "Tiruvannamalai",
    "tiruvannamalai": "Tiruvannamalai", "nagapattinam": "Nagapattinam", "pudukottai": "Pudukkottai",
}


def norm_district(text):
    """Match a district name as written in a report to our district names."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    if t in ALIASES:
        return ALIASES[t]
    for name in districts_names():
        if name.lower() == t or name.lower().replace(" ", "") == t.replace(" ", ""):
            return name
    return None


_NAMES = None


def districts_names():
    global _NAMES
    if _NAMES is None:
        with open(os.path.join(DATA, "districts.json")) as f:
            _NAMES = [ft["properties"]["name"] for ft in json.load(f)["features"]]
    return _NAMES
