# Catering Website Monitor

A small, free-to-run GitHub Actions monitor for
`http://38.242.209.134:8081/CATERING/`. It checks availability and response
performance; it does not generate visitors, submit forms, or create artificial
traffic.

## Repository structure

```text
.
├── .github/
│   └── workflows/
│       └── catering-monitor.yml
├── tests/
│   └── test_monitor.py
├── monitor.py
└── requirements.txt
```

## How monitoring works

- GitHub Actions schedules one workflow run every five minutes (UTC), and a
  manual `workflow_dispatch` run is also available.
- Each run makes one health-check GET call, follows redirects (up to five), and
  uses a 5-second connection timeout and a 15-second response timeout. There
  are no retry loops.
- HTTP 2xx and 3xx responses count as available. HTTP 4xx and 5xx responses
  count as failed and are labeled as client or server errors. Timeouts and
  connection/request failures are recorded separately.
- Runs share a concurrency group so monitoring jobs do not run simultaneously.
- The UTC-day CSV accumulates checks using GitHub Actions cache. The workflow
  uploads the current day's CSV as a downloadable artifact for each run; it
  does not commit reports back to the repository. Artifacts are retained for
  14 days.
- The job summary shows the latest result and daily totals, success percentage,
  and average response time. A failed check marks the workflow run as failed,
  but the CSV and summary are still saved/uploaded when available.

GitHub may delay scheduled workflows, and its cache can be evicted. If the
daily cache is unavailable, that day's cumulative totals restart with the next
check; the individual run artifacts remain downloadable for their retention
period.

## Enable GitHub Actions

1. Open the repository's **Actions** tab.
2. If prompted, enable GitHub Actions for the repository.
3. Confirm scheduled workflows are allowed by repository or organization
   policy. No API key, paid monitoring service, or additional secret is needed.
4. After the workflow is present on the default branch, scheduled checks begin
   automatically. GitHub can take several minutes to start a newly enabled
   scheduled workflow.

## Run a manual check

1. Open **Actions** → **Catering Monitor**.
2. Select **Run workflow**, choose the default branch, then confirm.
3. Open the resulting run to see its job summary and downloadable artifact.

For a local one-off check, install Python 3 and the dependency, then run:

```bash
python -m pip install -r requirements.txt
python monitor.py --report catering-monitor-report.csv
```

The local command makes one real GET request to the configured target. Unit
tests use mocked responses and do not contact the target:

```bash
python -m unittest discover -s tests -v
```

## Download CSV reports

Open a completed workflow run and download the
`catering-monitor-report-<run>-<attempt>` artifact from the **Artifacts**
section. Each artifact contains the cumulative CSV for that run's UTC day.
Artifacts expire after 14 days.

CSV columns:

| Column | Meaning |
| --- | --- |
| `timestamp_utc` | Check start time in UTC, ISO 8601 format |
| `target_url` | Monitored URL |
| `http_status_code` | Final HTTP status, blank if no HTTP response arrived |
| `response_time_ms` | Elapsed time for the request, including redirects |
| `availability_status` | `UP` for HTTP 2xx/3xx; otherwise `DOWN` |
| `error_description` | HTTP error category or connection/request failure detail |

## Understand HTTP status codes

- **2xx:** the server processed the request; treated as available.
- **3xx:** redirect response; redirects are followed up to five hops. A final
  3xx is treated as available.
- **4xx:** client/request error (for example, 404 Not Found); treated as a
  failed check because the monitored endpoint did not return a successful
  response.
- **5xx:** server-side error (for example, 503 Service Unavailable); treated
  as a failed check.
- **Blank status:** no HTTP response was received, typically because of a
  timeout or connection failure.

The workflow run is marked failed when the check is `DOWN`; inspect the summary
and CSV for the recorded cause. This does not assert that the website is online
or offline until a check has actually run.

## Change the monitoring schedule

Edit the `cron` expression under `on.schedule` in
`.github/workflows/catering-monitor.yml`. GitHub Actions cron schedules use
UTC; for example, `*/10 * * * *` requests a run every ten minutes. GitHub
Actions may start scheduled runs late during high load.
