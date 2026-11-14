import csv
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from monitor import (
    CSV_FIELDS,
    MAX_REDIRECTS,
    TARGET_URL,
    _bounded_get,
    build_summary,
    daily_statistics,
    main,
    perform_check,
    update_daily_report,
)


class MonitorCheckTests(unittest.TestCase):
    def test_success_records_status_and_uses_one_bounded_get(self):
        response = Mock(status_code=200, reason="OK")
        get = Mock(return_value=response)

        record = perform_check(get=get)

        get.assert_called_once_with(
            TARGET_URL,
            timeout=(5, 15),
            allow_redirects=True,
        )
        self.assertEqual(record["availability_status"], "UP")
        self.assertEqual(record["http_status_code"], 200)
        self.assertEqual(record["error_description"], "")
        self.assertGreaterEqual(record["response_time_ms"], 0)
        self.assertTrue(record["timestamp_utc"].endswith("Z"))

    def test_client_http_error_is_not_available(self):
        record = perform_check(
            get=Mock(return_value=Mock(status_code=404, reason="Not Found"))
        )

        self.assertEqual(record["availability_status"], "DOWN")
        self.assertEqual(record["http_status_code"], 404)
        self.assertIn("client error", record["error_description"])

    def test_server_http_error_is_distinguished(self):
        record = perform_check(
            get=Mock(return_value=Mock(status_code=503, reason="Unavailable"))
        )

        self.assertEqual(record["availability_status"], "DOWN")
        self.assertEqual(record["http_status_code"], 503)
        self.assertIn("server error", record["error_description"])

    def test_timeout_has_no_http_status(self):
        get = Mock(side_effect=requests.Timeout("slow response"))

        record = perform_check(get=get)

        get.assert_called_once()
        self.assertEqual(record["availability_status"], "DOWN")
        self.assertEqual(record["http_status_code"], "")
        self.assertIn("timed out", record["error_description"])

    def test_connection_failure_is_distinguished_from_http_error(self):
        record = perform_check(
            get=Mock(side_effect=requests.ConnectionError("connection refused"))
        )

        self.assertEqual(record["availability_status"], "DOWN")
        self.assertEqual(record["http_status_code"], "")
        self.assertIn("Connection failure", record["error_description"])

    def test_redirect_limit_is_finite(self):
        self.assertEqual(MAX_REDIRECTS, 5)

    def test_http_session_makes_one_get_with_a_finite_redirect_limit(self):
        with patch("monitor.requests.Session") as session_factory:
            session = session_factory.return_value.__enter__.return_value
            _bounded_get(
                TARGET_URL,
                timeout=(5, 15),
                allow_redirects=True,
            )

        self.assertEqual(session.max_redirects, 5)
        session.get.assert_called_once_with(
            TARGET_URL,
            timeout=(5, 15),
            allow_redirects=True,
        )


class ReportTests(unittest.TestCase):
    def test_csv_and_daily_statistics_include_success_and_failure(self):
        first = {
            "timestamp_utc": "2026-11-14T10:00:00Z",
            "target_url": TARGET_URL,
            "http_status_code": 200,
            "response_time_ms": 100.0,
            "availability_status": "UP",
            "error_description": "",
        }
        second = {
            **first,
            "timestamp_utc": "2026-11-14T10:05:00Z",
            "http_status_code": 503,
            "response_time_ms": 300.0,
            "availability_status": "DOWN",
            "error_description": "HTTP 503 Service Unavailable (server error)",
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catering-monitor-report.csv"
            rows = update_daily_report(path, first)
            rows = update_daily_report(path, second)

            with path.open(newline="", encoding="utf-8") as csv_file:
                parsed = list(csv.DictReader(csv_file))

        self.assertEqual(len(parsed), 2)
        self.assertEqual(tuple(parsed[0].keys()), CSV_FIELDS)
        self.assertEqual(parsed[1]["http_status_code"], "503")
        self.assertEqual(
            daily_statistics(rows),
            {
                "total_checks": 2,
                "successful_checks": 1,
                "failed_checks": 1,
                "success_percentage": 50.0,
                "average_response_time_ms": 200.0,
            },
        )

    def test_report_discards_rows_from_a_different_utc_day(self):
        old_record = {
            "timestamp_utc": "2026-11-13T23:55:00Z",
            "target_url": TARGET_URL,
            "http_status_code": 200,
            "response_time_ms": 100,
            "availability_status": "UP",
            "error_description": "",
        }
        today_record = {**old_record, "timestamp_utc": "2026-11-14T00:00:00Z"}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.csv"
            with path.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerow(old_record)

            rows = update_daily_report(path, today_record)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timestamp_utc"], today_record["timestamp_utc"])

    def test_job_summary_contains_daily_statistics(self):
        record = {
            "timestamp_utc": "2026-11-14T10:05:00Z",
            "target_url": TARGET_URL,
            "http_status_code": 503,
            "response_time_ms": 300.0,
            "availability_status": "DOWN",
            "error_description": "HTTP 503 Service Unavailable (server error)",
        }
        summary = build_summary(
            record,
            {
                "total_checks": 2,
                "successful_checks": 1,
                "failed_checks": 1,
                "success_percentage": 50.0,
                "average_response_time_ms": 200.0,
            },
        )

        self.assertIn("HTTP status:** 503", summary)
        self.assertIn("| 2 | 1 | 1 | 50.00% | 200.00 ms |", summary)

    def test_failed_check_still_writes_job_summary_and_returns_failure(self):
        record = {
            "timestamp_utc": "2026-11-14T10:05:00Z",
            "target_url": TARGET_URL,
            "http_status_code": "",
            "response_time_ms": 15000.0,
            "availability_status": "DOWN",
            "error_description": "Request timed out",
        }
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.csv"
            summary_path = Path(directory) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path)}):
                with patch("monitor.perform_check", return_value=record):
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = main(["--report", str(report_path)])
            summary = summary_path.read_text(encoding="utf-8")
            self.assertTrue(report_path.exists())

        self.assertEqual(result, 1)
        self.assertIn("Availability:** DOWN", summary)


if __name__ == "__main__":
    unittest.main()
