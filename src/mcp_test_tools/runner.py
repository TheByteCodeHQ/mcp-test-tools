"""Run configured suites as subprocesses and keep their results on disk.

Safety model: the agent picks a suite *name* and, if the suite allows it, a few vetted selector
arguments. It never supplies a command. Commands run without a shell, with a minimal environment
(so tokens for Jira or GitHub are not visible to the code under test), a timeout and a run cap.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import DEFAULT_ENV, Config, Suite
from .junit import FAILED_OUTCOMES, TestResult, parse_junit, summarize
from .redact import redact, truncate

_RUN_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
_SAFE_VALUE = re.compile(r"^[\w\-./:\[\]=*,@+ ()]*$")
_TRAVERSAL = re.compile(r"(^|/)\.\.(/|$)")
_DRIVE = re.compile(r"^[A-Za-z]:(?!:)")  # C:foo, but not the pytest node id separator in file.py::test
MAX_SELECTOR_TOKENS = 20
MAX_TOKEN_LENGTH = 200


class SelectorError(ValueError):
    pass


class RunError(ValueError):
    pass


def parse_selector(selector: str | None, suite: Suite) -> list[str]:
    """Turn a free-text selector into vetted extra arguments, or refuse it."""
    if not selector or not selector.strip():
        return []
    if not suite.allow_selector:
        raise SelectorError(f"Suite '{suite.name}' does not accept a selector")
    if "\\" in selector:  # shlex would silently drop backslashes and turn C:\x into the relative path C:x
        raise SelectorError("Backslashes are not allowed in a selector. Use forward slashes.")
    try:
        tokens = shlex.split(selector)
    except ValueError as exc:
        raise SelectorError(f"Could not parse selector: {exc}") from exc
    if len(tokens) > MAX_SELECTOR_TOKENS:
        raise SelectorError(f"Selector has more than {MAX_SELECTOR_TOKENS} parts")
    for token in tokens:
        if len(token) > MAX_TOKEN_LENGTH:
            raise SelectorError("A selector part is too long")
        if token.startswith("-"):
            flag = token.split("=", 1)[0]
            if flag not in suite.allowed_flags:
                raise SelectorError(
                    f"Option '{flag}' is not allowed. Allowed: {', '.join(suite.allowed_flags) or 'none'}"
                )
            token = token.split("=", 1)[1] if "=" in token else ""
        if not _SAFE_VALUE.match(token):
            raise SelectorError(f"Unsupported characters in selector part '{token[:40]}'")
        if token.startswith("/") or _DRIVE.match(token) or _TRAVERSAL.search(token):
            raise SelectorError("Absolute paths, drive letters and '..' are not allowed in a selector")
    return tokens


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


class RunManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.config.runs_dir.mkdir(parents=True, exist_ok=True)
        self._procs: dict[str, subprocess.Popen] = {}
        self._done: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # -- paths and metadata -----------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        if not _RUN_ID.match(run_id):
            raise RunError(f"Invalid run id '{run_id[:40]}'")
        return self.config.runs_dir / run_id

    def _write_meta(self, run_id: str, meta: dict) -> None:
        path = self.run_dir(run_id) / "meta.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _read_meta(self, run_id: str) -> dict:
        path = self.run_dir(run_id) / "meta.json"
        if not path.is_file():
            raise RunError(f"Unknown run id '{run_id}'")
        return json.loads(path.read_text(encoding="utf-8"))

    # -- starting a run ---------------------------------------------------------------------

    def _env(self, suite: Suite) -> dict[str, str]:
        wanted = set(DEFAULT_ENV) | set(suite.env_passthrough)
        return {k: v for k, v in os.environ.items() if k in wanted}

    def start(self, suite_name: str, selector: str | None = None, wait_seconds: float = 0) -> dict:
        suite = self.config.suites.get(suite_name)
        if suite is None:
            raise RunError(f"Unknown suite '{suite_name}'. Available: {', '.join(self.config.suites)}")
        extra = parse_selector(selector, suite)
        with self._lock:
            active = sum(1 for p in self._procs.values() if p.poll() is None)
            if active >= self.config.max_concurrent_runs:
                raise RunError(f"{active} runs are already in progress (limit {self.config.max_concurrent_runs})")

        run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True)
        junit_path = run_dir / "junit.xml"
        argv = [t.replace("{python}", sys.executable).replace("{junit}", str(junit_path)) for t in suite.command]
        argv += extra

        meta = {"run_id": run_id, "suite": suite.name, "selector": selector or "", "status": "running",
                "started_at": _now(), "finished_at": None, "exit_code": None, "ok": None, "summary": None}
        self._write_meta(run_id, meta)

        try:
            log = (run_dir / "output.log").open("wb")
            kwargs: dict = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            proc = subprocess.Popen(argv, cwd=suite.cwd, env=self._env(suite), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, **kwargs)
        except OSError as exc:
            meta.update(status="crashed", finished_at=_now(), ok=False, error=f"Could not start: {exc}")
            self._write_meta(run_id, meta)
            return self.status(run_id)

        done = threading.Event()
        with self._lock:
            self._procs[run_id] = proc
            self._done[run_id] = done
        threading.Thread(target=self._watch, args=(run_id, proc, log, suite, junit_path, done),
                         daemon=True).start()
        if wait_seconds > 0:
            done.wait(wait_seconds)
        return self.status(run_id)

    def _watch(self, run_id: str, proc: subprocess.Popen, log, suite: Suite, junit_path: Path,
               done: threading.Event) -> None:
        status = "completed"
        try:
            proc.wait(timeout=suite.timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait()
            status = "timed_out"
        finally:
            log.close()
        meta = self._read_meta(run_id)
        meta.update(finished_at=_now(), exit_code=proc.returncode)
        summary = None
        if junit_path.is_file():
            try:
                summary = summarize(parse_junit(junit_path))
            except Exception as exc:  # malformed XML from a crashed runner
                meta["error"] = f"Could not read results: {exc}"
        if status == "completed" and summary is None:
            status = "crashed"
        meta.update(status=status, summary=summary)
        meta["ok"] = bool(status == "completed" and proc.returncode == 0
                          and summary and not summary["failed"] and not summary["error"])
        self._write_meta(run_id, meta)
        done.set()

    # -- reading ----------------------------------------------------------------------------

    def status(self, run_id: str) -> dict:
        meta = self._read_meta(run_id)
        if meta["status"] == "running" and run_id not in self._procs:
            meta.update(status="lost", error="The server restarted while this run was in progress")
        return meta

    def list_runs(self, limit: int = 10) -> list[dict]:
        ids = sorted((p.name for p in self.config.runs_dir.iterdir() if p.is_dir() and _RUN_ID.match(p.name)),
                     reverse=True)
        return [self.status(i) for i in ids[:limit]]

    def results(self, run_id: str) -> list[TestResult]:
        self.run_dir(run_id)
        junit_path = self.run_dir(run_id) / "junit.xml"
        if not junit_path.is_file():
            if self.status(run_id)["status"] == "running":
                raise RunError("The run is still in progress. Check get_run_status and try again.")
            raise RunError("This run produced no results file. See get_run_log for the output.")
        return parse_junit(junit_path)

    def log_tail(self, run_id: str, chars: int = 3000) -> str:
        path = self.run_dir(run_id) / "output.log"
        if not path.is_file():
            raise RunError(f"Unknown run id '{run_id}'")
        text = path.read_text(encoding="utf-8", errors="replace")
        return truncate(redact(text), chars, keep="tail")

    def failures(self, run_id: str) -> list[TestResult]:
        return [r for r in self.results(run_id) if r.outcome in FAILED_OUTCOMES]
