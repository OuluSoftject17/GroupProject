"""harness.baseline: report shape and statistics (short runs only)."""

import json
import platform
import re

import pytest

from harness import baseline
from harness.faults import FaultProxy, ServerError


@pytest.mark.parametrize("p, expected", [(50, 5), (95, 10), (99, 10), (1, 1), (100, 10)])
def test_nearest_rank_percentile(p, expected):
    assert baseline.percentile(list(range(1, 11)), p) == expected


def test_percentile_of_nothing_is_none():
    assert baseline.percentile([], 50) is None


def test_cli_writes_a_complete_report(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_TARGET_URL", raising=False)
    out = tmp_path / "baseline.json"

    assert baseline.main(["--duration", "1", "--warmup", "0.3", "--out", str(out)]) == 0

    report = json.loads(out.read_text(encoding="utf-8"))
    results, env = report["results"], report["environment"]
    assert results["requests"] > 0
    assert results["errors"]["total"] == 0
    latency = results["latency_ms"]
    assert 0 < latency["min"] <= latency["p50"] <= latency["p95"] <= latency["p99"] <= latency["max"]
    assert results["requests_per_second"] > 0
    assert 30 <= results["response_bytes"]["min"] <= results["response_bytes"]["max"] < 100
    assert report["config"]["warmup_s"] == 0.3
    assert env["python_version"] == platform.python_version()
    assert env["os"] == platform.system()
    assert env["git_commit"] is None or re.fullmatch(r"[0-9a-f]{40}", env["git_commit"])


def test_errors_are_counted_by_type(target_url):
    with FaultProxy(target_url, ServerError()) as proxy:
        results = baseline.measure(proxy.url, duration=0.3, warmup=0.0)

    assert results["successful"] == 0
    assert results["errors"]["total"] == results["requests"] > 0
    assert set(results["errors"]["by_type"]) == {"SensorHTTPError"}
    assert results["latency_ms"]["p50"] is None


@pytest.mark.slow
def test_full_length_baseline_run(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_TARGET_URL", raising=False)
    out = tmp_path / "baseline.json"
    baseline.main(["--duration", "30", "--out", str(out)])

    assert json.loads(out.read_text(encoding="utf-8"))["results"]["errors"]["total"] == 0
