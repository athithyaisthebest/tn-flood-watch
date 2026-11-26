"""
Extra downloads (run on GitHub Actions via "Fetch history" with what = forecasts / rivers):

  forecasts  Archived rain forecasts from Open-Meteo's Previous Runs API (Jan 2024 -> now):
             for every district point (and upstream catchments), the daily rain that was forecast
             0, 1, 2 and 3 days ahead. -> data_hist/forecast_archive.csv
  rivers     For each river point, search nearby GloFAS grid squares for the real main channel
             (the square with the highest average flow) and add new upstream river points.
             -> data_hist/river_search.csv and data_hist/glofas_river_v2.csv
"""

import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, HIST, UPSTREAM  # noqa: E402

UA = {"User-Agent": "tn-flood-watch student project"}
used = []


def budget(weight, per_hour=4000):
    while True:
        now = time.time()
        while used and now - used[0][0] > 3600:
            used.pop(0)
        if sum(w for _, w in used) + weight <= per_hour:
            used.append((now, weight))
            return
        print("pausing for Open-Meteo's hourly limit...", flush=True)
        time.sleep(300)


def get(url, params, weight):
    budget(weight)
    for attempt in range(6):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=300)
        except requests.RequestException as e:
            print("network:", e)
            time.sleep(20)
            continue
        if r.status_code == 429:
            print("429, waiting 10 min")
            time.sleep(600)
            continue
        if r.status_code != 200:
            print("HTTP", r.status_code, r.text[:300])
            if r.status_code >= 500:
                time.sleep(30)
                continue
            return None
        d = r.json()
        return d if isinstance(d, list) else [d]
    return None


def points():
    pts = json.load(open(os.path.join(DATA, "points.json")))
    rep = {}
    for p in pts:
        rep[p["district"]] = (p["lat"], p["lon"])
    for n, (w, s, e, nn) in UPSTREAM.items():
        rep[f"{n} upstream"] = (round((s + nn) / 2, 3), round((w + e) / 2, 3))
    return rep


