"""Lightweight, single-request-per-run catering website monitor."""

from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

TARGET_URL = "http://38.242.209.134:8081/CATERING/"
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15
MAX_REDIRECTS = 5
DEFAULT_REPORT = Path("catering-monitor-report.csv")
CSV_FIELDS = (
    "timestamp_utc",
    "target_url",
    "http_status_code",
    "response_time_ms",
    "availability_status",
    "error_description",
)


def _bounded_get(url: str, **kwargs: Any) -> requests.Response:
    """Make one GET call with bounded redirect following and no retry adapter."""
    with requests.Session() as session:
        session.max_redirects = MAX_REDIRECTS
        return session.get(url, **kwargs)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _exception_message(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    return message[:300] or type(exc).__name__


def perform_check(
    target_url: str = TARGET_URL,
    get: Callable[..., requests.Response] = _bounded_get,
) -> dict[str, Any]:
    """Perform one health-check call and return a CSV-ready record."""
    timestamp = _utc_timestamp()
    started = time.perf_counter()
    status_code: int | None = None
    availability = "DOWN"
    error = ""

    try:
        response = get(
            target_url,
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            allow_redirects=True,
        )
        status_code = int(response.status_code)
        reason = " ".join(str(getattr(response, "reason", "") or "").split())

        if 200 <= status_code < 400:
            availability = "UP"
        elif 400 <= status_code < 500:
            error = f"HTTP {status_code} {reason or 'Client Error'} (client error)"
        elif 500 <= status_code < 600:
            error = f"HTTP {status_code} {reason or 'Server Error'} (server error)"
        else:
            error = f"Unexpected HTTP status {status_code}"
    except requests.Timeout:
        error = (
            "Request timed out "
            f"(connect timeout {CONNECT_TIMEOUT_SECONDS}s; "
            f"read timeout {READ_TIMEOUT_SECONDS}s)"
        )
    except requests.TooManyRedirects:
        error = f"Redirect limit exceeded ({MAX_REDIRECTS} redirects)"
    except requests.ConnectionError as exc:
        error = f"Connection failure: {_exception_message(exc)}"
    except requests.RequestException as exc:
        error = f"Request failure: {_exception_message(exc)}"

    response_time_ms = round((time.perf_counter() - started) * 1000, 2)
    return {
        "timestamp_utc": timestamp,
        "target_url": target_url,
        "http_status_code": "" if status_code is None else status_code,
        "response_time_ms": response_time_ms,
        "availability_status": availability,
        "error_description": error,
    }


def update_daily_report(report_path: Path, current_record: dict[str, Any]) -> list[dict[str, str]]:
    """Append a record and keep only records from its UTC calendar day."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    current_day = str(current_record["timestamp_utc"])[:10]
    records: list[dict[str, str]] = []

    if report_path.exists() and report_path.stat().st_size:
        with report_path.open("r", newline="", encoding="utf-8") as report_file:
            reader = csv.DictReader(report_file)
            if tuple(reader.fieldnames or ()) != CSV_FIELDS:
                raise ValueError(f"{report_path} has an unexpected CSV header")
            records.extend(
                row
                for row in reader
                if row.get("timestamp_utc", "").startswith(current_day)
            )

    records.append({field: str(current_record[field]) for field in CSV_FIELDS})
    with report_path.open("w", newline="", encoding="utf-8") as report_file:
        writer = csv.DictWriter(report_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(records)
    return records


def daily_statistics(records: list[dict[str, Any]]) -> dict[str, Any]:
    successful = sum(record["availability_status"] == "UP" for record in records)
    total = len(records)
    response_times = [float(record["response_time_ms"]) for record in records]
    average = round(sum(response_times) / len(response_times), 2) if response_times else None
    return {
        "total_checks": total,
        "successful_checks": successful,
        "failed_checks": total - successful,
        "success_percentage": round((successful / total) * 100, 2) if total else 0.0,
        "average_response_time_ms": average,
    }


def _markdown_safe(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def build_summary(record: dict[str, Any], statistics: dict[str, Any]) -> str:
    average = statistics["average_response_time_ms"]
    average_text = "n/a" if average is None else f"{average:.2f} ms"
    status_code = record["http_status_code"] or "—"
    error = record["error_description"] or "—"
    return "\n".join(
        (
            "## Catering website monitor",
            "",
            f"- **UTC day:** {_markdown_safe(str(record['timestamp_utc'])[:10])}",
            f"- **Target:** {_markdown_safe(record['target_url'])}",
            f"- **Latest check:** {_markdown_safe(record['timestamp_utc'])}",
            f"- **Availability:** {_markdown_safe(record['availability_status'])}",
            f"- **HTTP status:** {_markdown_safe(status_code)}",
            f"- **Response time:** {_markdown_safe(record['response_time_ms'])} ms",
            f"- **Error:** {_markdown_safe(error)}",
            "",
            "### Daily statistics (UTC)",
            "",
            "| Checks | Successful | Failed | Success rate | Average response time |",
            "| ---: | ---: | ---: | ---: | ---: |",
            (
                f"| {statistics['total_checks']} | "
                f"{statistics['successful_checks']} | "
                f"{statistics['failed_checks']} | "
                f"{statistics['success_percentage']:.2f}% | {average_text} |"
            ),
            "",
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="CSV file to append today's UTC checks to",
    )
    args = parser.parse_args(argv)

    record = perform_check()
    records = update_daily_report(args.report, record)
    summary = build_summary(record, daily_statistics(records))
    print(summary, end="")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary_file:
            summary_file.write(summary)

    return 0 if record["availability_status"] == "UP" else 1


if __name__ == "__main__":
    raise SystemExit(main())
