"""Source registry: raw Apple Health source names to canonical device keys.

Source names contain a curly apostrophe (U+2019); resolution is wildcard-based,
never straight-quote equality.
"""

from __future__ import annotations

import fnmatch
from functools import lru_cache

from heliosd.config import load_source_registry
from heliosd.trust.schema import validate_registry

EXCLUDED_KEY = "excluded"


class SourceRegistry:
    def __init__(self, cfg: dict | None = None):
        # Validated on construction (heliosd.trust.schema.REGISTRY_SCHEMA):
        # unknown keys, a bad ignored_mode or a duplicate device key raise
        # PolicyError (a ValueError) with every problem listed.
        cfg = validate_registry(cfg or load_source_registry())
        self.devices: list[dict] = cfg.get("devices", [])
        self.ignored: list[str] = cfg.get("ignored", [])
        self.fallback: str = cfg.get("fallback_key", "other")
        # ignored_mode (plan v2 4.2): drop = ignored sources never land (the
        # behaviour before Phase 1a); store = they land with device_key
        # 'excluded', outside every priority list, and the eligibility view
        # filters them. Nothing is dropped silently in store mode.
        self.ignored_mode: str = str(cfg.get("ignored_mode", "drop"))
        if self.ignored_mode not in ("drop", "store"):
            raise ValueError(f"source_registry ignored_mode must be drop or store, got {self.ignored_mode!r}")
        self.labels: dict[str, str] = {d["key"]: d.get("label", d["key"]) for d in self.devices}
        # Devices marked active: false are history-only; the watchdog never
        # expects fresh data from them.
        self.inactive: set[str] = {d["key"] for d in self.devices if d.get("active") is False}

    @lru_cache(maxsize=512)
    def resolve(self, source_name: str) -> str | None:
        """Device key for a raw source name; None if the source is ignored in
        drop mode, 'excluded' in store mode.

        Apple device names hide non-ASCII whitespace: a curly apostrophe
        (U+2019) and, between 'Apple' and 'Watch', a non-breaking space
        (U+00A0) or narrow no-break space (U+202F). Patterns use plain ASCII
        spaces, so normalize whitespace before matching; without this, years
        of Apple Watch data silently fell into the fallback bucket."""
        name = (source_name or "").replace("\u00a0", " ").replace("\u202f", " ").strip()
        for pat in self.ignored:
            if fnmatch.fnmatchcase(name, pat):
                return EXCLUDED_KEY if self.ignored_mode == "store" else None
        for d in self.devices:
            for pat in d.get("patterns", []):
                if fnmatch.fnmatchcase(name, pat):
                    return d["key"]
        return self.fallback

    def label(self, device_key: str) -> str:
        return self.labels.get(device_key, device_key)
