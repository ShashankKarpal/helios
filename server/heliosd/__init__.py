"""Helios daemon. Designed and built by Claude (Claude Fable 5)."""

# The daemon's version, written here only: pyproject.toml reads it (setuptools
# dynamic attr) and FastAPI imports it. Keep it a plain literal. A release moves
# it together with web/package.json (npm version X --no-git-tag-version) and the
# CHANGELOG heading; tests/test_version_single_source.py fails otherwise.
__version__ = "2.0.1"
