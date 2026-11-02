"""One version, one source (2026-11-02).

heliosd/__init__.py is the only place the daemon's version is written.
pyproject.toml reads it from there (setuptools dynamic attr) and FastAPI
imports it. The daemon runs from an editable install whose metadata keeps the
number it had when it was installed, so nothing reads importlib.metadata.

The web app keeps its own copy in web/package.json (and package-lock.json,
set together by `npm version X --no-git-tag-version`). The check below fails
when that copy, the Python version and the newest released version heading in
CHANGELOG.md disagree, so a release commit has to move all three together.
The Helios Bridge app keeps its own version (ios-bridge/project.yml)."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

import pytest

import heliosd

SERVER = Path(__file__).resolve().parents[1]
REPO = SERVER.parent

# "## v2.0.0" (optionally followed by a space and anything, such as a date) is
# a release; "## Unreleased" (or anything else) is not.
RELEASE_HEADING = re.compile(r"^## v(\d+\.\d+\.\d+)(?:[ \t].*)?$", re.M)
SEMVER = re.compile(r"\A\d+\.\d+\.\d+\Z")


def newest_released(changelog: str) -> str | None:
    """The version of the first release heading in the file (newest first)."""
    m = RELEASE_HEADING.search(changelog)
    return m.group(1) if m else None


def version_disagreements(python_version: str, package_json: str, package_lock: str,
                          changelog: str) -> list[str]:
    """Empty when every copy names the same version; otherwise every copy and
    its value, for the failure message."""
    pkg = json.loads(package_json)
    lock = json.loads(package_lock)
    found = {
        "heliosd.__version__": python_version,
        "web/package.json version": pkg.get("version"),
        "web/package-lock.json version": lock.get("version"),
        'web/package-lock.json packages[""].version': lock.get("packages", {}).get("", {}).get("version"),
        "CHANGELOG.md newest release heading": newest_released(changelog),
    }
    values = set(found.values())
    if len(values) == 1 and None not in values:
        return []
    return [f"{k} = {v!r}" for k, v in found.items()]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_heliosd_is_imported_from_this_checkout():
    # A worktree run must test the worktree's code, not the editable install's
    # target (the main checkout).
    assert Path(heliosd.__file__).resolve().parent == SERVER / "heliosd"


def test_the_version_is_a_plain_literal_in_init():
    # setuptools reads attr: versions statically when the value is a literal,
    # so an install never has to import heliosd.
    tree = ast.parse((SERVER / "heliosd" / "__init__.py").read_text(encoding="utf-8"))
    literals = [n.value.value for n in tree.body
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "__version__" for t in n.targets)
                and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)]
    assert len(literals) == 1 and SEMVER.match(literals[0]), literals
    assert heliosd.__version__ == literals[0]


def test_pyproject_reads_the_version_from_heliosd():
    data = tomllib.loads((SERVER / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert "version" not in project, "a static version in pyproject is a second source"
    assert "version" in project.get("dynamic", [])
    assert data["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "heliosd.__version__"}


def test_fastapi_reports_the_heliosd_version(tmp_path):
    from heliosd.config import Settings
    from heliosd.main import create_app
    app = create_app(Settings(raw={"server": {"ingest_token": "t" * 32},
                                   "storage": {"db_path": str(tmp_path / "h.duckdb")}}))
    assert app.version == heliosd.__version__


def test_python_web_and_changelog_name_the_same_version():
    problems = version_disagreements(heliosd.__version__, _read("web/package.json"),
                                     _read("web/package-lock.json"), _read("CHANGELOG.md"))
    assert problems == [], "versions disagree:\n" + "\n".join(problems)


# Planted controls: the check must fail on each single disagreement.

PKG = json.dumps({"name": "helios-web", "version": "2.0.0"})
LOCK = json.dumps({"name": "helios-web", "version": "2.0.0",
                   "packages": {"": {"name": "helios-web", "version": "2.0.0"}}})
LOG = "# Changelog\n\n## Unreleased\n\n- next\n\n## v2.0.0\n\n- shipped\n\n## v1.0.0\n"


def test_the_check_passes_when_all_agree_and_skips_unreleased():
    assert version_disagreements("2.0.0", PKG, LOCK, LOG) == []
    assert newest_released(LOG) == "2.0.0"


def test_a_release_heading_with_a_suffix_is_still_read():
    # Review round 1: a skipped "## v2.1.0 (date)" heading would compare an
    # older release, and a stale version everywhere else could then pass.
    log = "# Changelog\n\n## v2.1.0 (2026-11-10)\n\n- new\n\n## v2.0.0\n"
    assert newest_released(log) == "2.1.0"
    assert version_disagreements("2.0.0", PKG, LOCK, log) != []
    assert newest_released("## v2.0.0-rc1\n## v1.0.0\n") == "1.0.0"


@pytest.mark.parametrize("which", ["python", "package", "lock", "lock_packages", "changelog", "no_heading"])
def test_the_check_fails_on_each_single_disagreement(which):
    py, pkg, lock, log = "2.0.0", PKG, LOCK, LOG
    if which == "python":
        py = "2.0.1"
    elif which == "package":
        pkg = PKG.replace("2.0.0", "1.0.0")
    elif which == "lock":
        lock = json.dumps({"name": "helios-web", "version": "1.0.0",
                           "packages": {"": {"name": "helios-web", "version": "2.0.0"}}})
    elif which == "lock_packages":
        lock = json.dumps({"name": "helios-web", "version": "2.0.0",
                           "packages": {"": {"name": "helios-web", "version": "1.0.0"}}})
    elif which == "changelog":
        log = LOG.replace("## v2.0.0", "## v2.0.1")
    elif which == "no_heading":
        log = "# Changelog\n\n## Unreleased\n\n- next\n"
    assert version_disagreements(py, pkg, lock, log) != []
