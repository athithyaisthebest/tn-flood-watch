"""
Tamil Nadu Flood Watch - backtest and threshold tuning.

Uses the historical data from download_history.py (tn_flood_history/) and the
flood events list (docs/data/flood_events.json) to:
  1. set each district's rain thresholds (and each river's flow thresholds) from
     its own history, using only the TRAINING years (before July 2022);
  2. test how many recorded floods those thresholds would have flagged, on the
     TRAINING years and on the unseen TEST years (July 2022 onwards);
  3. compare with a simple baseline: IMD's fixed "very heavy rain" level (115.6 mm/day).

Writes docs/data/thresholds.json (used by the live site) and docs/data/backtest.json.
Run:  python scripts/backtest.py path/to/tn_flood_history
"""

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
SPLIT = date(2022, 7, 1)
QUANTILES = [0.95, 0.96, 0.97, 0.975, 0.98, 0.985, 0.99, 0.993, 0.995, 0.997]
FA_WEIGHT = 0.05      # how much one false-alarm day per district-season costs vs. one missed flood (as share)


def d(s):
    return date.fromisoformat(s)


def read_series(path, col):
    out = defaultdict(dict)
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for row in csv.DictReader(f):
            v = row[col]
            if v in ("", "None", None):
                continue
            out[row["name"]][d(row["date"])] = float(v)
    return out


