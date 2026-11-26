"""
Readers for the three extra data sources.

  imd_days(start, end)        IMD gridded rainfall (rain-gauge based, 0.25 deg) via imdlib
  imerg_days(start, end, run) NASA GPM IMERG daily rainfall (0.1 deg) via NASA Harmony
                              run = "final" (research quality, ~3.5 months delay) or "early" (~4 hours delay)
  tnsmart_rain(day)           TN-SMART daily rain-gauge report (state's ~590 gauges)
  tnsmart_reservoirs(day)     TN-SMART daily reservoir storage report

Each returns plain Python data; the callers decide what to save.
"""

import glob
import io
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta

import numpy as np
import requests

from common import BBOX, district_cells, norm_district, summarise

UA = {"User-Agent": "tn-flood-watch (student project; github.com/athithyaisthebest/tn-flood-watch)"}
TNSMART = "https://beta-tnsmart.rimes.int/DATA/Rainfall"


# ----------------------------------------------------------------------------- IMD gridded
def imd_days(start, end, workdir):
    """Yield (day, {district: (mean, max)}) from IMD's 0.25 deg gridded rainfall."""
    import imdlib as imd
    os.makedirs(workdir, exist_ok=True)
    cells = None
    for year in range(start.year, end.year + 1):
        try:
            obj = imd.get_data("rain", year, year, fn_format="yearwise", file_dir=workdir)
            ds = obj.get_xarray()
        except Exception as e:  # recent years may only exist as "real-time" data
            print(f"IMD final data for {year} not available ({e}); trying real-time data")
            a = max(date(year, 1, 1), start)
            b = min(date(year, 12, 31), end)
            try:
                obj = imd.get_real_data("rain", a.isoformat(), b.isoformat(), file_dir=workdir)
                ds = obj.get_xarray()
            except Exception as e2:
                print(f"IMD real-time data for {year} failed: {e2}")
                continue
        da = ds["rain"]
        lat_name = "lat" if "lat" in da.dims else "latitude"
        lon_name = "lon" if "lon" in da.dims else "longitude"
        da = da.sel({lat_name: slice(BBOX[1], BBOX[3]), lon_name: slice(BBOX[0], BBOX[2])})
        lats, lons = da[lat_name].values, da[lon_name].values
        if cells is None:
            cells = district_cells(lats, lons)
        da = da.transpose("time", lat_name, lon_name)
        arr = da.values.astype(float)
        arr[arr < 0] = np.nan  # -999 = missing
        for k, t in enumerate(da["time"].values):
            day = datetime.utcfromtimestamp(t.astype("datetime64[s]").astype(int)).date()
            if start <= day <= end:
                yield day, summarise(arr[k], cells)


# ----------------------------------------------------------------------------- NASA IMERG
def _concept_id(short_name):
    r = requests.get("https://cmr.earthdata.nasa.gov/search/collections.json",
                     params={"short_name": short_name, "page_size": 50}, timeout=60)
    r.raise_for_status()
    entries = r.json()["feed"]["entry"]
    if not entries:
        raise RuntimeError(f"no collection called {short_name}")
    entries.sort(key=lambda e: e.get("version_id", ""), reverse=True)
    print("versions found:", [e.get("version_id") for e in entries])
    return entries[0]["id"]


def imerg_days(start, end, run, user, password, workdir):
    """Yield (day, {district: (mean, max)}) from IMERG daily rainfall, subset to Tamil Nadu."""
    import xarray as xr
    from harmony import BBox, Client, Collection, Request
    short = {"final": "GPM_3IMERGDF", "early": "GPM_3IMERGDE", "late": "GPM_3IMERGDL"}[run]
    cid = _concept_id(short)
    print(f"IMERG {run}: collection {short} = {cid}")
    client = Client(auth=(user, password))
    cells = None
    # one Harmony job per 3 months keeps each job a manageable size
    a = start
    while a <= end:
        b = min(end, (a.replace(day=1) + timedelta(days=95)).replace(day=1) - timedelta(days=1))
        req = Request(collection=Collection(id=cid),
                      spatial=BBox(BBOX[0], BBOX[1], BBOX[2], BBOX[3]),
                      temporal={"start": datetime(a.year, a.month, a.day),
                                "stop": datetime(b.year, b.month, b.day, 23, 59, 59)},
                      variables=["precipitation"])
        tmp = tempfile.mkdtemp(dir=workdir)
        try:
            job = client.submit(req)
            client.wait_for_processing(job, show_progress=False)
            files = [f.result() for f in client.download_all(job, directory=tmp, overwrite=True)]
        except Exception as e:
            print(f"IMERG {a}..{b} failed: {e}")
            shutil.rmtree(tmp, ignore_errors=True)
            a = b + timedelta(days=1)
            continue
        for path in sorted(files):
            try:
                ds = xr.open_dataset(path)
            except Exception as e:
                print(f"could not open {path}: {e}")
                continue
            p = ds["precipitation"]
            if "time" in p.dims:
                days = [datetime.utcfromtimestamp(t.astype("datetime64[s]").astype(int)).date() for t in p["time"].values]
            else:
                m = re.search(r"(\d{8})", os.path.basename(path))
                days = [datetime.strptime(m.group(1), "%Y%m%d").date()]
                p = p.expand_dims("time")
            p = p.transpose("time", "lat", "lon")
            lats, lons = p["lat"].values, p["lon"].values
            if cells is None:
                cells = district_cells(lats, lons)
            arr = p.values.astype(float)
            for k, day in enumerate(days):
                yield day, summarise(arr[k], cells)
            ds.close()
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"IMERG {run}: {a} to {b} done ({len(files)} files)")
        a = b + timedelta(days=1)


