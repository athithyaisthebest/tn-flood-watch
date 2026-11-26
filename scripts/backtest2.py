"""
Backtest v2: combine every signal and test on past floods.

Signals (each district, each day):
  forecast  - model rainfall (ERA5 history stands in for the forecast), 1-day and 3-day
  imd       - IMD gridded gauge rainfall, wettest 0.25-degree cell in the district, 1-day and 3-day
  satellite - NASA IMERG rainfall, wettest 0.1-degree cell in the district, 1-day and 3-day
  gauge     - TN-SMART: heaviest single rain gauge in the district (IMD categories)
  river     - GloFAS river flow at points in the district
  reservoir - TN-SMART reservoir storage above 90% / 95% and rising, for downstream districts

Thresholds for forecast/imd/satellite/river come from each district's own history before
July 2022 (percentile q). Two settings are chosen on the training years only:
  balanced          - best trade-off between floods caught and alerts with no recorded flood
  high_sensitivity  - catches every training flood it can, accepting more alerts
Writes docs/data/thresholds.json and docs/data/backtest.json.
"""

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
HIST = os.path.join(ROOT, "data_hist")
SPLIT = date(2022, 7, 1)
QS = [0.95, 0.96, 0.97, 0.975, 0.98, 0.985, 0.99, 0.993, 0.995, 0.997, 0.998]
GAUGE = {"watch": 115.6, "warning": 204.5, "danger": 300.0}   # one gauge: very heavy -> Watch, extremely heavy -> Warning
FA_WEIGHT = 0.05
D = date.fromisoformat


def read(name, key, val, keycol="district"):
    out = defaultdict(dict)
    path = os.path.join(HIST, name)
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for r in csv.DictReader(f):
            v = r.get(val)
            if v in (None, "", "None", "nan"):
                continue
            out[r[keycol]][D(r["date"])] = float(v)
    return out


def three_day(s):
    return {d: sum(s.get(d - timedelta(days=k), 0.0) for k in range(3)) for d in s}


def pct(vals, q):
    v = sorted(vals)
    if not v:
        return None
    i = q * (len(v) - 1)
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def in_season(d):
    return d.month >= 7 or d.month == 1   # Jul-Jan: both monsoons