def pct(values, q):
    v = sorted(values)
    if not v:
        return None
    i = q * (len(v) - 1)
    lo, hi = int(i), min(int(i) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def season_key(day):
    """(year, 'NE'|'SW') a day belongs to."""
    if day.month >= 10:
        return (day.year, "NE")
    if day.month == 1:
        return (day.year - 1, "NE")
    return (day.year, "SW")


def main(hist):
    rain = read_series(os.path.join(hist, "rain_NE.csv"), "precip_mm")
    sw = read_series(os.path.join(hist, "rain_SW.csv"), "precip_mm")
    for n, s in sw.items():
        rain[n].update(s)
    flow = read_series(os.path.join(hist, "river.csv"), "discharge_m3s")
    rivers = {k: v for k, v in json.load(open(os.path.join(DATA, "rivers.json"))).items() if v.get("use", True)}
    flow = {k: v for k, v in flow.items() if k in rivers}
    events = json.load(open(os.path.join(DATA, "flood_events.json")))
    districts = sorted(rain)
    print(f"{len(districts)} districts, {sum(len(s) for s in rain.values())} district-days of rain, "
          f"{len(flow)} rivers, {len(events)} flood events")

    # 1-day and 3-day rain per district per day
    r1, r3 = {}, {}
    for n in districts:
        s = rain[n]
        r1[n] = s
        r3[n] = {day: sum(s.get(day - timedelta(days=k), 0.0) for k in range(3)) for day in s}

    # windows around each flood in each district
    ev_window = defaultdict(list)     # district -> [(start-1, end, event)]
    quiet_window = defaultdict(list)  # district -> [(start-3, end+3)]
    for e in events:
        for n in e["districts"]:
            ev_window[n].append((d(e["start"]) - timedelta(days=1), d(e["end"]), e))
            quiet_window[n].append((d(e["start"]) - timedelta(days=3), d(e["end"]) + timedelta(days=3)))

    def near_event(n, day):
        return any(a <= day <= b for a, b in quiet_window[n])

    def thresholds_for(q):
        t = {}
        for n in districts:
            tr1 = [v for day, v in r1[n].items() if day < SPLIT]
            tr3 = [v for day, v in r3[n].items() if day < SPLIT]
            qs = {"watch": max(q - 0.03, 0.9), "warning": q, "danger": min(q + (1 - q) * 0.7, 0.9995)}
            t[n] = {
                "r1": {k: round(max(pct(tr1, qq) or 0, 20.0), 1) for k, qq in qs.items()},
                "r3": {k: round(max(pct(tr3, qq) or 0, 40.0), 1) for k, qq in qs.items()},
            }
        tq = {}
        for rn, s in flow.items():
            tr = [v for day, v in s.items() if day < SPLIT]
            if len(tr) > 100:
                tq[rn] = {k: round(pct(tr, qq), 1) for k, qq in
                          {"watch": max(q - 0.03, 0.9), "warning": q, "danger": min(q + (1 - q) * 0.7, 0.9995)}.items()}
        return t, tq

    def level(v, thr):
        l = 0
        for i, k in enumerate(["watch", "warning", "danger"], 1):
            if v is not None and thr and v >= thr[k]:
                l = i
        return l

    def daily_levels(t, tq):
        """district -> day -> level, from rain and the district's rivers"""
        out = {}
        for n in districts:
            L = {}
            for day in r1[n]:
                L[day] = max(level(r1[n][day], t[n]["r1"]), level(r3[n][day], t[n]["r3"]))
            for rn, info in rivers.items():
                if info["district"] == n and rn in tq:
                    for day, v in flow.get(rn, {}).items():
                        if day in L:
                            L[day] = max(L[day], level(v, tq[rn]))
            out[n] = L
        return out

    def baseline_levels():
        out = {}
        for n in districts:
            out[n] = {day: (2 if v >= 115.6 else 0) for day, v in r1[n].items()}
        return out

    def evaluate(L, period):
        lo, hi = (date(2000, 1, 1), SPLIT) if period == "train" else (SPLIT, date(2100, 1, 1))
        hits = pairs = 0
        fa_days = 0
        seasons = set()
        per_event = {}
        for e in events:
            if not (lo <= d(e["start"]) < hi):
                continue
            hit, missed = [], []
            for n in e["districts"]:
                if n not in L:
                    continue
                pairs += 1
                a, b = d(e["start"]) - timedelta(days=1), d(e["end"])
                if any(L[n].get(a + timedelta(days=k), 0) >= 2 for k in range((b - a).days + 1)):
                    hits += 1
                    hit.append(n)
                else:
                    missed.append(n)
            per_event[e["id"]] = (hit, missed)
        for n in districts:
            for day, l in L[n].items():
                if lo <= day < hi:
                    seasons.add((n, season_key(day)))
                    if l >= 2 and not near_event(n, day):
                        fa_days += 1
        fa_rate = fa_days / max(len(seasons), 1)
        years = sorted({season_key(day)[0] for n in districts for day in L[n] if lo <= day < hi})
        return {"hits": hits, "pairs": pairs, "false_alert_days": fa_days,
                "alert_days_per_district_season": round(fa_rate, 2),
                "first_year": years[0] if years else None, "last_year": years[-1] if years else None}, per_event

    # tune q on training data only
    best = None
    for q in QUANTILES:
        t, tq = thresholds_for(q)
        res, _ = evaluate(daily_levels(t, tq), "train")
        score = res["hits"] / max(res["pairs"], 1) - FA_WEIGHT * res["alert_days_per_district_season"]
        print(f"q={q}: train hits {res['hits']}/{res['pairs']}, "
              f"false-alert days/district-season {res['alert_days_per_district_season']}, score {score:.3f}")
        if best is None or score > best[0]:
            best = (score, q, t, tq)
    _, q, t, tq = best
    L = daily_levels(t, tq)
    train, pe_train = evaluate(L, "train")
    test, pe_test = evaluate(L, "test")
    B = baseline_levels()
    btrain, _ = evaluate(B, "train")
    btest, _ = evaluate(B, "test")
    print(f"\nChosen percentile {q}. TRAIN {train}. TEST {test}. BASELINE test {btest}")

    # typical (median) thresholds for the site's legend
    def med(key, lvl):
        return round(sorted(t[n][key][lvl] for n in districts)[len(districts) // 2], 1)
    thr = {
        "version": f"v1 tuned {date.today().isoformat()}: percentile {q} of each district's 2015-2022 history",
        "rain": {"_default": {k: {l: med(k, l) for l in ["watch", "warning", "danger"]} for k in ["r1", "r3"]}},
        "river": tq,
    }
    thr["rain"].update(t)
    json.dump(thr, open(os.path.join(DATA, "thresholds.json"), "w"), indent=1)

    pe = {**pe_train, **pe_test}
    bt = {
        "generated": date.today().isoformat(),
        "percentile": q,
        "summary": {"train": train, "test": test, "baseline": {"train": btrain, "test": btest}},
        "events": [{**{k: e[k] for k in ["id", "name", "start", "end", "set", "source"]},
                    "hit": pe.get(e["id"], ([], []))[0], "missed": pe.get(e["id"], ([], []))[1]} for e in events],
        "note": ("Backtest uses observed rainfall (ERA5 reanalysis via Open-Meteo) and GloFAS river reanalysis, "
                 "as if the forecast had been perfect, so real-time skill will be lower. A district flood counts as "
                 "flagged if Warning or above was reached from the day before it began to its last day. "
                 "'Days with no recorded flood' may include real floods missing from our events list."
                 " Thresholds come from reanalysis rainfall, which smooths out downpours; live forecasts can show sharper peaks, so the live site may give more warnings than the backtest suggests until it is recalibrated on archived forecasts."),
    }
    json.dump(bt, open(os.path.join(DATA, "backtest.json"), "w"), indent=1)
    print("Wrote thresholds.json and backtest.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tn_flood_history")
