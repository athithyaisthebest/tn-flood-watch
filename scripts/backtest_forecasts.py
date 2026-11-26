"""
Real-forecast test: how much warning would the live site have given, using the forecasts
that were actually issued at the time (Open-Meteo Previous Runs archive, Jan 2024 onwards)?

For each flood since 2024 and each lead time L (0-3 days), a district counts as warned at lead L
if the forecast issued L days before any day of the flood showed Warning or above for that day.
Also counts Warning days (lead 1) with no recorded flood, July-January.
Writes docs/data/forecast_test.json.
"""

import csv
import json
import os
from collections import defaultdict
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
HIST = os.path.join(ROOT, "data_hist")
D = date.fromisoformat
LEADS = [0, 1, 2, 3]


def lvl(v, th):
    out = 0
    for i, k in enumerate(["watch", "warning", "danger"], 1):
        if v is not None and th and v >= th[k]:
            out = i
    return out


def main():
    events = json.load(open(os.path.join(DATA, "flood_events.json")))
    thr = json.load(open(os.path.join(DATA, "thresholds.json")))
    rain_t = thr["rain"]
    fc = defaultdict(dict)   # (key) -> day -> [lead0..3]
    for r in csv.DictReader(open(os.path.join(HIST, "forecast_archive.csv"))):
        fc[r["district"]][D(r["date"])] = [None if r[f"lead{L}_mm"] in ("", "None") else float(r[f"lead{L}_mm"])
                                           for L in LEADS]
    first = min(min(s) for s in fc.values())
    last = max(max(s) for s in fc.values())
    print(f"forecast archive {first} to {last}, {len(fc)} points")

    # level per district per day per lead (upstream catchments count for their district)
    level = defaultdict(lambda: defaultdict(dict))   # district -> lead -> day -> level
    for key, s in fc.items():
        n = key[:-len(" upstream")] if key.endswith(" upstream") else key
        t = rain_t.get(key) or rain_t["_default"]
        for day in s:
            for li, L in enumerate(LEADS):
                r1 = s[day][li]
                if r1 is None:
                    continue
                r3 = sum((s.get(day - timedelta(days=k), [0] * 4)[li] or 0) for k in range(3))
                lv = max(lvl(r1, t["r1"]), lvl(r3, t["r3"]))
                level[n][L][day] = max(level[n][L].get(day, 0), lv)

    out_events = []
    pairs = 0
    caught = {L: 0 for L in LEADS}
    for e in events:
        a, b = D(e["start"]), D(e["end"])
        if a < first or b > last:
            continue
        rows = []
        for n in e["districts"]:
            pairs += 1
            best = None
            for L in sorted(LEADS, reverse=True):
                if any(level[n][L].get(a + timedelta(days=k), 0) >= 2 for k in range((b - a).days + 1)):
                    best = L
                    break
            for L in LEADS:
                if any(level[n][L].get(a + timedelta(days=k), 0) >= 2 for k in range((b - a).days + 1)):
                    caught[L] += 1
            rows.append({"district": n, "max_lead_days": best})
        out_events.append({"id": e["id"], "name": e["name"], "start": e["start"], "end": e["end"], "districts": rows})

    quiet = defaultdict(list)
    for e in events:
        for n in e["districts"]:
            quiet[n].append((D(e["start"]) - timedelta(days=3), D(e["end"]) + timedelta(days=3)))
    fa = defaultdict(int)
    seasons = set()
    for n, by in level.items():
        for L, days in by.items():
            for day, lv in days.items():
                if not (day.month >= 7 or day.month == 1):
                    continue
                if L == 1:
                    seasons.add((n, day.year if day.month >= 7 else day.year - 1))
                if lv >= 2 and not any(x <= day <= y for x, y in quiet[n]):
                    fa[L] += 1
    nseason = max(len(seasons), 1)
    summary = {
        "period": [first.isoformat(), last.isoformat()],
        "district_floods": pairs,
        "caught_by_lead": {str(L): caught[L] for L in LEADS},
        "warned_at_least": {str(L): sum(1 for e in out_events for r in e["districts"]
                                        if r["max_lead_days"] is not None and r["max_lead_days"] >= L) for L in LEADS},
        "no_flood_warning_days_per_district_season": {str(L): round(fa[L] / nseason, 2) for L in LEADS},
        "thresholds": thr.get("version"),
        "note": ("Uses the forecasts that were actually issued at the time (Open-Meteo Previous Runs archive). "
                 "Lead 1 = the forecast made the day before. Only rainfall forecasts are tested here; "
                 "rain gauges, satellite and river signals add warnings as rain starts to fall."),
    }
    json.dump({"summary": summary, "events": out_events}, open(os.path.join(DATA, "forecast_test.json"), "w"), indent=1)
    print(json.dumps(summary, indent=1))
    for e in out_events:
        print(e["name"], [(r["district"], r["max_lead_days"]) for r in e["districts"]])


if __name__ == "__main__":
    main()