def main():
    events = json.load(open(os.path.join(DATA, "flood_events.json")))
    rivers = {k: v for k, v in json.load(open(os.path.join(DATA, "rivers.json"))).items() if v.get("use", True)}
    names = [f["properties"]["name"] for f in json.load(open(os.path.join(DATA, "districts.json")))["features"]]

    # ---- load signals: sig[signal][district] = {day: (r1, r3)}
    era = read("era5_rain_NE.csv", "name", "precip_mm", "name")
    for n, s in read("era5_rain_SW.csv", "name", "precip_mm", "name").items():
        era[n].update(s)
    series = {
        "forecast": era,
        "imd": read("imd_rain.csv", "district", "max_mm"),
        "satellite": read("imerg_rain.csv", "district", "max_mm"),
    }
    sig = {k: {n: {d: (v, r3) for (d, v), r3 in zip(s.items(), three_day(s).values())} for n, s in ser.items()}
           for k, ser in series.items()}
    gauge = read("tnsmart_rain.csv", "district", "max_gauge_mm")
    flow = read("glofas_river.csv", "name", "discharge_m3s", "name")
    flow = {k: v for k, v in flow.items() if k in rivers}
    # reservoirs: day -> district -> level
    res_level = defaultdict(lambda: defaultdict(int))
    path = os.path.join(HIST, "tnsmart_reservoirs.csv")
    if os.path.exists(path):
        last = {}
        rows = sorted(csv.DictReader(open(path)), key=lambda r: (r["reservoir"], r["date"]))
        for r in rows:
            day, p = D(r["date"]), float(r["storage_pct"])
            prev = last.get(r["reservoir"])
            rising = prev is not None and p > prev[1] and (day - prev[0]).days <= 3
            lvl = 1 if (p >= 90 and rising) else 0   # reservoirs only ever raise Watch
            if lvl:
                for dn in (r["downstream"] or "").split(";"):
                    if dn:
                        res_level[day][dn] = max(res_level[day][dn], lvl)
            last[r["reservoir"]] = (day, p)
    available = [k for k in sig if sig[k]] + (["gauge"] if gauge else []) + (["river"] if flow else []) + \
                (["reservoir"] if res_level else [])
    print("signals available:", available)

    def thresholds(q):
        qs = {"watch": max(q - 0.03, 0.9), "warning": q, "danger": min(q + (1 - q) * 0.7, 0.9995)}
        t = {}
        for k in sig:
            t[k] = {}
            for n, s in sig[k].items():
                tr = [v for d, v in s.items() if d < SPLIT]
                if len(tr) < 200:
                    continue
                t[k][n] = {"r1": {l: round(max(pct([a for a, _ in tr], qq), 20.0), 1) for l, qq in qs.items()},
                           "r3": {l: round(max(pct([b for _, b in tr], qq), 40.0), 1) for l, qq in qs.items()}}
        tr_ = {}
        for rn, s in flow.items():
            tr = [v for d, v in s.items() if d < SPLIT]
            if len(tr) > 200:
                tr_[rn] = {l: round(pct(tr, qq), 1) for l, qq in qs.items()}
        return t, tr_

    def lvl(v, th):
        out = 0
        for i, k in enumerate(["watch", "warning", "danger"], 1):
            if v is not None and th and v >= th[k]:
                out = i
        return out

    def levels(t, tr_, use, mode="max"):
        """district -> day -> (level, set of signals at that level).
        mode "max": highest level from any signal.  mode "agree": Warning needs two signals at
        Warning or above (or one at Danger); a lone Warning signal shows as Watch."""
        if mode == "agree":
            per = {k: levels(t, tr_, {k}) for k in use}
            out = defaultdict(dict)
            for n in names:
                days = set()
                for k in per:
                    days |= set(per[k].get(n, {}))
                for d in days:
                    ls = {k: per[k][n][d][0] for k in per if d in per[k].get(n, {})}
                    strong = [k for k, l in ls.items() if l >= 2]
                    if any(l >= 3 for l in ls.values()) and len(strong) >= 2:
                        lv = 3
                    elif len(strong) >= 2 or any(l >= 3 for l in ls.values()):
                        lv = 2
                    else:
                        lv = 1 if ls else 0
                    if lv:
                        top = max(ls.values())
                        out[n][d] = (lv, {k for k, l in ls.items() if l >= min(top, 2)})
            return out
        L = defaultdict(dict)

        def put(n, d, l, why):
            if l <= 0:
                return
            if n.endswith(" upstream"):   # hill catchment rain counts for the district below it
                n = n[:-len(" upstream")]
            cur = L[n].get(d, (0, set()))
            if l > cur[0]:
                L[n][d] = (l, {why})
            elif l == cur[0]:
                cur[1].add(why)

        for k in sig:
            if k not in use:
                continue
            for n, s in sig[k].items():
                th = t[k].get(n)
                if not th:
                    continue
                for d, (a, b) in s.items():
                    put(n, d, max(lvl(a, th["r1"]), lvl(b, th["r3"])), k)
        if "gauge" in use:
            for n, s in gauge.items():
                for d, v in s.items():
                    put(n, d, lvl(v, GAUGE), "gauge")
        if "river" in use:
            for rn, s in flow.items():
                n = rivers[rn]["district"]
                for d, v in s.items():
                    put(n, d, lvl(v, tr_.get(rn)), "river")
        if "reservoir" in use:
            for d, m in res_level.items():
                for n, l in m.items():
                    put(n, d, l, "reservoir")
        return L

    quiet = defaultdict(list)
    for e in events:
        for n in e["districts"]:
            quiet[n].append((D(e["start"]) - timedelta(days=3), D(e["end"]) + timedelta(days=3)))

    def evaluate(L, period):
        lo, hi = (date(2000, 1, 1), SPLIT) if period == "train" else (SPLIT, date(2100, 1, 1))
        hits = pairs = 0
        per = {}
        for e in events:
            if not (lo <= D(e["start"]) < hi):
                continue
            hit, missed, by = [], [], set()
            a, b = D(e["start"]) - timedelta(days=1), D(e["end"])
            for n in e["districts"]:
                pairs += 1
                found = [L[n].get(a + timedelta(days=k)) for k in range((b - a).days + 1)]
                found = [f for f in found if f and f[0] >= 2]
                if found:
                    hits += 1
                    hit.append(n)
                    for f in found:
                        by |= f[1]
                else:
                    missed.append(n)
            per[e["id"]] = (hit, missed, sorted(by))
        fa = 0
        years = set()
        for n in names:
            for d, (l, _) in L[n].items():
                if lo <= d < hi and in_season(d) and l >= 2 and not any(x <= d <= y for x, y in quiet[n]):
                    fa += 1
        for k in sig.values():
            for s in k.values():
                years |= {d.year for d in s if lo <= d < hi}
                break
        nyears = max(len(years), 1)
        return {"hits": hits, "pairs": pairs, "false_alert_days": fa,
                "alert_days_per_district_season": round(fa / (len(names) * nyears), 2),
                "first_year": min(years) if years else None, "last_year": max(years) if years else None}, per

    # candidate signal sets: everything, or everything except one rain source (chosen on training years only)
    sets = [frozenset(available)] + [frozenset(available) - {k} for k in ("satellite", "imd", "forecast") if k in available]
    results = []
    for use in sets:
        for mode in ["max", "agree"]:
            for q in QS:
                t, tr_ = thresholds(q)
                r, _ = evaluate(levels(t, tr_, use, mode), "train")
                results.append(((q, mode, use), r))
            best = max((x for x in results if x[0][1:] == (mode, use)),
                       key=lambda x: x[1]["hits"] / max(x[1]["pairs"], 1) - FA_WEIGHT * x[1]["alert_days_per_district_season"])
            print(f"{mode:5s} without {sorted(set(available) - use) or '-'}: best q={best[0][0]} train "
                  f"{best[1]['hits']}/{best[1]['pairs']}, {best[1]['alert_days_per_district_season']} no-flood alert days")
    score = lambda x: x[1]["hits"] / max(x[1]["pairs"], 1) - FA_WEIGHT * x[1]["alert_days_per_district_season"]
    bal = max(results, key=score)[0]
    best_hits = max(r["hits"] for _, r in results)
    high = min((x for x in results if x[1]["hits"] == best_hits),
               key=lambda x: x[1]["alert_days_per_district_season"])[0]   # least alarming that still catches the most

    out_settings = {}
    for label, (q, mode, use) in [("balanced", bal), ("high_sensitivity", high)]:
        t, tr_ = thresholds(q)
        L = levels(t, tr_, use, mode)
        train, ptr = evaluate(L, "train")
        test, pte = evaluate(L, "test")
        out_settings[label] = {"percentile": q, "mode": mode, "signals": sorted(use), "thresholds": t, "river": tr_,
                               "train": train, "test": test, "per_event": {**ptr, **pte}}
        print(f"{label} (q={q}, {mode}, {sorted(use)}): TRAIN {train['hits']}/{train['pairs']}  TEST {test['hits']}/{test['pairs']} "
              f"no-flood alert days {test['alert_days_per_district_season']}")
    # single-signal comparison on the test years at the balanced setting
    t, tr_ = thresholds(bal[0])
    by_signal = {}
    for s in available:
        r, _ = evaluate(levels(t, tr_, {s}), "test")
        by_signal[s] = r
    base = {n: {d: (2 if v >= 115.6 else 0, {"forecast"}) for d, v in era[n].items()} for n in era}
    btrain, _ = evaluate(base, "train")
    btest, _ = evaluate(base, "test")

    def med(sigk, key, l, th):
        vals = sorted(th[sigk][n][key][l] for n in th[sigk])
        return vals[len(vals) // 2] if vals else None

    thr = {"version": f"v2 {date.today().isoformat()}", "signals": available, "settings": {}}
    for label, s in out_settings.items():
        th = s["thresholds"]
        thr["settings"][label] = {
            "percentile": s["percentile"], "mode": s["mode"], "signals": s["signals"],
            "rain": {"_default": {k: {l: med("forecast", k, l, th) for l in ["watch", "warning", "danger"]}
                                  for k in ["r1", "r3"]}, **th.get("forecast", {})},
            "satellite": th.get("satellite", {}), "imd": th.get("imd", {}),
            "gauge": GAUGE, "river": s["river"],
            "reservoir": {"watch": "90% full and rising"},
        }
    default = "balanced"
    thr["default_setting"] = default
    # keep the v1 shape for the live updater: top-level rain/river = default setting
    thr["rain"] = thr["settings"][default]["rain"]
    thr["river"] = thr["settings"][default]["river"]
    json.dump(thr, open(os.path.join(DATA, "thresholds.json"), "w"), indent=1)

    s = out_settings[default]
    hs = out_settings["high_sensitivity"]
    bt = {
        "generated": date.today().isoformat(),
        "signals": available,
        "setting": default,
        "summary": {"train": s["train"], "test": s["test"], "baseline": {"train": btrain, "test": btest},
                    "high_sensitivity": {"train": hs["train"], "test": hs["test"], "percentile": hs["percentile"], "mode": hs["mode"]},
                    "mode": s["mode"],
                    "by_signal_test": by_signal},
        "events": [{**{k: e[k] for k in ["id", "name", "start", "end", "set", "source"]},
                    "hit": s["per_event"].get(e["id"], ([], [], []))[0],
                    "missed": s["per_event"].get(e["id"], ([], [], []))[1],
                    "caught_by": s["per_event"].get(e["id"], ([], [], []))[2],
                    "hit_high": hs["per_event"].get(e["id"], ([], [], []))[0]} for e in events],
        "note": ("Backtest uses what was observed (rain gauges, satellite, reanalysis rainfall and river flow), "
                 "as if forecasts had been perfect, so live accuracy will be lower. A district flood counts as "
                 "caught if Warning or above was reached from the day before it began to its last day. "
                 "No-flood alert days are counted from July to January and may include real floods missing "
                 "from our list."),
    }
    json.dump(bt, open(os.path.join(DATA, "backtest.json"), "w"), indent=1)
    print("wrote thresholds.json and backtest.json")


if __name__ == "__main__":
    main()
