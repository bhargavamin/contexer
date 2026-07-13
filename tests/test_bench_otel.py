"""Embedded OTLP/HTTP JSON receiver used to cross-check session numbers."""
import json
import urllib.request

import pytest

from benchmarks.otel import OtelReceiver


def _payload(token_points, cost=0.05):
    def dp(val, typ):
        return {"asInt": str(val),
                "attributes": [{"key": "type", "value": {"stringValue": typ}}]}
    return json.dumps({"resourceMetrics": [{"scopeMetrics": [{"metrics": [
        {"name": "claude_code.token.usage",
         "sum": {"dataPoints": [dp(v, t) for v, t in token_points]}},
        {"name": "claude_code.cost.usage",
         "sum": {"dataPoints": [{"asDouble": cost, "attributes": []}]}},
        {"name": "claude_code.session.count",
         "sum": {"dataPoints": [{"asInt": "1", "attributes": []}]}},
    ]}]}]}).encode()


@pytest.fixture
def rx():
    r = OtelReceiver()
    r.start()
    yield r
    r.stop()


def _post(port, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/metrics", data=body,
                                 headers={"Content-Type": "application/json"})
    assert urllib.request.urlopen(req, timeout=5).status in (200, 202)


class TestOtelReceiver:
    def test_sums_token_types_and_cost(self, rx):
        _post(rx.port, _payload([(1000, "input"), (200, "output"), (5000, "cacheRead")]))
        _post(rx.port, _payload([(500, "input"), (30, "cacheCreation")], cost=0.02))
        snap = rx.snapshot()
        assert snap["tokens"] == {"input": 1500, "output": 200,
                                  "cacheRead": 5000, "cacheCreation": 30}
        assert abs(snap["cost_usd"] - 0.07) < 1e-9

    def test_reset_zeroes(self, rx):
        _post(rx.port, _payload([(10, "input")]))
        rx.reset()
        assert rx.snapshot()["tokens"]["input"] == 0

    def test_garbage_post_is_tolerated(self, rx):
        _post(rx.port, b"not json at all")
        assert rx.snapshot()["cost_usd"] == 0.0
