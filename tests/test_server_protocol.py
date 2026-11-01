"""End-to-end over the real MCP protocol: in-process, and through a stdio subprocess."""

import asyncio
import json
import sys

from mcp import Client, StdioServerParameters

from mcp_test_tools.server import create_server

from .conftest import ROOT, SAMPLE


def data(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def run(coro):
    return asyncio.run(coro)


def test_tools_and_prompt_are_listed(config):
    async def go():
        async with Client(create_server(config)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            prompts = [p.name for p in (await client.list_prompts()).prompts]
            return tools, prompts

    tools, prompts = run(go())
    assert set(tools) == {"list_suites", "run_tests", "get_run_status", "list_runs", "get_results",
                          "get_test_details", "get_run_log", "compare_runs", "file_bug", "list_bugs"}
    assert prompts == ["triage_failures"]
    assert tools["list_suites"].annotations.read_only_hint is True
    assert tools["file_bug"].annotations.read_only_hint is False
    assert tools["file_bug"].annotations.open_world_hint is True


def test_run_read_and_file_a_bug_with_a_confirmation_gate(config):
    async def go():
        async with Client(create_server(config)) as client:
            started = data(await client.call_tool("run_tests", {"suite": "sample", "wait_seconds": 60}))
            run_id = started["run_id"]
            failures = data(await client.call_tool("get_results", {"run_id": run_id}))
            test_id = next(r["test_id"] for r in failures["results"] if "payment" in r["test_id"])
            details = data(await client.call_tool("get_test_details", {"run_id": run_id, "test_id": test_id}))
            draft = data(await client.call_tool("file_bug", {"run_id": run_id, "test_id": test_id}))
            filed = data(await client.call_tool("file_bug", {"run_id": run_id, "test_id": test_id, "confirm": True}))
            dup = data(await client.call_tool("file_bug", {"run_id": run_id, "test_id": test_id, "confirm": True}))
            listed = data(await client.call_tool("list_bugs", {}))
            rerun = data(await client.call_tool("run_tests", {"suite": "sample", "selector": "-k shipping",
                                                              "wait_seconds": 60}))
            diff = data(await client.call_tool("compare_runs", {"base_run_id": run_id, "run_id": rerun["run_id"]}))
            return started, failures, details, draft, filed, dup, listed, diff

    started, failures, details, draft, filed, dup, listed, diff = run(go())
    assert started["summary"]["failed"] == 2 and failures["matched"] == 2
    assert "UNTRUSTED" in failures["note"].upper()
    assert "sk-demo-0123456789abcdefghij" not in json.dumps(details)           # redacted in tool output
    assert "buyer@shop-demo.io" not in json.dumps(details)
    assert draft["status"] == "draft"                                           # nothing filed yet
    assert filed["status"] == "filed" and dup["status"] == "duplicate"
    assert [b["id"] for b in listed["bugs"]] == [filed["bug"]["id"]]
    assert len(diff["missing_tests"]) > 0 and diff["new_failures"] == []


def test_expected_errors_reach_the_caller_with_their_reason(config):
    async def go():
        async with Client(create_server(config)) as client:
            return [
                await client.call_tool("run_tests", {"suite": "nope"}),
                await client.call_tool("run_tests", {"suite": "sample", "selector": "--junitxml=/tmp/x"}),
                await client.call_tool("get_results", {"run_id": "../../etc"}),
            ]

    unknown, blocked, bad_id = run(go())
    assert unknown.is_error and "Unknown suite" in unknown.content[0].text
    assert blocked.is_error and "not allowed" in blocked.content[0].text
    assert bad_id.is_error and "Invalid run id" in bad_id.content[0].text


def test_real_stdio_server_started_from_a_config_file(tmp_path):
    cfg = tmp_path / "mcp-test-tools.toml"
    cfg.write_text(
        f'runs_dir = "runs"\n[suites.sample]\ncwd = {json.dumps(str(SAMPLE))}\nallow_selector = true\n'
        'command = ["{python}", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml={junit}"]\n'
        '[bugs]\ndir = "bugs"\n', encoding="utf-8")

    async def go():
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "mcp_test_tools", "--config", str(cfg)], cwd=str(ROOT))
        async with Client(params) as client:
            suites = data(await client.call_tool("list_suites", {}))
            started = data(await client.call_tool("run_tests", {"suite": "sample", "wait_seconds": 60}))
            return suites, started

    suites, started = run(go())
    assert suites["suites"][0]["name"] == "sample"
    assert started["status"] == "completed" and started["summary"]["total"] == 9


def test_server_exits_cleanly_with_a_helpful_message_on_bad_config(tmp_path, capsys):
    from mcp_test_tools.server import main
    assert main(["--config", str(tmp_path / "missing.toml")]) == 1
    assert "Config file not found" in capsys.readouterr().err
