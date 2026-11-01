import os

import pytest

from mcp_test_tools.config import Suite
from mcp_test_tools.junit import parse_junit, summarize
from mcp_test_tools.runner import RunError, RunManager

from .conftest import make_config

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="s" tests="4">
  <testcase classname="a.b" name="ok" time="0.5"/>
  <testcase classname="a.b" name="bad" time="1.0"><failure message="boom 42">Traceback...</failure>
    <system-out>out text</system-out></testcase>
  <testcase classname="a.b" name="err" time="0.1"><error message="kaput">trace</error></testcase>
  <testcase name="skipped" time="0"><skipped message="later"/></testcase>
</testsuite></testsuites>"""


def test_junit_parsing(tmp_path):
    path = tmp_path / "r.xml"
    path.write_text(JUNIT, encoding="utf-8")
    results = parse_junit(path)
    assert [r.outcome for r in results] == ["passed", "failed", "error", "skipped"]
    assert results[1].test_id == "a.b::bad" and results[1].message == "boom 42" and results[1].output == "out text"
    assert results[3].test_id == "skipped"
    assert summarize(results) == {"total": 4, "passed": 1, "failed": 1, "error": 1, "skipped": 1,
                                  "duration_seconds": 1.6}


def test_real_run_of_the_sample_project(config):
    runs = RunManager(config)
    status = runs.start("sample", wait_seconds=60)
    assert status["status"] == "completed" and status["ok"] is False
    summary = status["summary"]
    assert summary["failed"] == 2 and summary["skipped"] == 1 and summary["passed"] == 6
    # pytest prefixes the classname with the path from its rootdir, so compare on the ending
    failed = {r.test_id.rsplit(".", 1)[-1] for r in runs.failures(status["run_id"])}
    assert failed == {"test_cart::test_discount_above_100_percent_is_rejected",
                      "test_checkout::test_payment_gateway_call"}
    assert "passed" in runs.log_tail(status["run_id"]) or "failed" in runs.log_tail(status["run_id"])


def test_selector_narrows_the_run(config):
    runs = RunManager(config)
    status = runs.start("sample", "-k total", wait_seconds=60)
    assert status["summary"]["total"] == 3 and status["ok"] is True


def test_run_is_asynchronous_when_not_waiting(config):
    runs = RunManager(config)
    started = runs.start("sample", wait_seconds=0)
    assert started["status"] in ("running", "completed")
    runs._done[started["run_id"]].wait(60)
    assert runs.status(started["run_id"])["status"] == "completed"


def test_unknown_suite_and_bad_run_ids(config):
    runs = RunManager(config)
    with pytest.raises(RunError, match="Unknown suite"):
        runs.start("missing")
    for bad in ("../../etc", "nope", "20260101-000000-zzzzzz"):
        with pytest.raises(RunError):
            runs.status(bad)


def test_timeout_kills_the_run(tmp_path, python):
    config = make_config(tmp_path)
    config.suites["slow"] = Suite(name="slow", cwd=tmp_path, timeout_seconds=1,
                                  command=["{python}", "-c", "import time; time.sleep(60)", "{junit}"])
    runs = RunManager(config)
    status = runs.start("slow", wait_seconds=20)
    assert status["status"] == "timed_out" and status["ok"] is False


def test_missing_executable_is_reported_not_raised(tmp_path):
    config = make_config(tmp_path)
    config.suites["ghost"] = Suite(name="ghost", cwd=tmp_path, command=["definitely-not-a-real-binary", "{junit}"])
    status = RunManager(config).start("ghost")
    assert status["status"] == "crashed" and "Could not start" in status["error"]


def test_concurrency_limit(tmp_path):
    config = make_config(tmp_path)
    config.max_concurrent_runs = 1
    config.suites["slow"] = Suite(name="slow", cwd=tmp_path, timeout_seconds=30,
                                  command=["{python}", "-c", "import time; time.sleep(20)", "{junit}"])
    runs = RunManager(config)
    first = runs.start("slow")
    try:
        with pytest.raises(RunError, match="already in progress"):
            runs.start("slow")
    finally:
        runs._procs[first["run_id"]].kill()
        runs._done[first["run_id"]].wait(20)


def test_tests_do_not_see_tokens(config, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 36)
    monkeypatch.setenv("JIRA_API_TOKEN", "secret")
    monkeypatch.setenv("BASE_URL", "http://demo")
    runs = RunManager(config)
    suite = config.suites["sample"]
    env = runs._env(suite)
    assert "GITHUB_TOKEN" not in env and "JIRA_API_TOKEN" not in env and "BASE_URL" not in env
    assert "PATH" in env or os.name == "nt"
    suite.env_passthrough = ("BASE_URL",)
    assert runs._env(suite)["BASE_URL"] == "http://demo"
