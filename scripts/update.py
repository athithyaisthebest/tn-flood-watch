"""
Tamil Nadu Flood Watch - live updater.

Runs every 3 hours on GitHub Actions. Fetches:
  * rainfall: past 3 days + next 3 days per district (Open-Meteo forecast API)
  * river flow: past 3 days + next 7 days at 16 river points (Open-Meteo Flood API / GloFAS)
Compares them with the thresholds in docs/data/thresholds.json and writes
docs/data/risk.json (what the website shows) plus a monthly history file.

No API key is needed. Run locally with:  python scripts/update.py
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
IST = timezone(timedelta(hours=5, minutes=30))
LEVELS = ["Low", "Watch", "Warning", "Danger"]


def load(name):
    with open(os.path.join(DATA, name)) as f:
        return json.load(f)


def get(url, params):
    for attempt in range(5):
        try:
            r = requests.get(url, params=params, timeout=120)
            if r.status_code == 200:
                d = r.json()
                return d if isinstance(d, list) else [d]
            print(f"HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"network error: {e}", file=sys.stderr)
        time.sleep(10 * (attempt + 1))
    return None


def level_for(value, thr):
    """thr = {'watch': x, 'warning': y, 'danger': z}; returns 0..3"""
    if value is None or thr is None:
        return 0
    lvl = 0
    for i, k in enumerate(["watch", "warning", "danger"], start=1):
        if thr.get(k) is not None and value >= thr[k]:
            lvl = i
    return lvl


def _raise(d, signal, lvl, reason):
    if lvl <= 0:
        return
    d.setdefault("signals", {})
    d["signals"][signal] = max(d["signals"].get(signal, 0), lvl)
    d["reasons"].append(f"{LEVELS[lvl]}: {reason}")


def add_observed(out, today, thr):
    """Add what has actually been observed: TN-SMART gauges and reservoirs, NASA satellite rain.
    Each source is optional: if it fails, the site still works on the others."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    ok = {}
    try:
        import sources
    except Exception as e:
        print("observed sources unavailable:", e)
        return {"gauges": False, "reservoirs": False, "satellite": False}
    setting = thr.get("settings", {}).get(thr.get("default_setting", ""), {})
    gauge_t = setting.get("gauge", {"watch": 64.5, "warning": 115.6, "danger": 204.5})

    # rain gauges: today's report (8:30 am yesterday -> 8:30 am today), else yesterday's
    try:
        rep, used = None, None
        for back in (0, 1):
            rep = sources.tnsmart_rain(today - timedelta(days=back))
            if rep:
                used = today - timedelta(days=back)
                break
        ok["gauges"] = bool(rep)
        for n, v in (rep or {}).items():
            if n in out and v.get("max"):
                _raise(out[n], "gauge", level_for(v["max"], gauge_t),
                       f"a rain gauge recorded {v['max']:.0f} mm in the 24 hours to 8:30 am {used:%d %b}")
            if n in out:
                out[n]["gauge"] = {"report_date": used.isoformat(), "avg_mm": v.get("avg"), "max_mm": v.get("max")}
    except Exception as e:
        print("gauges failed:", e)
        ok["gauges"] = False

    # reservoirs: compare today's (or yesterday's) report with the day before
    try:
        cur, prev = None, None
        for back in (0, 1, 2):
            r = sources.tnsmart_reservoirs(today - timedelta(days=back))
            if r and cur is None:
                cur = r
            elif r and prev is None:
                prev = r
                break
        ok["reservoirs"] = bool(cur)
        before = {x["name"]: x["storage_pct"] for x in (prev or [])}
        for x in cur or []:
            p, b = x["storage_pct"], before.get(x["name"])
            rising = b is not None and p > b
            lvl = 2 if (p >= 95 and rising) else (1 if (p >= 90 and rising) else 0)
            for n in x["downstream"]:
                if n in out:
                    out[n].setdefault("reservoirs", []).append({"name": x["name"], "storage_pct": p, "rising": rising})
                    _raise(out[n], "reservoir", lvl, f"{x['name']} reservoir is {p:.0f}% full and rising")
    except Exception as e:
        print("reservoirs failed:", e)
        ok["reservoirs"] = False

    # IMD gridded rain-gauge analysis (published daily), wettest square in each district, last 3 days
    imd_t = setting.get("imd", {})
    if imd_t:
        try:
            days = dict(sources.imd_days(today - timedelta(days=3), today, "/tmp/imd"))
            ok["imd"] = bool(days)
            for n in out:
                t = imd_t.get(n)
                series = [days[k][n][1] for k in sorted(days) if n in days[k] and days[k][n][1] is not None]
                if not t or not series:
                    continue
                r1, r3 = series[-1], sum(series[-3:])
                lvl = max(level_for(r1, t["r1"]), level_for(r3, t["r3"]))
                out[n]["imd"] = {"last_day_mm": round(r1, 1), "three_day_mm": round(r3, 1), "date": max(days).isoformat()}
                _raise(out[n], "imd", lvl, f"IMD's rain-gauge analysis shows {r3:.0f} mm over 3 days to {max(days):%d %b}")
        except Exception as e:
            print("IMD failed:", e)
            ok["imd"] = False

    # satellite: NASA IMERG Early daily rain, wettest spot in each district, last 3 days
    user, pw = os.environ.get("EARTHDATA_USERNAME"), os.environ.get("EARTHDATA_PASSWORD")
    sat_t = setting.get("satellite", {})
    if user and pw and sat_t:
        try:
            days = {}
            for day, res in sources.imerg_days(today - timedelta(days=3), today - timedelta(days=1),
                                               "early", user, pw, "/tmp"):
                days[day] = res
            ok["satellite"] = bool(days)
            for n in out:
                t = sat_t.get(n)
                series = [days[k][n][1] for k in sorted(days) if n in days[k] and days[k][n][1] is not None]
                if not t or not series:
                    continue
                r1, r3 = series[-1], sum(series[-3:])
                lvl = max(level_for(r1, t["r1"]), level_for(r3, t["r3"]))
                out[n]["satellite"] = {"last_day_mm": round(r1, 1), "three_day_mm": round(r3, 1)}
                _raise(out[n], "satellite", lvl, f"satellites measured {r3:.0f} mm over the last 3 days")
        except Exception as e:
            print("satellite failed:", e)
            ok["satellite"] = False
    else:
        ok["satellite"] = False
    return ok


