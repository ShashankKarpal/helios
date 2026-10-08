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
import ssl

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
# [server] allow_clients (fix program A24, owner decision D3, 2026-10-08):
# "tailnet" serves loopback, this Mac's own interface addresses and the
# Tailscale ranges and refuses everything else with 403; "any" is the rollback.
ALLOW_CLIENTS_VALUES = ("tailnet", "any")


class ConfigError(RuntimeError):
    """A setting that cannot be used as written. run() prints it and exits 2
    instead of starting in a weaker mode than the file asks for."""


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
        """The one shared secret: sent by the Bridge to /ingest, by the PWA,
        Shortcut and MCP client to every /api route."""
        return str(self.raw.get("server", {}).get("ingest_token", "") or "")

    @property
    def api_auth_off(self) -> bool:
        """Rollback switch: [server] api_auth = "off" serves every /api route
        without a token and logs a warning at startup. Anything else (unset,
        "on") enforces the token. Exists so a mis-wired client can be repaired
        without stopping ingestion; never the steady state."""
        return str(self.raw.get("server", {}).get("api_auth", "on")).lower() == "off"

    @property
    def first_tick_seconds(self) -> float:
        """Delay before the first background tick (recompute, Whoop pull,
        feeds, watchdog). It was a fixed hour, so a restart hid the morning's
        Whoop night for an hour (fix program A3, K3); default 120 s."""
        return max(0.0, float(self.raw.get("server", {}).get("first_tick_seconds", 120)))

    @property
    def background_interval_seconds(self) -> float:
        """Cadence of the background tick after the first one (default an hour)."""
        return max(1.0, float(self.raw.get("server", {}).get("background_interval_seconds", 3600)))

    @property
    def allow_clients(self) -> str:
        """Who may connect (A24, decision D3). Unset means "tailnet"; an unknown
        value is a ConfigError, never a silent fallback to serving everyone."""
        v = str(self.raw.get("server", {}).get("allow_clients", "tailnet") or "tailnet").strip().lower()
        if v not in ALLOW_CLIENTS_VALUES:
            raise ConfigError(f"[server] allow_clients = {v!r}: expected one of {', '.join(ALLOW_CLIENTS_VALUES)}")
        return v

    @property
    def tls(self) -> tuple[str, str] | None:
        """(cert, key) when TLS is configured, None when neither is set. A
        configured pair that is missing, unreadable, not PEM or not a matching
        certificate and key is a ConfigError (fix program A24): the daemon used
        to fall back to plain HTTP on the same port without a word. The pair is
        loaded into a server SSL context here, so a bad file stops the start
        with its path instead of failing inside uvicorn."""
        s = self.raw.get("server", {})
        cert, key = _expand(str(s.get("tls_cert", "") or "")), _expand(str(s.get("tls_key", "") or ""))
        if not cert and not key:
            return None
        if not (cert and key):
            raise ConfigError("[server] tls_cert and tls_key must both be set for TLS (or both empty for plain "
                              "HTTP); only one is set")
        for label, path in (("tls_cert", cert), ("tls_key", key)):
            if not Path(path).is_file():
                raise ConfigError(f"[server] {label} = {path!r} is not a file; refusing to start without TLS")
        try:
            # A password callback that returns nothing: an encrypted key fails
            # here instead of prompting on a terminal launchd does not have.
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key, password=lambda: b"")
        except (ssl.SSLError, OSError) as e:
            why = getattr(e, "reason", None) or getattr(e, "strerror", None) or type(e).__name__
            raise ConfigError(f"[server] tls_cert {cert!r} and tls_key {key!r} do not load as a certificate and its "
                              f"private key ({why}); refusing to start without TLS")
        return cert, key

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
            "fallback_model": "qwen3.6-35b-a3b",
            "narrative_temperature": 0.2,
            "chat_temperature": 0.65,
            "timeout_seconds": 120,
        }
        d.update(self.raw.get("llm", {}))
        return d

    @property
    def whoop(self) -> dict[str, Any]:
        d = {"enabled": False, "client_id": "", "client_secret": "",
             "redirect_uri": "", "token_path": str(helios_home() / "data" / "whoop_tokens.json"),
             # A3: inside wake_window (reporting-zone hours) pull every
             # wake_poll_minutes until the night that ends today has landed;
             # 0 minutes turns the polling off (the hourly pull stays).
             "wake_window": "05:00-10:00", "wake_poll_minutes": 15}
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


def validate_overlay(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """The overlay is a PATCH (priority lists, snoozes, a sources list), so it
    is checked for allowed keys and value shapes with nothing required. The
    merged result is validated strictly by MetricPolicy and SourceRegistry.
    Raises heliosd.trust.schema.PolicyError naming every problem."""
    from heliosd.trust import schema
    if name == "metric_policy.yaml":
        return schema.validate_policy(data, strict=False, what=f"overlay {name}")
    if name == "source_registry.yaml":
        return schema.validate_registry(data, what=f"overlay {name}")
    return data


def load_yaml(name: str, overlay: bool = True) -> dict[str, Any]:
    """Repository default merged with the HELIOS_HOME overlay of the same name.
    The overlay is validated as a patch before the merge (dates normalised to
    ISO strings). `overlay=False` returns the tracked default alone (used by
    the test that proves the public copy is self-consistent)."""
    with open(CONFIG_DIR / name, "r", encoding="utf-8") as f:
        base = yaml.safe_load(f) or {}
    ov = overlay_path(name)
    if overlay and ov.is_file():
        with open(ov, "r", encoding="utf-8") as f:
            patch = validate_overlay(name, yaml.safe_load(f) or {})
        base = deep_merge(base, patch)
    return base


def load_metric_policy() -> dict[str, Any]:
    return load_yaml("metric_policy.yaml")


def load_source_registry() -> dict[str, Any]:
    return load_yaml("source_registry.yaml")
