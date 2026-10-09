"""Behavioral verification for incident confirmation and runbook failure paths."""
import itertools
import json
from types import SimpleNamespace

import httpx
import pytest

from dr import runbook


def setup_run(monkeypatch, primary_ready, target_alive):
    ticks = itertools.count(1000000, 1)
    monkeypatch.setattr(runbook.time, "time", lambda: next(ticks))
    monkeypatch.setattr(runbook.time, "sleep", lambda _: None)
    monkeypatch.setattr(runbook, "step", lambda *args, **kwargs: kwargs)

    def probe(url, **kwargs):
        ready = primary_ready if url.endswith("/readyz") else target_alive
        return httpx.Response(200 if ready else 503)

    monkeypatch.setattr(runbook.httpx, "get", probe)
    calls = []

    def failover(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "state": {"count": 200, "weights": True}}

    monkeypatch.setattr(runbook.fo, "failover", failover)
    return calls


def test_healthy_primary_does_not_failover(monkeypatch):
    calls = setup_run(monkeypatch, True, True)
    result = runbook.run("a", "b", "fs", True)
    assert result == {"ok": False, "error": "primary_still_ready"}
    assert calls == []


def test_unreachable_target_does_not_failover(monkeypatch):
    calls = setup_run(monkeypatch, False, False)
    result = runbook.run("a", "b", "fs", True)
    assert result == {"ok": False, "error": "target_unreachable"}
    assert calls == []


def test_same_region_is_rejected(monkeypatch):
    calls = setup_run(monkeypatch, False, True)
    assert runbook.run("a", "a", "fs", True)["error"] == "invalid_regions"
    assert calls == []


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_golden_signals_are_not_reported_as_success(monkeypatch, timeout):
    calls = setup_run(monkeypatch, False, True)

    class Client:
        def __init__(self, **kwargs):
            self.requests = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            assert self.requests == 10

        def get(self, *args, **kwargs):
            self.requests += 1
            if timeout:
                raise httpx.ReadTimeout("probe timed out")
            return httpx.Response(503)

    monkeypatch.setattr(runbook.httpx, "Client", Client)
    result = runbook.run("a", "b", "fs", True)
    assert len(calls) == 1
    assert result["ok"] is False
    assert result["error_rate"] == 1.0
    if timeout:
        assert result["p95_latency_ms"] is None


def test_single_failure_after_success_does_not_confirm_outage(monkeypatch):
    calls = setup_run(monkeypatch, False, True)
    replies = iter([200, 503])
    monkeypatch.setattr(runbook.httpx, "get", lambda url, **kwargs:
                        httpx.Response(next(replies) if url.endswith('/readyz') else 200))
    assert runbook.run('a', 'b', 'fs', True)['error'] == 'outage_not_confirmed'
    assert calls == []


def test_recovered_health_alert_does_not_confirm_one_failure(monkeypatch):
    calls = setup_run(monkeypatch, False, True)

    class HealthLog:
        def exists(self):
            return True

        def read_text(self):
            return '\n'.join(json.dumps(record) for record in [
                {'event': 'state_change', 'region': 'a', 'to': 'UNHEALTHY',
                 'ts': 999999, 'consecutive_fails': 3},
                {'event': 'state_change', 'region': 'a', 'to': 'HEALTHY',
                 'ts': 1000000, 'consecutive_fails': 0},
            ])

    monkeypatch.setattr(runbook, 'pathlib', SimpleNamespace(Path=lambda _: HealthLog()))
    replies = iter([503, 200, 503])
    monkeypatch.setattr(runbook.httpx, 'get', lambda url, **kwargs:
                        httpx.Response(next(replies) if url.endswith('/readyz') else 200))
    assert runbook.run('a', 'b', 'fs', True)['error'] == 'outage_not_confirmed'
    assert calls == []


def test_declined_confirmation_does_not_call_failover(monkeypatch):
    calls = setup_run(monkeypatch, False, True)
    monkeypatch.setattr(runbook, 'confirm', lambda auto, message: False)
    assert runbook.run('a', 'b', 'fs', False) == {'ok': False, 'aborted': True}
    assert calls == []


@pytest.mark.parametrize('answer, expected', [('', False), ('n', False), ('y', True)])
def test_confirmation_requires_explicit_yes(monkeypatch, answer, expected):
    monkeypatch.setattr('builtins.input', lambda _: answer)
    assert runbook.confirm(False, 'Failover?') is expected
