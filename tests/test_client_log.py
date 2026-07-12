"""The /api/client_log beacon: the SPA reports client-perceived fetch stalls
here so network/path hangs land in the same log stream as server request
timing. We only care that it accepts the report and logs at the right level."""

import logging

from fastapi.testclient import TestClient


def test_client_log_accepts_report(client: TestClient) -> None:
    resp = client.post(
        "/api/client_log",
        json={"url": "/api/session/next", "outcome": "slow", "duration_ms": 2400},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_slow_report_logs_at_info(client: TestClient, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="kana_quiz.api"):
        client.post(
            "/api/client_log",
            json={"url": "/api/session/next", "outcome": "retry", "duration_ms": 1800, "attempts": 2},
        )
    rec = next(r for r in caplog.records if "client retry" in r.getMessage())
    assert rec.levelno == logging.INFO
    assert "attempts=2" in rec.getMessage()


def test_timeout_report_logs_at_warning(client: TestClient, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="kana_quiz.api"):
        client.post(
            "/api/client_log",
            json={
                "url": "/api/session/answer",
                "outcome": "timeout",
                "duration_ms": 6000,
                "attempts": 4,
                "detail": "AbortError",
            },
        )
    rec = next(r for r in caplog.records if "client timeout" in r.getMessage())
    assert rec.levelno == logging.WARNING
    assert "AbortError" in rec.getMessage()
