import json

import pytest

from mcp_test_tools.bugs import (
    BugError,
    BugService,
    GitHubBackend,
    JiraBackend,
    LocalBackend,
    build_draft,
    fingerprint,
)
from mcp_test_tools.config import BugsConfig
from mcp_test_tools.junit import TestResult

RUN = {"run_id": "20260101-000000-abcdef", "suite": "sample", "selector": ""}


def failing(test_id="mod::test_pay", message="gateway returned 502", output="", details="Traceback..."):
    cls, _, name = test_id.rpartition("::")
    return TestResult(test_id=test_id, classname=cls, name=name, outcome="failed", duration=0.1,
                      message=message, details=details, output=output)


def service(tmp_path, **kw):
    cfg = BugsConfig(dir=tmp_path / "bugs", **kw)
    return BugService(cfg, LocalBackend(cfg.dir))


def test_fingerprint_ignores_numbers_but_not_the_reason():
    assert fingerprint("t", "gateway returned 502") == fingerprint("t", "gateway returned 503")
    assert fingerprint("t", "gateway returned 502") != fingerprint("t", "timeout after 30s")
    assert fingerprint("t1", "x") != fingerprint("t2", "x")


def test_draft_redacts_secrets_and_personal_data():
    result = failing(output="api_key=sk-demo-0123456789abcdefghij mail buyer@shop-demo.io",
                     details="Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456")
    draft = build_draft(RUN, result, notes="password: hunter2")
    for leaked in ("sk-demo-0123456789abcdefghij", "buyer@shop-demo.io", "hunter2", "abcdefghijklmnopqrstuvwxyz123456"):
        assert leaked not in draft.body
    assert draft.fingerprint in draft.body and draft.labels == ["bug", "severity-medium", "auto-filed"]


def test_cannot_file_a_bug_for_a_passing_test():
    ok = TestResult(test_id="m::t", classname="m", name="t", outcome="passed", duration=0)
    with pytest.raises(BugError, match="no bug to file"):
        build_draft(RUN, ok)
    with pytest.raises(BugError, match="severity"):
        build_draft(RUN, failing(), severity="urgent")


def test_nothing_is_filed_without_confirmation(tmp_path):
    svc = service(tmp_path)
    out = svc.file_bug(RUN, failing())
    assert out["status"] == "draft" and not list((tmp_path / "bugs").glob("BUG-*.md"))
    assert svc.list_bugs() == []


def test_confirmed_bug_is_written_and_then_deduplicated(tmp_path):
    svc = service(tmp_path)
    filed = svc.file_bug(RUN, failing(), confirm=True)
    assert filed["status"] == "filed" and filed["bug"]["id"] == "BUG-0001"
    assert (tmp_path / "bugs" / "BUG-0001.md").read_text(encoding="utf-8").startswith("# Test failure")
    again = svc.file_bug(RUN, failing(message="gateway returned 504"), confirm=True)
    assert again["status"] == "duplicate" and again["duplicate_of"]["id"] == "BUG-0001"
    forced = svc.file_bug(RUN, failing(), confirm=True, allow_duplicate=True)
    assert forced["status"] == "filed" and forced["bug"]["id"] == "BUG-0002"


def test_per_run_cap(tmp_path):
    svc = service(tmp_path, max_per_run=2)
    for i in range(2):
        assert svc.file_bug(RUN, failing(f"m::t{i}", f"reason {'x' * i}"), confirm=True)["status"] == "filed"
    with pytest.raises(BugError, match="Limit reached"):
        svc.file_bug(RUN, failing("m::t9", "another reason"), confirm=True)


class Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_github_backend_searches_then_creates():
    calls = []

    def opener(request):
        calls.append(request)
        if "search/issues" in request.full_url:
            return Resp({"items": []})
        return Resp({"number": 12, "html_url": "https://github.com/o/r/issues/12"})

    backend = GitHubBackend("o/r", token="t", opener=opener)
    draft = build_draft(RUN, failing())
    assert backend.find_duplicate(draft) is None
    assert backend.create(draft) == {"id": "#12", "url": "https://github.com/o/r/issues/12"}
    assert calls[1].headers["Authorization"] == "Bearer t"
    assert json.loads(calls[1].data)["labels"] == draft.labels


def test_github_backend_reports_existing_duplicate():
    backend = GitHubBackend("o/r", token="t", opener=lambda r: Resp(
        {"items": [{"number": 3, "html_url": "https://github.com/o/r/issues/3"}]}))
    assert backend.find_duplicate(build_draft(RUN, failing()))["id"] == "#3"


def test_github_needs_a_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(BugError, match="GITHUB_TOKEN"):
        GitHubBackend("o/r")


def test_jira_backend_creates_issue(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://x.atlassian.net/")
    monkeypatch.setenv("JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    sent = []
    backend = JiraBackend("QA", opener=lambda r: (sent.append(r), Resp({"key": "QA-9"}))[1])
    created = backend.create(build_draft(RUN, failing(), severity="critical"))
    assert created == {"id": "QA-9", "url": "https://x.atlassian.net/browse/QA-9"}
    body = json.loads(sent[0].data)["fields"]
    assert body["priority"] == {"name": "Highest"} and body["project"] == {"key": "QA"}
    assert sent[0].full_url == "https://x.atlassian.net/rest/api/2/issue"


def test_jira_needs_credentials(monkeypatch):
    for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(BugError, match="JIRA_BASE_URL"):
        JiraBackend("QA")
