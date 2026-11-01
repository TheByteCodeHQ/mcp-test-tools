"""Configuration. The agent can only run suites that a human defined here, never arbitrary commands."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

DEFAULT_FLAGS = ("-k", "-m", "-x", "--maxfail")
DEFAULT_ENV = ("PATH", "SYSTEMROOT", "SystemRoot", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR",
               "LANG", "LC_ALL", "PYTHONIOENCODING")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
CONFIG_ENV = "MCP_TEST_TOOLS_CONFIG"
DEFAULT_CONFIG_FILE = "mcp-test-tools.toml"


class ConfigError(ValueError):
    pass


@dataclass
class Suite:
    name: str
    command: list[str]
    cwd: Path
    description: str = ""
    allow_selector: bool = False
    allowed_flags: tuple[str, ...] = DEFAULT_FLAGS
    timeout_seconds: int = 300
    env_passthrough: tuple[str, ...] = ()


@dataclass
class BugsConfig:
    backend: str = "local"  # local | github | jira
    dir: Path = Path(".mcp-test-tools/bugs")
    max_per_run: int = 5
    github_repo: str = ""
    jira_project: str = ""
    jira_issue_type: str = "Bug"


@dataclass
class Config:
    root: Path
    runs_dir: Path
    suites: dict[str, Suite] = field(default_factory=dict)
    bugs: BugsConfig = field(default_factory=BugsConfig)
    max_concurrent_runs: int = 2


def _suite(name: str, raw: dict, base: Path) -> Suite:
    if not _NAME.match(name):
        raise ConfigError(f"Suite name '{name}' must be lowercase letters, digits, '-' or '_'")
    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
        raise ConfigError(f"Suite '{name}': 'command' must be a non-empty list of strings (no shell string)")
    if not any("{junit}" in token for token in command):
        raise ConfigError(f"Suite '{name}': the command must write JUnit XML to the '{{junit}}' placeholder")
    return Suite(
        name=name,
        command=command,
        cwd=(base / raw.get("cwd", ".")).resolve(),
        description=raw.get("description", ""),
        allow_selector=bool(raw.get("allow_selector", False)),
        allowed_flags=tuple(raw.get("allowed_flags", DEFAULT_FLAGS)),
        timeout_seconds=int(raw.get("timeout_seconds", 300)),
        env_passthrough=tuple(raw.get("env_passthrough", ())),
    )


def load_config(path: str | Path | None = None) -> Config:
    location = Path(path or os.getenv(CONFIG_ENV) or DEFAULT_CONFIG_FILE)
    if not location.is_file():
        raise ConfigError(
            f"Config file not found: {location}. Copy mcp-test-tools.example.toml to "
            f"{DEFAULT_CONFIG_FILE} or set {CONFIG_ENV}."
        )
    raw = tomllib.loads(location.read_text(encoding="utf-8"))
    base = location.resolve().parent
    suites_raw = raw.get("suites", {})
    if not suites_raw:
        raise ConfigError("Define at least one [suites.<name>] table")

    bugs_raw = raw.get("bugs", {})
    backend = bugs_raw.get("backend", "local")
    if backend not in ("local", "github", "jira"):
        raise ConfigError("bugs.backend must be local, github or jira")
    bugs = BugsConfig(
        backend=backend,
        dir=(base / bugs_raw.get("dir", ".mcp-test-tools/bugs")).resolve(),
        max_per_run=int(bugs_raw.get("max_per_run", 5)),
        github_repo=bugs_raw.get("github_repo", ""),
        jira_project=bugs_raw.get("jira_project", ""),
        jira_issue_type=bugs_raw.get("jira_issue_type", "Bug"),
    )
    if backend == "github" and "/" not in bugs.github_repo:
        raise ConfigError("bugs.github_repo must look like owner/name")
    if backend == "jira" and not bugs.jira_project:
        raise ConfigError("bugs.jira_project is required for the jira backend")

    return Config(
        root=base,
        runs_dir=(base / raw.get("runs_dir", ".mcp-test-tools/runs")).resolve(),
        suites={name: _suite(name, body, base) for name, body in suites_raw.items()},
        bugs=bugs,
        max_concurrent_runs=int(raw.get("max_concurrent_runs", 2)),
    )