# ----------------------------------------------------------------------------- TN-SMART
def _pdf_text(url):
    import pdfplumber
    r = requests.get(url, headers=UA, timeout=60)
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        return None, None
    pdf = pdfplumber.open(io.BytesIO(r.content))
    text = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
    tables = [t for pg in pdf.pages for t in (pg.extract_tables() or [])]
    pdf.close()
    return text, tables


NUM = r"-?\d+(?:\.\d+)?"


def tnsmart_rain(day):
    """{district: {"avg": mm, "max": mm (heaviest gauge, if listed), "gauges": n}} or None.
    Report for day D covers 8:30 am of D-1 to 8:30 am of D."""
    text, _ = _pdf_text(f"{TNSMART}/daily_reports/rainfall_analysis_{day.isoformat()}.pdf")
    if not text:
        return None
    out = {}
    # district rows: "<sl> <District Name> <available> <updated> <recorded> <average>"
    for m in re.finditer(rf"^\s*\d+\s+([A-Za-z .]+?)\s+(\d+)\s+(\d+)\s+({NUM})\s+({NUM})\s*$", text, re.M):
        name = norm_district(m.group(1))
        if name:
            out[name] = {"avg": float(m.group(5)), "max": None, "gauges": int(m.group(3))}
    # heavy-rain station rows: "<sl> <District> <Taluk> <Station> <mm>" (only listed when >= 64.5 mm)
    section = text.split("Heavy Rainfall", 1)
    if len(section) > 1 or "Extremely" in text:
        for line in text.splitlines():
            m = re.match(rf"^\s*\d+\s+(.+?)\s+({NUM})\s*$", line)
            if not m:
                continue
            words = m.group(1).split()
            mm = float(m.group(2))
            if mm < 64.5 or mm > 1500:
                continue
            for n in range(min(3, len(words)), 0, -1):  # district names are 1-3 words
                name = norm_district(" ".join(words[:n]))
                if name:
                    d = out.setdefault(name, {"avg": None, "max": None, "gauges": None})
                    d["max"] = max(d["max"] or 0, mm)
                    break
    return out or None


# reservoir -> districts that flood when it is full and releasing (main ones only)
DOWNSTREAM = {
    "chembarambakkam": ["Chennai", "Kancheepuram", "Chengalpattu"],
    "poondi": ["Thiruvallur", "Chennai"], "puzhal": ["Thiruvallur", "Chennai"],
    "redhills": ["Thiruvallur", "Chennai"], "cholavaram": ["Thiruvallur", "Chennai"],
    "mettur": ["Salem", "Erode", "Namakkal", "Karur", "Tiruchirappalli", "Thanjavur"],
    "stanleyreservoir": ["Salem", "Erode", "Namakkal", "Karur", "Tiruchirappalli", "Thanjavur"],
    "bhavanisagar": ["Erode"], "amaravathi": ["Tiruppur", "Karur"],
    "vaigai": ["Theni", "Madurai", "Sivaganga", "Ramanathapuram"], "periyar": ["Theni"],
    "papanasam": ["Tirunelveli", "Thoothukudi"], "manimuthar": ["Tirunelveli", "Thoothukudi"],
    "servalar": ["Tirunelveli", "Thoothukudi"],
    "sathanur": ["Tiruvannamalai", "Kallakurichi", "Viluppuram", "Cuddalore"],
    "krishnagiri": ["Krishnagiri", "Dharmapuri"], "kelavarapalli": ["Krishnagiri", "Dharmapuri"],
    "pechiparai": ["Kanyakumari"], "perunchani": ["Kanyakumari"],
    "veeranam": ["Cuddalore"], "wellingdon": ["Cuddalore"],
}


def tnsmart_reservoirs(day):
    """[{name, district, storage_pct, downstream}] or None."""
    text, tables = _pdf_text(f"{TNSMART}/daily_reservoir/reservoir_{day.strftime('%d-%m-%Y')}.pdf")
    if not text:
        return None
    rows = []
    for t in tables:
        for row in t:
            cells = [(c or "").replace("\n", " ").strip() for c in row]
            nums = [c for c in cells if re.fullmatch(rf"{NUM}%?", c)]
            if len(nums) < 5:
                continue
            texts = [c for c in cells if c and not re.fullmatch(rf"{NUM}%?", c)]
            if not texts or any("total" in c.lower() for c in texts):
                continue
            pct = float(nums[-1].rstrip("%"))
            name = texts[-1]
            dist = None
            for c in texts:
                dist = dist or norm_district(c)
            key = re.sub(r"[^a-z]", "", name.lower())
            down = next((v for k, v in DOWNSTREAM.items() if k in key), None)
            rows.append({"name": re.sub(r"\s+", " ", name), "district": dist,
                         "storage_pct": pct, "downstream": down or ([dist] if dist else [])})
    return rows or None
