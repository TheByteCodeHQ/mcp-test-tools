import sys
from pathlib import Path

import pytest

from mcp_test_tools.config import BugsConfig, Config, Suite

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "examples" / "sample-project"


def make_config(tmp_path: Path, **suite_overrides) -> Config:
    suite = Suite(
        name="sample",
        command=["{python}", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml={junit}"],
        cwd=SAMPLE,
        description="sample",
        allow_selector=True,
        timeout_seconds=120,
        **suite_overrides,
    )
    return Config(
        root=tmp_path,
        runs_dir=tmp_path / "runs",
        suites={"sample": suite},
        bugs=BugsConfig(dir=tmp_path / "bugs", max_per_run=2),
    )


@pytest.fixture
def config(tmp_path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def python() -> str:
    return sys.executable
