import pytest

from mcp_test_tools.config import ConfigError, Suite, load_config
from mcp_test_tools.runner import SelectorError, parse_selector

SUITE = Suite(name="s", command=["x", "{junit}"], cwd=".", allow_selector=True)


@pytest.mark.parametrize("selector, expected", [
    (None, []),
    ("", []),
    ("-k login", ["-k", "login"]),
    ('-k "login and not slow"', ["-k", "login and not slow"]),
    ("test_cart.py::test_total[10-20]", ["test_cart.py::test_total[10-20]"]),
    ("--maxfail=2 tests/unit", ["--maxfail=2", "tests/unit"]),
])
def test_valid_selectors(selector, expected):
    assert parse_selector(selector, SUITE) == expected


@pytest.mark.parametrize("selector", [
    "--junitxml=/etc/passwd",          # not an allowed option (would redirect results)
    "-p evil_plugin",                  # plugin loading
    "--rootdir=/",                     # not allowed
    "../../secrets",                   # traversal
    "/etc/passwd",                     # absolute path
    "tests; rm -rf /",                 # shell metacharacters
    "tests && whoami",
    "$(whoami)",
    "`id`",
    "a|b",
    "C:\\Windows\\system32",           # backslashes
    "C:Windows/system32",              # drive-relative path
    "-k " + "x" * 500,                 # too long
    " ".join(["a"] * 25),              # too many parts
    '-k "unterminated',
])
def test_blocked_selectors(selector):
    with pytest.raises(SelectorError):
        parse_selector(selector, SUITE)


def test_suite_without_selector_support_refuses_any():
    closed = Suite(name="s", command=["x", "{junit}"], cwd=".", allow_selector=False)
    with pytest.raises(SelectorError):
        parse_selector("-k a", closed)


def write(tmp_path, text):
    path = tmp_path / "mcp-test-tools.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_valid_config(tmp_path):
    path = write(tmp_path, '[suites.unit]\ncommand = ["{python}", "-m", "pytest", "--junitxml={junit}"]\n'
                           'cwd = "."\nallow_selector = true\n')
    cfg = load_config(path)
    assert cfg.suites["unit"].allow_selector and cfg.suites["unit"].cwd == tmp_path.resolve()
    assert cfg.bugs.backend == "local"


@pytest.mark.parametrize("body, message", [
    ('[suites.a]\ncommand = "pytest --junitxml={junit}"\n', "list of strings"),
    ('[suites.a]\ncommand = ["pytest"]\n', "{junit}"),
    ('[suites."Bad Name"]\ncommand = ["x", "{junit}"]\n', "lowercase"),
    ('', "at least one"),
    ('[suites.a]\ncommand = ["x", "{junit}"]\n[bugs]\nbackend = "github"\n', "owner/name"),
    ('[suites.a]\ncommand = ["x", "{junit}"]\n[bugs]\nbackend = "other"\n', "local, github or jira"),
])
def test_invalid_configs(tmp_path, body, message):
    with pytest.raises(ConfigError, match=message.replace("{", r"\{").replace("}", r"\}")):
        load_config(write(tmp_path, body))


def test_missing_config_file_has_a_helpful_message(tmp_path):
    with pytest.raises(ConfigError, match="Config file not found"):
        load_config(tmp_path / "nope.toml")
