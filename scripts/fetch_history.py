"""
Download historical data from the extra sources into data_hist/.
Run on GitHub Actions (workflow "Fetch history") or locally:

    python scripts/fetch_history.py imd        # IMD gridded rainfall 2015 -> now
    python scripts/fetch_history.py imerg      # NASA IMERG 2015 -> now (needs EARTHDATA_USERNAME/PASSWORD)
    python scripts/fetch_history.py tnsmart    # TN-SMART rain-gauge and reservoir reports
"""

import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import HIST  # noqa: E402
import sources  # noqa: E402

START = date(2015, 1, 1)
TODAY = date.today()


def write_rows(path, header, rows):
    os.makedirs(HIST, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {os.path.relpath(path)}")


def grid_rows(gen):
    rows = []
    for day, res in gen:
        for name, (mean, mx) in sorted(res.items()):
            rows.append([day.isoformat(), name,
                         None if mean is None else round(mean, 2), None if mx is None else round(mx, 2)])
    return rows


def run_imd():
    rows = grid_rows(sources.imd_days(START, TODAY, "/tmp/imd"))
    write_rows(os.path.join(HIST, "imd_rain.csv"), ["date", "district", "mean_mm", "max_mm"], rows)


def run_imerg():
    user, pw = os.environ.get("EARTHDATA_USERNAME"), os.environ.get("EARTHDATA_PASSWORD")
    if not user or not pw:
        sys.exit("EARTHDATA_USERNAME / EARTHDATA_PASSWORD secrets are missing")
    os.makedirs("/tmp/imerg", exist_ok=True)
    path = os.path.join(HIST, "imerg_rain.csv")
    rows = grid_rows(sources.imerg_days(date(2015, 1, 1), date(2025, 12, 31), "final", user, pw, "/tmp/imerg"))
    done = {r[0] for r in rows}
    # months the research-quality run hasn't covered yet: fill from the "late" run
    gaps, d = [], date(2015, 1, 1)
    while d <= date(2025, 12, 31):
        if d.isoformat() not in done:
            gaps.append(d)
        d += timedelta(days=1)
    if gaps:
        print(f"{len(gaps)} days missing from the final run; filling from the late run")
        a = gaps[0]
        rows += [r for r in grid_rows(sources.imerg_days(a, gaps[-1], "late", user, pw, "/tmp/imerg"))
                 if r[0] not in done]
        done = {r[0] for r in rows}
    rows += [r for r in grid_rows(sources.imerg_days(date(2026, 1, 1), TODAY - timedelta(days=1), "early",
                                                     user, pw, "/tmp/imerg")) if r[0] not in done]
    write_rows(path, ["date", "district", "mean_mm", "max_mm"], sorted(rows))


def run_imerg_fill():
    """Fill a gap in imerg_rain.csv with the 'late' run: python fetch_history.py imerg_fill 2025-10-01 2025-12-31"""
    import csv as _csv
    user, pw = os.environ.get("EARTHDATA_USERNAME"), os.environ.get("EARTHDATA_PASSWORD")
    a, b = date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3])
    os.makedirs("/tmp/imerg", exist_ok=True)
    new = grid_rows(sources.imerg_days(a, b, "late", user, pw, "/tmp/imerg"))
    path = os.path.join(HIST, "imerg_rain.csv")
    old = list(_csv.reader(open(path)))[1:]
    have = {(r[0], r[1]) for r in old}
    rows = old + [r for r in new if (r[0], r[1]) not in have]
    write_rows(path, ["date", "district", "mean_mm", "max_mm"], sorted(rows))


def run_tnsmart():
    days = [START + timedelta(days=k) for k in range((TODAY - START).days + 1)]

    def rain(day):
        try:
            return day, sources.tnsmart_rain(day)
        except Exception as e:
            return day, f"error: {e}"

    def res(day):
        try:
            return day, sources.tnsmart_reservoirs(day)
        except Exception as e:
            return day, f"error: {e}"

    with ThreadPoolExecutor(8) as ex:
        rain_results = list(ex.map(rain, days))
        res_results = list(ex.map(res, days))
    rrows, errs = [], 0
    for day, r in rain_results:
        if isinstance(r, str):
            errs += 1
            continue
        for name, v in sorted((r or {}).items()):
            rrows.append([day.isoformat(), name, v["avg"], v["max"], v["gauges"]])
    print(f"TN-SMART rain: {len({r[0] for r in rrows})} days found, {errs} errors")
    write_rows(os.path.join(HIST, "tnsmart_rain.csv"), ["date", "district", "avg_mm", "max_gauge_mm", "gauges"], rrows)
    vrows = []
    for day, r in res_results:
        if isinstance(r, str):
            continue
        for v in r or []:
            vrows.append([day.isoformat(), v["name"], v["district"], v["storage_pct"], ";".join(v["downstream"])])
    print(f"TN-SMART reservoirs: {len({r[0] for r in vrows})} days found")
    write_rows(os.path.join(HIST, "tnsmart_reservoirs.csv"),
               ["date", "reservoir", "district", "storage_pct", "downstream"], vrows)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    import fetch_extra
    jobs = {"imd": run_imd, "imerg": run_imerg, "tnsmart": run_tnsmart, "imerg_fill": run_imerg_fill,
            "forecasts": fetch_extra.run_forecasts, "rivers": fetch_extra.run_rivers}
    for name, fn in jobs.items():
        if what == name or (what == "all" and name in ("imd", "imerg", "tnsmart")):
            print(f"===== {name} =====", flush=True)
            try:
                fn()
            except SystemExit as e:
                print(f"{name} stopped: {e}")
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"{name} failed: {e}")