def main():
    points = load("points.json")
    rep = {}
    for p in points:          # the last point listed for a district is its representative point
        rep[p["district"]] = (p["lat"], p["lon"])
    rivers = {k: v for k, v in load("rivers.json").items() if v.get("use", True)}
    thr = load("thresholds.json")
    names = sorted(rep)
    now = datetime.now(IST)
    today = now.date()

    # ---------------- rainfall
    rain = {}
    ok_rain = True
    for i in range(0, len(names), 10):
        chunk = names[i:i + 10]
        res = get("https://api.open-meteo.com/v1/forecast", {
            "latitude": ",".join(str(rep[n][0]) for n in chunk),
            "longitude": ",".join(str(rep[n][1]) for n in chunk),
            "daily": "precipitation_sum", "past_days": 3, "forecast_days": 4,
            "timezone": "Asia/Kolkata",
        })
        if res is None:
            ok_rain = False
            continue
        for n, r in zip(chunk, res):
            rain[n] = dict(zip(r["daily"]["time"], r["daily"]["precipitation_sum"]))

    # ---------------- river flow
    flow = {}
    ok_river = True
    rnames = list(rivers)
    for i in range(0, len(rnames), 10):
        chunk = rnames[i:i + 10]
        res = get("https://flood-api.open-meteo.com/v1/flood", {
            "latitude": ",".join(str(rivers[n]["lat"]) for n in chunk),
            "longitude": ",".join(str(rivers[n]["lon"]) for n in chunk),
            "daily": "river_discharge", "past_days": 3, "forecast_days": 7,
        })
        if res is None:
            ok_river = False
            continue
        for n, r in zip(chunk, res):
            flow[n] = dict(zip(r["daily"]["time"], r["daily"]["river_discharge"]))

    # ---------------- assess each district
    days = [(today + timedelta(days=k)).isoformat() for k in range(0, 3)]  # today, +1, +2
    out = {}
    for n in names:
        series = rain.get(n)
        d = {"level": None, "level_name": "No data", "reasons": [], "rain": {}, "rivers": []}
        if series:
            def v(day):
                x = series.get(day)
                return 0.0 if x is None else float(x)
            best = 0
            for day in days:
                dt = datetime.fromisoformat(day).date()
                r1 = v(day)
                r3 = sum(v((dt - timedelta(days=k)).isoformat()) for k in range(3))
                t = thr["rain"].get(n, thr["rain"].get("_default"))
                l1 = level_for(r1, t.get("r1"))
                l3 = level_for(r3, t.get("r3"))
                lvl = max(l1, l3)
                d["rain"][day] = {"r1": round(r1, 1), "r3": round(r3, 1), "level": lvl}
                if lvl > best:
                    best = lvl
                    which = "1-day" if l1 >= l3 else "3-day"
                    amount = r1 if l1 >= l3 else r3
                    lead = (dt - today).days
                    when = "today" if lead == 0 else ("tomorrow" if lead == 1 else f"in {lead} days")
                    d["reasons"] = [f"{LEVELS[lvl]}: {which} rainfall of {amount:.0f} mm expected {when}"]
            d["level"] = best
        # rivers in this district
        for rn, info in rivers.items():
            if info["district"] != n or rn not in flow:
                continue
            q = [x for day, x in flow[rn].items() if day >= today.isoformat() and x is not None][:7]
            peak = max(q) if q else None
            rl = level_for(peak, thr["river"].get(rn))
            d["rivers"].append({"name": rn, "peak_7d_m3s": None if peak is None else round(peak),
                                "level": rl})
            if rl and (d["level"] is None or rl >= d["level"]):
                d["reasons"].append(f"{LEVELS[rl]}: {rn} forecast to reach {peak:.0f} m³/s")
            if d["level"] is not None:
                d["level"] = max(d["level"], rl)
        d["signals"] = {"forecast": d["level"] or 0}
        out[n] = d

    # ---------------- observed signals: rain gauges, reservoirs, satellite
    ok_obs = add_observed(out, today, thr)
    for d in out.values():
        if d["level"] is not None:
            d["level"] = max([d["level"]] + list(d["signals"].values()))
            d["level_name"] = LEVELS[d["level"]]

    risk = {
        "updated": now.isoformat(timespec="minutes"),
        "sources_ok": {"rain": ok_rain, "river": ok_river, **ok_obs},
        "thresholds_version": thr.get("version"),
        "districts": out,
    }
    with open(os.path.join(DATA, "risk.json"), "w") as f:
        json.dump(risk, f, indent=1)

    # ---------------- history (one row per district per run)
    hist_dir = os.path.join(DATA, "history")
    os.makedirs(hist_dir, exist_ok=True)
    path = os.path.join(hist_dir, f"{now:%Y-%m}.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["run_time", "district", "level", "r1_today", "r1_tomorrow", "r1_day2", "reason"])
        for n, d in out.items():
            r = [d["rain"].get(day, {}).get("r1") for day in days]
            w.writerow([risk["updated"], n, d["level"], *r, "; ".join(d["reasons"])])
    counts = {}
    for d in out.values():
        counts[d["level_name"]] = counts.get(d["level_name"], 0) + 1
    print("Updated", risk["updated"], counts)


if __name__ == "__main__":
    main()
