"""Config layering: the tracked defaults are generic and self-consistent, the
HELIOS_HOME overlay is what carries a real lineup, and the merge is exact."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from heliosd import config
from heliosd.config import deep_merge, load_yaml
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

REPO_CONFIG = Path(config.CONFIG_DIR)


def test_suite_runs_on_the_fixture_overlay_not_the_owner_home():
    home = Path(os.environ["HELIOS_HOME"])
    assert home.name == "fixtures" and home.is_dir()
    assert config.helios_home() == home
    assert sorted(config.active_overlays()) == ["metric_policy.yaml", "source_registry.yaml"]


def test_deep_merge_dicts_recursively_lists_replace():
    base = {"metrics": {"hr": {"priority": ["a", "b"], "cadence_hours": 12},
                        "spo2": {"priority": ["a"]}},
            "baseline": {"windows_days": [30, 60, 90]}}
    overlay = {"metrics": {"hr": {"priority": ["c"]}, "new": {"priority": ["z"]}}}
    out = deep_merge(base, overlay)
    assert out["metrics"]["hr"] == {"priority": ["c"], "cadence_hours": 12}
    assert out["metrics"]["spo2"] == {"priority": ["a"]}
    assert out["metrics"]["new"] == {"priority": ["z"]}
    assert out["baseline"] == base["baseline"]
    # inputs untouched
    assert base["metrics"]["hr"]["priority"] == ["a", "b"]


def test_overlay_is_applied_to_policy_and_registry():
    assert MetricPolicy().priority("heart_rate")[0] == "zepp_helio"
    assert SourceRegistry().resolve("Owner’s Ultra 1") == "apple_watch_ultra"
    # Untouched keys still come from the tracked default.
    assert MetricPolicy().cadence_hours("heart_rate") == 12


def test_tracked_defaults_are_generic_and_self_consistent():
    """A fresh clone with no overlay must load a policy whose device keys all
    exist in the tracked registry, and must carry no personal state."""
    policy = load_yaml("metric_policy.yaml", overlay=False)
    registry = load_yaml("source_registry.yaml", overlay=False)
    keys = {d["key"] for d in registry["devices"]}
    missing = {(m, k) for m, spec in policy["metrics"].items()
               for k in spec.get("priority", []) if k not in keys}
    assert not missing, f"policy names device keys the registry lacks: {sorted(missing)}"
    for name, spec in policy["metrics"].items():
        assert "snooze_until" not in spec, f"{name}: snoozes belong in the HELIOS_HOME overlay"
    text = (REPO_CONFIG / "metric_policy.yaml").read_text(encoding="utf-8")
    assert "redacted" not in text.lower()


def test_tracked_defaults_alone_still_build_objects(tmp_path, monkeypatch):
    """Point HELIOS_HOME at an empty directory: the tracked files are enough."""
    monkeypatch.setenv("HELIOS_HOME", str(tmp_path))
    assert config.active_overlays() == []
    p, r = MetricPolicy(), SourceRegistry()
    assert p.priority("heart_rate")[0] == "zepp_strap"
    assert r.resolve("WHOOP") == "whoop"
    assert r.resolve("Owner’s Apple Watch") == "apple_watch"
    assert r.resolve("Unknown Gadget") == "other"


def test_overlay_files_parse_as_mappings():
    for name in config.OVERLAY_FILES:
        data = yaml.safe_load((Path(os.environ["HELIOS_HOME"]) / name).read_text(encoding="utf-8"))
        assert isinstance(data, dict)
