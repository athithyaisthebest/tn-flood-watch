"""
Tamil Nadu Flood Watch - historical data download
--------------------------------------------------
Downloads past daily rainfall for all 38 districts and past daily river flow
for 16 major river points in Tamil Nadu, from Open-Meteo (free, no key needed).

How to run:
    pip install requests
    python download_history.py

It respects Open-Meteo's free limits (5,000 calls per hour), so it may pause
for a while partway through. Total time: about 15-70 minutes.
If it stops, just run it again: finished parts are skipped.

When it says "All done", upload  tn_flood_history.zip  to Claude.
"""

import csv
import json
import os
import sys
import time
import zipfile
from datetime import date, timedelta

try:
    import requests
except ImportError:
    sys.exit("Please run:  pip install requests   and then try again.")

OUT = "tn_flood_history"
HOURLY_BUDGET = 4500          # stay under Open-Meteo's 5,000 calls/hour
END = date.today() - timedelta(days=7)

# One representative point per district (inside the district boundary)
DISTRICTS = {
    "Ariyalur": (11.153, 79.259), "Chengalpattu": (12.637, 80.066), "Chennai": (13.100, 80.219),
    "Coimbatore": (10.813, 77.082), "Cuddalore": (11.520, 79.338), "Dharmapuri": (12.145, 78.113),
    "Dindigul": (10.427, 77.816), "Erode": (11.419, 77.331), "Kallakurichi": (11.792, 79.051),
    "Kancheepuram": (12.794, 79.774), "Kanyakumari": (8.329, 77.354), "Karur": (10.822, 78.384),
    "Krishnagiri": (12.515, 78.010), "Madurai": (9.936, 78.008), "Mayiladuthurai": (11.191, 79.696),
    "Nagapattinam": (10.602, 79.761), "Namakkal": (11.305, 78.119), "Nilgiris": (11.446, 76.697),
    "Perambalur": (11.290, 78.930), "Pudukkottai": (10.292, 78.817), "Ramanathapuram": (9.519, 78.650),
    "Ranipet": (12.919, 79.408), "Salem": (11.647, 78.211), "Sivaganga": (9.965, 78.720),
    "Tenkasi": (9.092, 77.476), "Thanjavur": (10.660, 79.198), "Theni": (9.869, 77.422),
    "Thiruvallur": (13.255, 80.054), "Thiruvarur": (10.649, 79.500), "Thoothukudi": (8.846, 77.993),
    "Tiruchirappalli": (10.851, 78.727), "Tirunelveli": (8.549, 77.580), "Tirupathur": (12.584, 78.625),
    "Tiruppur": (10.791, 77.535), "Tiruvannamalai": (12.429, 79.000), "Vellore": (12.902, 79.061),
    "Viluppuram": (12.125, 79.601), "Virudhunagar": (9.493, 77.864),
}
# Districts that also flood in the south-west monsoon (Jul-Sep)
WESTERN = ["Nilgiris", "Coimbatore", "Erode", "Salem", "Namakkal", "Karur",
           "Tiruchirappalli", "Theni", "Kanyakumari", "Dharmapuri", "Tiruppur"]

# Major river points: name -> (lat, lon, district)
RIVERS = {
    "Cauvery at Mettur": (11.80, 77.80, "Salem"),
    "Cauvery at Kodumudi": (11.08, 77.89, "Erode"),
    "Cauvery at Musiri": (10.95, 78.44, "Tiruchirappalli"),
    "Cauvery at Grand Anicut": (10.83, 78.82, "Thanjavur"),
    "Bhavani at Savandapur": (11.44, 77.58, "Erode"),
    "Amaravathi at Karur": (10.96, 78.08, "Karur"),
    "Tamirabarani at Tirunelveli": (8.73, 77.70, "Tirunelveli"),
    "Tamirabarani at Srivaikuntam": (8.63, 77.91, "Thoothukudi"),
    "Vaigai at Madurai": (9.93, 78.12, "Madurai"),
    "Ponnaiyar at Tirukoilur": (11.95, 79.20, "Kallakurichi"),
    "Ponnaiyar at Cuddalore": (11.75, 79.75, "Cuddalore"),
    "Palar at Vellore": (12.92, 79.13, "Vellore"),
    "Vellar at Sethiathope": (11.40, 79.53, "Cuddalore"),
    "Adyar at Chennai": (13.01, 80.24, "Chennai"),
    "Kosasthalaiyar near Ennore": (13.23, 80.29, "Thiruvallur"),
    "Gundar at Kamuthi": (9.40, 78.37, "Ramanathapuram"),
}