def run_forecasts():
    rep = points()
    names = sorted(rep)
    leads = [0, 1, 2, 3]
    var = ["precipitation"] + [f"precipitation_previous_day{k}" for k in leads[1:]]
    start, end = date(2024, 1, 1), date.today() - timedelta(days=2)
    rows = []
    a = start
    while a <= end:
        b = min(end, a + timedelta(days=90))
        for i in range(0, len(names), 10):
            chunk = names[i:i + 10]
            res = get("https://previous-runs-api.open-meteo.com/v1/forecast", {
                "latitude": ",".join(str(rep[n][0]) for n in chunk),
                "longitude": ",".join(str(rep[n][1]) for n in chunk),
                "hourly": ",".join(var), "start_date": a.isoformat(), "end_date": b.isoformat(),
                "timezone": "Asia/Kolkata",
            }, len(chunk) * ((b - a).days + 1) / 14 * 1.0)
            if not res:
                print(f"failed {a}..{b} {chunk[0]}")
                continue
            for n, r in zip(chunk, res):
                h = r["hourly"]
                daily = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
                seen = defaultdict(lambda: [0, 0, 0, 0])
                for idx, t in enumerate(h["time"]):
                    day = t[:10]
                    for li, v in enumerate(var):
                        x = h.get(v, [None] * len(h["time"]))[idx]
                        if x is not None:
                            daily[day][li] += x
                            seen[day][li] += 1
                for day in sorted(daily):
                    rows.append([day, n] + [round(daily[day][li], 1) if seen[day][li] >= 20 else None
                                            for li in range(len(leads))])
        print(f"forecasts {a}..{b} done", flush=True)
        a = b + timedelta(days=1)
    with open(os.path.join(HIST, "forecast_archive.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "district", "lead0_mm", "lead1_mm", "lead2_mm", "lead3_mm"])
        w.writerows(rows)
    print(f"wrote {len(rows)} rows")


NEW_RIVERS = {
    "Cauvery at Hogenakkal (inflow from Karnataka)": (12.12, 77.78, "Dharmapuri"),
    "Vaigai near Theni": (10.01, 77.50, "Theni"),
    "Tamirabarani near Papanasam": (8.71, 77.37, "Tirunelveli"),
    "Thenpennai near Krishnagiri": (12.47, 78.25, "Krishnagiri"),
}


def run_rivers():
    rivers = json.load(open(os.path.join(DATA, "rivers.json")))
    for k, (la, lo, d) in NEW_RIVERS.items():
        rivers.setdefault(k, {"lat": la, "lon": lo, "district": d})
    step, reach = 0.05, 3      # GloFAS grid is 0.05 deg; search +-0.15 deg
    results = []
    for name, info in rivers.items():
        cands = [(round(info["lat"] + i * step, 3), round(info["lon"] + j * step, 3))
                 for i in range(-reach, reach + 1) for j in range(-reach, reach + 1)]
        best = None
        for c0 in range(0, len(cands), 25):
            chunk = cands[c0:c0 + 25]
            res = get("https://flood-api.open-meteo.com/v1/flood", {
                "latitude": ",".join(str(c[0]) for c in chunk), "longitude": ",".join(str(c[1]) for c in chunk),
                "daily": "river_discharge", "start_date": "2021-11-01", "end_date": "2021-11-30",
            }, len(chunk) * 30 / 14)
            for (la, lo), r in zip(chunk, res or []):
                v = [x for x in r["daily"]["river_discharge"] if x is not None]
                if v:
                    m = sum(v) / len(v)
                    if best is None or m > best[2]:
                        best = (la, lo, m, max(v))
        if best:
            dist = ((best[0] - info["lat"]) ** 2 + (best[1] - info["lon"]) ** 2) ** 0.5
            results.append([name, info["lat"], info["lon"], best[0], best[1], round(best[2], 1), round(best[3], 1), round(dist, 3)])
            print(f"{name}: best square {best[0]},{best[1]} mean {best[2]:.1f} m3/s (moved {dist:.2f} deg)", flush=True)
            info.update({"lat": best[0], "lon": best[1], "use": best[2] >= 5, "search_mean_m3s": round(best[2], 1)})
    with open(os.path.join(HIST, "river_search.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["river", "old_lat", "old_lon", "new_lat", "new_lon", "mean_m3s", "max_m3s", "moved_deg"])
        w.writerows(results)
    json.dump(rivers, open(os.path.join(DATA, "rivers.json"), "w"), indent=1)
    # history for the corrected points
    rows = []
    names = [n for n in rivers if rivers[n].get("use", True)]
    for y in range(2015, date.today().year + 1):
        a, b = date(y, 1, 1), min(date(y, 12, 31), date.today() - timedelta(days=1))
        for i in range(0, len(names), 10):
            chunk = names[i:i + 10]
            res = get("https://flood-api.open-meteo.com/v1/flood", {
                "latitude": ",".join(str(rivers[n]["lat"]) for n in chunk),
                "longitude": ",".join(str(rivers[n]["lon"]) for n in chunk),
                "daily": "river_discharge", "start_date": a.isoformat(), "end_date": b.isoformat(),
            }, len(chunk) * ((b - a).days + 1) / 14)
            for n, r in zip(chunk, res or []):
                for t, v in zip(r["daily"]["time"], r["daily"]["river_discharge"]):
                    if v is not None:
                        rows.append([t, n, v])
        print(f"river history {y} done", flush=True)
    with open(os.path.join(HIST, "glofas_river_v2.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "name", "discharge_m3s"])
        w.writerows(rows)
    print(f"wrote {len(rows)} river rows")


if __name__ == "__main__":
    {"forecasts": run_forecasts, "rivers": run_rivers}[sys.argv[1]]()
