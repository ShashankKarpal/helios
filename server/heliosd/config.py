"""Configuration loading: TOML settings plus YAML policy files."""

from __future__ import annotations

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


def _expand(p: str) -> str:
    return os.path.expanduser(p) if p else p


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
        return Path(_expand(self.raw.get("storage", {}).get("data_dir", "~/Helios/data")))

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
             "redirect_uri": "", "token_path": "~/Helios/data/whoop_tokens.json"}
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
        os.path.expanduser("~/Helios/helios.toml"),
        str(CONFIG_DIR / "helios.example.toml"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            with open(c, "rb") as f:
                return Settings(raw=tomllib.load(f))
    return Settings()


def load_yaml(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_metric_policy() -> dict[str, Any]:
    return load_yaml("metric_policy.yaml")


def load_source_registry() -> dict[str, Any]:
    return load_yaml("source_registry.yaml")