used = []  # (time, weight) for the rolling hourly budget


def wait_for_budget(weight):
    while True:
        now = time.time()
        while used and now - used[0][0] > 3600:
            used.pop(0)
        if sum(w for _, w in used) + weight <= HOURLY_BUDGET:
            used.append((now, weight))
            return
        wait = int(3600 - (now - used[0][0])) + 5
        print(f"   pausing {wait // 60} min to stay within Open-Meteo's free limit...")
        time.sleep(min(wait, 600))


def fetch(url, params, weight):
    wait_for_budget(weight)
    for attempt in range(6):
        try:
            r = requests.get(url, params=params, timeout=180)
        except requests.RequestException as e:
            print(f"   network problem ({e}); retrying...")
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 429:
            print("   Open-Meteo says slow down; waiting 10 min...")
            time.sleep(600)
            continue
        if r.status_code >= 500:
            time.sleep(15 * (attempt + 1))
            continue
        if r.status_code != 200:
            sys.exit(f"Open-Meteo error {r.status_code}: {r.text[:300]}")
        data = r.json()
        return data if isinstance(data, list) else [data]
    sys.exit("Gave up after several retries - please send this output to Claude.")


def windows(months):
    """Yield (start, end) date windows for each year 2015..now."""
    for y in range(2015, END.year + 1):
        if months == "NE":   # north-east monsoon: Oct 1 -> Jan 31
            a, b = date(y, 10, 1), date(y + 1, 1, 31)
        elif months == "SW":  # south-west monsoon: Jul 1 -> Sep 30
            a, b = date(y, 7, 1), date(y, 9, 30)
        else:                 # Jul 1 -> Jan 31
            a, b = date(y, 7, 1), date(y + 1, 1, 31)
        if a > END:
            continue
        yield a, min(b, END)


def download(kind, names, coords, months, path):
    if os.path.exists(path):
        print(f"  {os.path.basename(path)} already done, skipping")
        return
    if kind == "rain":
        url = "https://archive-api.open-meteo.com/v1/archive"
        var = "precipitation_sum"
    else:
        url = "https://flood-api.open-meteo.com/v1/flood"
        var = "river_discharge"
    rows = []
    for a, b in windows(months):
        days = (b - a).days + 1
        for i in range(0, len(names), 10):   # 10 locations per request
            chunk_n, chunk_c = names[i:i + 10], coords[i:i + 10]
            params = {
                "latitude": ",".join(str(c[0]) for c in chunk_c),
                "longitude": ",".join(str(c[1]) for c in chunk_c),
                "start_date": a.isoformat(), "end_date": b.isoformat(),
                "daily": var, "timezone": "Asia/Kolkata",
            }
            weight = len(chunk_n) * max(1, days / 14)
            results = fetch(url, params, weight)
            for name, res in zip(chunk_n, results):
                d = res["daily"]
                for t, v in zip(d["time"], d[var]):
                    rows.append((t, name, v))
        print(f"  {kind}: {a} to {b} done")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "name", "precip_mm" if kind == "rain" else "discharge_m3s"])
        w.writerows(rows)


def main():
    os.makedirs(OUT, exist_ok=True)
    names = list(DISTRICTS)
    print("1/3  Rainfall, north-east monsoon seasons (all 38 districts)...")
    download("rain", names, [DISTRICTS[n] for n in names], "NE", f"{OUT}/rain_NE.csv")
    print("2/3  Rainfall, south-west monsoon seasons (western districts)...")
    download("rain", WESTERN, [DISTRICTS[n] for n in WESTERN], "SW", f"{OUT}/rain_SW.csv")
    print("3/3  River flow at 16 river points (GloFAS)...")
    rn = list(RIVERS)
    download("river", rn, [RIVERS[n][:2] for n in rn], "ALL", f"{OUT}/river.csv")
    with open(f"{OUT}/rivers.json", "w") as f:
        json.dump({k: {"lat": v[0], "lon": v[1], "district": v[2]} for k, v in RIVERS.items()}, f, indent=1)
    with zipfile.ZipFile("tn_flood_history.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in os.listdir(OUT):
            z.write(os.path.join(OUT, name))
    print("\nAll done! Upload  tn_flood_history.zip  to Claude.")


if __name__ == "__main__":
    main()
