"""Bug filing: draft -> duplicate check -> (confirmed) create.

Nothing is filed by default. ``file_bug`` returns a draft until the caller passes ``confirm=True``,
which lets a client put a human between the agent and the tracker. A per-run cap stops an agent
from flooding a tracker, and a fingerprint stops the same failure being filed twice.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .config import BugsConfig
from .junit import TestResult
from .redact import redact, truncate

SEVERITIES = ("low", "medium", "high", "critical")
_PRIORITY = {"low": "Low", "medium": "Medium", "high": "High", "critical": "Highest"}


class BugError(ValueError):
    pass


@dataclass
class BugDraft:
    title: str
    body: str
    severity: str
    labels: list[str]
    fingerprint: str
    test_id: str
    run_id: str

    def to_dict(self) -> dict:
        return asdict(self)


def fingerprint(test_id: str, message: str) -> str:
    """Stable id for 'this test failing for this reason', ignoring numbers and memory addresses."""
    first_line = (message.strip().splitlines() or [""])[0]
    normalised = re.sub(r"0x[0-9a-f]+|\d+", "N", first_line.lower())
    return hashlib.sha1(f"{test_id}|{normalised}".encode()).hexdigest()[:12]


def build_draft(run: dict, result: TestResult, *, severity: str = "medium", title: str | None = None,
                notes: str = "") -> BugDraft:
    if severity not in SEVERITIES:
        raise BugError(f"severity must be one of {', '.join(SEVERITIES)}")
    if result.outcome not in ("failed", "error"):
        raise BugError(f"'{result.test_id}' did not fail (outcome: {result.outcome}), so there is no bug to file")
    fp = fingerprint(result.test_id, result.message)
    reason = (result.message.strip().splitlines() or ["test failed"])[0]
    summary = title or f"Test failure: {result.name} - {reason}"
    body = "\n".join([
        f"**Test:** `{result.test_id}`",
        f"**Outcome:** {result.outcome}",
        f"**Suite:** {run['suite']}  |  **Run:** `{run['run_id']}`  |  **Selector:** `{run.get('selector') or '-'}`",
        "",
        "### Failure message",
        f"```\n{truncate(result.message, 800)}\n```",
        "### Details",
        f"```\n{truncate(result.details, 3000)}\n```",
        *(["### Captured output", f"```\n{truncate(result.output, 1500, keep='tail')}\n```"] if result.output else []),
        *(["### Notes", notes] if notes.strip() else []),
        "",
        f"fingerprint {fp}",
        "_Filed by mcp-test-tools. Please verify before acting._",
    ])
    return BugDraft(
        title=redact(summary)[:240],
        body=redact(body),
        severity=severity,
        labels=["bug", f"severity-{severity}", "auto-filed"],
        fingerprint=fp,
        test_id=result.test_id,
        run_id=run["run_id"],
    )


# -- backends ---------------------------------------------------------------------------------

class BugBackend(Protocol):
    name: str

    def find_duplicate(self, draft: BugDraft) -> dict | None: ...
    def create(self, draft: BugDraft) -> dict: ...


Opener = Callable[[urllib.request.Request], object]


def _http(request: urllib.request.Request, opener: Opener) -> dict:
    with opener(request) as response:  # type: ignore[attr-defined]
        return json.loads(response.read() or b"{}")


class LocalBackend:
    """Writes Markdown files. Needs no account, so it is the safe default and good for demos."""

    name = "local"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def find_duplicate(self, draft: BugDraft) -> dict | None:
        return None  # the shared index in BugService already covers local files

    def create(self, draft: BugDraft) -> dict:
        self.directory.mkdir(parents=True, exist_ok=True)
        number = len(list(self.directory.glob("BUG-*.md"))) + 1
        bug_id = f"BUG-{number:04d}"
        path = self.directory / f"{bug_id}.md"
        path.write_text(f"# {draft.title}\n\n*Severity:* {draft.severity}  \n*Labels:* {', '.join(draft.labels)}\n\n"
                        f"{draft.body}\n", encoding="utf-8")
        return {"id": bug_id, "url": str(path)}


class GitHubBackend:
    name = "github"

    def __init__(self, repo: str, token: str | None = None, opener: Opener = urllib.request.urlopen) -> None:
        self.repo = repo
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        self.opener = opener
        if not self.token:
            raise BugError("Set GITHUB_TOKEN to file bugs on GitHub")

    def _request(self, url: str, data: dict | None = None) -> urllib.request.Request:
        return urllib.request.Request(
            url, data=json.dumps(data).encode() if data else None, method="POST" if data else "GET",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"},
        )

    def find_duplicate(self, draft: BugDraft) -> dict | None:
        query = urllib.parse.quote(f'repo:{self.repo} is:issue is:open "{draft.fingerprint}" in:body')
        found = _http(self._request(f"https://api.github.com/search/issues?q={query}&per_page=1"), self.opener)
        items = found.get("items", [])
        return {"id": f"#{items[0]['number']}", "url": items[0]["html_url"]} if items else None

    def create(self, draft: BugDraft) -> dict:
        issue = _http(self._request(f"https://api.github.com/repos/{self.repo}/issues",
                                    {"title": draft.title, "body": draft.body, "labels": draft.labels}), self.opener)
        return {"id": f"#{issue['number']}", "url": issue["html_url"]}


class JiraBackend:
    """Creates issues through REST API v2. Duplicate detection relies on the local index only."""

    name = "jira"

    def __init__(self, project: str, issue_type: str = "Bug", opener: Opener = urllib.request.urlopen) -> None:
        names = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
        missing = [n for n in names if not os.getenv(n)]
        if missing:
            raise BugError("Missing environment variables for Jira: " + ", ".join(missing))
        self.base = os.environ["JIRA_BASE_URL"].rstrip("/")
        self.auth = base64.b64encode(f"{os.environ['JIRA_EMAIL']}:{os.environ['JIRA_API_TOKEN']}".encode()).decode()
        self.project, self.issue_type, self.opener = project, issue_type, opener

    def find_duplicate(self, draft: BugDraft) -> dict | None:
        return None

    def create(self, draft: BugDraft) -> dict:
        payload = {"fields": {
            "project": {"key": self.project}, "summary": draft.title, "description": draft.body,
            "issuetype": {"name": self.issue_type}, "priority": {"name": _PRIORITY[draft.severity]},
            "labels": [label.replace(" ", "-") for label in draft.labels],
        }}
        request = urllib.request.Request(
            f"{self.base}/rest/api/2/issue", data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Basic {self.auth}", "Content-Type": "application/json"})
        issue = _http(request, self.opener)
        return {"id": issue["key"], "url": f"{self.base}/browse/{issue['key']}"}


def make_backend(config: BugsConfig) -> BugBackend:
    if config.backend == "github":
        return GitHubBackend(config.github_repo)
    if config.backend == "jira":
        return JiraBackend(config.jira_project, config.jira_issue_type)
    return LocalBackend(config.dir)


# -- service ----------------------------------------------------------------------------------

@dataclass
class BugService:
    config: BugsConfig
    backend: BugBackend
    _index: Path = field(init=False)

    def __post_init__(self) -> None:
        self.config.dir.mkdir(parents=True, exist_ok=True)
        self._index = self.config.dir / "index.json"

    def _load(self) -> list[dict]:
        return json.loads(self._index.read_text(encoding="utf-8")) if self._index.is_file() else []

    def list_bugs(self, limit: int = 20) -> list[dict]:
        return list(reversed(self._load()))[:limit]

    def file_bug(self, run: dict, result: TestResult, *, severity: str = "medium", title: str | None = None,
                 notes: str = "", confirm: bool = False, allow_duplicate: bool = False) -> dict:
        draft = build_draft(run, result, severity=severity, title=title, notes=notes)
        index = self._load()
        existing = next((b for b in index if b["fingerprint"] == draft.fingerprint), None) \
            or self.backend.find_duplicate(draft)
        out: dict = {"backend": self.backend.name, "draft": draft.to_dict()}
        if existing and not allow_duplicate:
            return {**out, "status": "duplicate", "duplicate_of": {k: existing.get(k) for k in ("id", "url", "title")},
                    "message": "This failure was already filed. Pass allow_duplicate=true to file it again."}
        if not confirm:
            return {**out, "status": "draft",
                    "message": "Nothing was filed. Show this draft to a person, then call file_bug again "
                               "with confirm=true to create it."}
        filed_for_run = sum(1 for b in index if b["run_id"] == draft.run_id)
        if filed_for_run >= self.config.max_per_run:
            raise BugError(f"Limit reached: {filed_for_run} bugs already filed for run {draft.run_id} "
                           f"(max_per_run = {self.config.max_per_run})")
        created = self.backend.create(draft)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        index.append({"id": created["id"], "url": created["url"], "title": draft.title, "severity": severity,
                      "fingerprint": draft.fingerprint, "test_id": draft.test_id, "run_id": draft.run_id,
                      "backend": self.backend.name, "created_at": created_at})
        self._index.write_text(json.dumps(index, indent=2), encoding="utf-8")
        return {**out, "status": "filed", "bug": created}
