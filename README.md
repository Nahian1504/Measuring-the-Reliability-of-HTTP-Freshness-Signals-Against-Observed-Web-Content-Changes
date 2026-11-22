# Measuring the Reliability of HTTP Freshness Signals Against Observed Web Content Changes

This project downloads the same public web resources every 4 hours for 21 days, records their
HTTP freshness signals (Cache-Control, ETag, Last-Modified), detects when their content really
changes, measures how often the signals are wrong, and simulates what wrong signals cost caches
and crawlers.

## Requirements

* Windows 10/11, macOS or Linux, Python **3.12**
* About 5 GB of free disk space for the full study
* No accounts, keys or passwords are needed

## Setup (Windows, in a terminal opened in this folder)

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip freeze > requirements-lock.txt
python run.py test
```

On macOS/Linux use `python3.12 -m venv .venv` and `source .venv/bin/activate`.

## Commands

| Command | What it does | Module |
|---|---|---|
| `python run.py test` | runs all tests (CT1–CT6 and the T4 check) | all |
| `python run.py demo --db data/demo.db` | fake data, to see every output before real data exists | – |
| `python run.py sample` | picks the sites from the Tranco list | M1 |
| `python run.py collect` | one 4-hour round (Task Scheduler runs `collect.bat`) | M2, M3, M4 |
| `python run.py collect --hourly` | one hourly round for the 100 hourly resources | M2 |
| `python run.py status` | is collection healthy? | – |
| `python run.py pairs` | labels consecutive fetches (Table 1 categories) | M5 |
| `python run.py analyze` | RQ1 and RQ2 tables and figures | M5 |
| `python run.py simulate` | RQ3 policies and the T4 check | M6 |
| `python run.py all` | pairs + analyze + simulate | M5, M6 |
| `python run.py spotcheck` / `spotcheck-score` | 50-pair manual check of the detector | M4 |
| `python run.py dashboard` | builds `results/dashboard.html`, the interactive results dashboard (T5) | T5 |
| `python run.py export` | CSV files to publish (headers, hashes, metrics — no page text) | – |
| `python run.py testserver` | local test server with known changes and faults | M7 |

Add `--db data/pilot.db` to any command to use another database (for example the pilot).
All settings (Tranco list ID, seed 42, 4-hour interval, timeouts, theta) are in `config.yaml`.
The design document used `make test`, `make collect`, … — on Windows these are `python run.py test`,
`python run.py collect`, and so on.

## Project layout

```
config.yaml            all settings
run.py                 one entry point for every step
collect.bat            Task Scheduler: 4-hour round
collect_hourly.bat     Task Scheduler: hourly round
status.bat             double-click to check collection
freshness/
  sampler.py   M1  site sampler (Tranco, robots.txt, resource discovery)
  fetcher.py   M2  polite fetcher (aiohttp, 50 at once, 1 per site, 2 s gap)
  parser.py    M3  header parser (Cache-Control, ETag, dates, RFC 9111 lifetime)
  detector.py  M4  meaningful-change detector (trafilatura + 5-word-shingle MinHash)
  pairs.py     M5  signal classifier (flags B, M, E, L and the Table 1 categories)
  analysis.py  M5  analyzer (RQ1, RQ2, bootstrap intervals, figures)
  simulator.py M6  trace simulator (four policies, staleness and cost)
  dashboard.py T5  results dashboard (one self-contained HTML file)
  testserver.py M7 local test server
  db.py            SQLite schema (Figure 2)
tests/               CT1–CT6 and T4 tests (pytest)
data/                database and Tranco list (not in Git)
results/             tables (CSV) and figures (PNG)
```

## Ethics

The crawler identifies itself with a user agent that links to this repository, obeys robots.txt
(re-checked daily), sends at most one request at a time per site with a 2-second gap, and does not
bypass bot protection. Sites that ask to be excluded are added to `blocked_domains` in `config.yaml`.
Only headers, hashes and metrics are published, not page text.

