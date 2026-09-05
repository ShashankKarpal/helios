"""Configuration loading: TOML settings plus YAML policy files.

Two layers, so the repository never carries a person's device lineup or
thresholds:

1. `config/*.yaml` in the repository: generic defaults, safe to publish.
2. `$HELIOS_HOME/*.yaml` (default `~/Helios`): the owner's overlay, gitignored
   by location. Dicts merge recursively, key by key; lists and scalars in the
   overlay replace the default. So `~/Helios/metric_policy.yaml` can carry
   just `metrics: {heart_rate: {priority: [my_strap, my_watch]}}` and
   `~/Helios/source_registry.yaml` carries the whole `devices` list (order
   matters there, so it is replaced, not merged).

Tests point HELIOS_HOME at `server/tests/fixtures`, so a fresh clone and the
owner's Mac run the same suite against the same synthetic lineup.
"""

from __future__ import annotations

import copy
import os

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
OVERLAY_FILES = ("metric_policy.yaml", "source_registry.yaml")


def helios_home() -> Path:
    """Runtime home: config overlay, data, certs, logs. Never inside the repo."""
    return Path(os.path.expanduser(os.environ.get("HELIOS_HOME") or "~/Helios"))


def _expand(p: str) -> str:
    return os.path.expanduser(p) if p else p


def deep_merge(base: Any, overlay: Any) -> Any:
    """Recursive dict merge; lists and scalars from the overlay win outright.
    Neither input is mutated."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = {k: copy.deepcopy(v) for k, v in base.items()}
        for k, v in overlay.items():
            out[k] = deep_merge(out[k], v) if k in out else copy.deepcopy(v)
        return out
    return copy.deepcopy(overlay)


def overlay_path(name: str) -> Path:
    return helios_home() / name


def active_overlays() -> list[str]:
    """Names of overlay files present in HELIOS_HOME (for /api/health)."""
    return [n for n in OVERLAY_FILES if overlay_path(n).is_file()]


@dataclass
class Settings:
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return self.raw.get("server", {}).get("host", "127.0.0.1")

    @property
    def port(self) -> int:
        return int(self.raw.get("server", {}).get("port", 8420))

    @property
    def ingest_token(self) -> str:
        return self.raw.get("server", {}).get("ingest_token", "")

    @property
    def tls(self) -> tuple[str, str] | None:
        s = self.raw.get("server", {})
        cert, key = _expand(s.get("tls_cert", "")), _expand(s.get("tls_key", ""))
        if cert and key and Path(cert).exists() and Path(key).exists():
            return cert, key
        return None

    @property
    def data_dir(self) -> Path:
        p = self.raw.get("storage", {}).get("data_dir", "")
        return Path(_expand(p)) if p else helios_home() / "data"

    @property
    def db_path(self) -> Path:
        p = self.raw.get("storage", {}).get("db_path", "")
        return Path(_expand(p)) if p else self.data_dir / "helios.duckdb"

    @property
    def legacy_db_path(self) -> Path | None:
        p = self.raw.get("storage", {}).get("legacy_db_path", "")
        return Path(_expand(p)) if p else None

    @property
    def timezone(self) -> str:
        return self.raw.get("owner", {}).get("timezone", "UTC")

    @property
    def owner_name(self) -> str:
        return self.raw.get("owner", {}).get("name", "there")

    @property
    def heat_months(self) -> list[int]:
        return list(self.raw.get("owner", {}).get("heat_months", [5, 6, 7, 8, 9, 10]))

    @property
    def llm(self) -> dict[str, Any]:
        d = {
            "base_url": "http://localhost:1234/v1",
            "primary_model": "qwen3.6-35b-a3b",
            "fallback_model": "qwen3.5-9b",
            "narrative_temperature": 0.2,
            "chat_temperature": 0.65,
            "timeout_seconds": 120,
        }
        d.update(self.raw.get("llm", {}))
        return d

    @property
    def whoop(self) -> dict[str, Any]:
        d = {"enabled": False, "client_id": "", "client_secret": "",
             "redirect_uri": "", "token_path": str(helios_home() / "data" / "whoop_tokens.json")}
        d.update(self.raw.get("whoop", {}))
        d["token_path"] = _expand(d["token_path"])
        return d

    @property
    def macos_alerts(self) -> bool:
        return bool(self.raw.get("notifications", {}).get("macos_alerts", False))


def load_settings(path: str | None = None) -> Settings:
    candidates = [
        path,
        os.environ.get("HELIOS_CONFIG"),
        str(helios_home() / "helios.toml"),
        str(CONFIG_DIR / "helios.example.toml"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            with open(c, "rb") as f:
                return Settings(raw=tomllib.load(f))
    return Settings()


def load_yaml(name: str, overlay: bool = True) -> dict[str, Any]:
    """Repository default merged with the HELIOS_HOME overlay of the same name.
    `overlay=False` returns the tracked default alone (used by the test that
    proves the public copy is self-consistent)."""
    with open(CONFIG_DIR / name, "r", encoding="utf-8") as f:
        base = yaml.safe_load(f) or {}
    ov = overlay_path(name)
    if overlay and ov.is_file():
        with open(ov, "r", encoding="utf-8") as f:
            base = deep_merge(base, yaml.safe_load(f) or {})
    return base


def load_metric_policy() -> dict[str, Any]:
    return load_yaml("metric_policy.yaml")


def load_source_registry() -> dict[str, Any]:
    return load_yaml("source_registry.yaml")
