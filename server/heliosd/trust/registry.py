"""Source registry: raw Apple Health source names to canonical device keys.

Source names contain a curly apostrophe (U+2019); resolution is wildcard-based,
never straight-quote equality.
"""

from __future__ import annotations

import fnmatch
from functools import lru_cache

from heliosd.config import load_source_registry


class SourceRegistry:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or load_source_registry()
        self.devices: list[dict] = cfg.get("devices", [])
        self.ignored: list[str] = cfg.get("ignored", [])
        self.fallback: str = cfg.get("fallback_key", "other")
        self.labels: dict[str, str] = {d["key"]: d.get("label", d["key"]) for d in self.devices}
        # Devices marked active: false are history-only; the watchdog never
        # expects fresh data from them.
        self.inactive: set[str] = {d["key"] for d in self.devices if d.get("active") is False}

    @lru_cache(maxsize=512)
    def resolve(self, source_name: str) -> str | None:
        """Device key for a raw source name; None if the source is ignored.

        Apple device names hide non-ASCII whitespace: a curly apostrophe
        (U+2019) and, between 'Apple' and 'Watch', a non-breaking space
        (U+00A0) or narrow no-break space (U+202F). Patterns use plain ASCII
        spaces, so normalize whitespace before matching; without this, years
        of Apple Watch data silently fell into the fallback bucket."""
        name = (source_name or "").replace(" ", " ").replace(" ", " ").strip()
        for pat in self.ignored:
            if fnmatch.fnmatchcase(name, pat):
                return None
        for d in self.devices:
            for pat in d.get("patterns", []):
                if fnmatch.fnmatchcase(name, pat):
                    return d["key"]
        return self.fallback

    def label(self, device_key: str) -> str:
        return self.labels.get(device_key, device_key)
