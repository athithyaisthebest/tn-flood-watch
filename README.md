# Tamil Nadu Flood Watch

A free, open, district-by-district flood risk map for Tamil Nadu, for the next three days.
Updated every three hours from open rainfall and river-flow forecasts.

**Not an official warning.** Always follow TNSDMA, IMD and your District Collector.

## How it works

1. Every three hours a GitHub Action runs `scripts/update.py`.
2. It fetches the past 3 days and next 3 days of rainfall for each of the 38 districts, and
   7-day river-flow forecasts for 16 river points, from [Open-Meteo](https://open-meteo.com/) (no key needed).
3. It compares 1-day and 3-day rain totals and river flow with each district's thresholds in
   `docs/data/thresholds.json`, and writes `docs/data/risk.json`.
4. The website in `docs/` (served by GitHub Pages) draws the map from that file.
5. Every run is also saved in `docs/data/history/`, so the live track record can be checked later.

## Thresholds and backtest

`scripts/backtest.py` sets each district's thresholds from its own rainfall history (2015 to mid-2022)
and tests them on recorded floods in `docs/data/flood_events.json`, including floods from 2022 onwards
that were not used for tuning. Results appear on the site's Track record section.

```
python download_history.py           # downloads tn_flood_history/ (about 15-70 min)
python scripts/backtest.py tn_flood_history
```

## Folder layout

```
docs/                 the website (GitHub Pages serves this folder)
  index.html
  data/               districts, thresholds, live risk, flood events, backtest results, history
scripts/update.py     live updater (run by GitHub Actions)
scripts/backtest.py   tunes thresholds and tests them on past floods
download_history.py   downloads historical data
.github/workflows/update.yml   the 3-hourly schedule
```

## Limitations

- Uses one rainfall point per district, so very local downpours can be missed.
- Does not model urban drainage, dam or lake releases, tank breaches or storm surge.
- The backtest uses observed rainfall, as if the forecast had been perfect; live accuracy will be lower.
- The flood events list is incomplete, so some "false alarms" may be real, unrecorded floods.

## Credits

Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0). River data: Copernicus Emergency
Management Service, GloFAS. District boundaries: [datta07/INDIAN-SHAPEFILES](https://github.com/datta07/INDIAN-SHAPEFILES).

A student project by Athithya, built with help from AI tools.
