"""Schema validation for the policy files (single-source plan v2, Phase 1a item 8).

Two passes, because the owner's overlay is a PATCH, not a policy (checkpoint A,
point 14): the overlay may carry only priority lists and snoozes, so it is
validated for allowed keys and value shapes with nothing required; the MERGED
policy (repository default plus overlay) is validated strictly: every metric
has a unit, no key outside the allowlist, no two metrics mapped to one
HealthKit identifier, full HealthKit identifiers only, enumerations closed.
YAML hands dates to Python as date objects; they are normalised to ISO strings
first so one shape reaches every consumer.

Metric keys. Existing (plan v2 4.1, every one kept): hk, unit, priority,
direction, trust, flag_rule, cadence_hours, optional, snooze_until, zones,
live_overlay, never_blend, note, label (display name). Extensions (4.2): corroboration,
exercise_priority, sample_context, episode_group, day_basis, derive, daily,
agg, baseline_scope, discrepancy, coverage. Wave 2 (fix program design 1.0
point 2): sync_paths, merge, the day basis interval_midpoint, and qualified
device keys (`<device>:healthkit`) in priority and corroboration. Validated
here, applied by the Wave 2 groups; until then they have no runtime effect.
Top-level blocks kept as data: reporting_timezone, unknown_types, workouts,
activity_rings, ecg, labs.

Errors are collected, not raised one at a time: a startup failure names every
problem with its path, for example `metrics.heart_rate: Additional properties
are not allowed ('priorty' was unexpected)`.

The policy and the registry are then checked against each other
(registry_problems, validate_policy_against_registry; design B15): every
device a policy list names must exist in the registry. The daemon refuses to
start on any problem, the rebuild tool stops, and server/tools/check_policy.py
prints them for HELIOS_HOME's real files.
"""

from __future__ import annotations

import copy
import difflib
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator

HK_PATTERN = r"^HK[A-Za-z]+TypeIdentifier[A-Za-z0-9]+$"
_NUM = r"[0-9]+(\.[0-9]+)?"
FLAG_RULE_PATTERN = rf"^(none|abs_above_30d_avg >= {_NUM}|below_30d_baseline_pct >= {_NUM}|below_hours {_NUM})$"
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
DEFAULT_WINDOW = 30  # the effective default when baseline.default_window is absent (policy.py)
DIRECTIONS = ("lower", "higher", "band", "none", "contextual")  # contextual reads as none (4.1)
TRUSTS = ("absolute", "trend_only", "screening", "directional")
AGGS = ("sum", "avg", "last", "min", "max")
# calendar: the start wall date; sleep_end: the night's wake date; whoop_cycle:
# the day of the cycle's recovery; interval_midpoint (Wave 2, B3): the day that
# holds the interval's midpoint, so most of an interval up to two days long.
DAY_BASES = ("calendar", "sleep_end", "whoop_cycle", "interval_midpoint")
SAMPLE_CONTEXTS = ("all_day", "sleep_only", "non_exercise")
TOP_BLOCKS = ("reporting_timezone", "unknown_types", "workouts", "activity_rings", "ecg", "labs")
# merge: interval (Wave 2, B11): devices in priority order each add only the
# part of their intervals no higher device covered (steps, owner decision D4).
MERGES = ("interval",)
# The samples.sync_path values the store documents (store/schema.sql).
SYNC_PATHS = ("bridge", "whoop_live", "backfill", "legacy_import", "manual", "health_export")
# A qualified device key `<device>:healthkit` names the HealthKit copy of a
# device whose value normally comes from its own API (Whoop). With
# `sync_paths: {<device>: [...]}` the device's own key counts only rows from
# those paths; its rows from other paths count only under the qualified key,
# which arbitrates as a key of its own (fallback label, own baseline). Allowed
# in priority and corroboration only; every other place takes plain keys.
HEALTHKIT_QUALIFIER = "healthkit"
DEVICE_QUALIFIERS = (HEALTHKIT_QUALIFIER,)
QUALIFIED_KEY_LISTS = ("priority", "corroboration")

_STR_LIST = {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True}


def split_device_key(key: str) -> tuple[str, str | None]:
    """An arbitration key as (registry device, qualifier): 'whoop:healthkit'
    gives ('whoop', 'healthkit'), 'whoop' gives ('whoop', None). Shape only;
    validate_policy refuses any qualifier other than healthkit."""
    base, sep, qualifier = key.partition(":")
    return base, (qualifier if sep else None)


def base_device(key: str) -> str:
    """The registry device an arbitration key belongs to: 'whoop:healthkit'
    and 'whoop' both give 'whoop' (the corroboration rule drops every other
    key of the owner's own device, design 1.0 point 4)."""
    return split_device_key(key)[0]


METRIC_PROPERTIES: dict[str, Any] = {
    # existing keys
    "hk": {"type": ["string", "null"], "pattern": HK_PATTERN},
    "unit": {"type": "string", "minLength": 1},
    "priority": _STR_LIST,
    "direction": {"enum": list(DIRECTIONS)},
    "trust": {"enum": list(TRUSTS)},
    "flag_rule": {"type": "string", "pattern": FLAG_RULE_PATTERN},
    "cadence_hours": {"type": "number", "exclusiveMinimum": 0},
    "optional": {"type": "boolean"},
    "snooze_until": {"type": "string", "pattern": DATE_PATTERN},
    "zones": {"type": "object", "required": ["green", "yellow", "red"], "additionalProperties": {
        "type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}},
    "live_overlay": {"type": "string"},
    "never_blend": {"type": "boolean"},
    "note": {"type": "string"},
    # display label (fix program A20 / audit M17): what every surface calls the
    # metric; the key stays stable, the label says what the sensor measures
    "label": {"type": "string", "minLength": 1},
    # plan v2 4.2 extensions
    "corroboration": _STR_LIST,
    "exercise_priority": _STR_LIST,
    "sample_context": {"enum": list(SAMPLE_CONTEXTS)},
    "episode_group": {"type": "string", "minLength": 1},
    "day_basis": {"enum": list(DAY_BASES)},
    # a derived metric needs a parent and at least one device to take from it
    "derive": {"type": "object", "required": ["from", "devices"], "additionalProperties": False,
               "properties": {"from": {"type": "string", "minLength": 1}, "devices": {**_STR_LIST, "minItems": 1}}},
    "daily": {"type": "boolean"},
    "agg": {"enum": list(AGGS)},
    "baseline_scope": {"enum": ["source"]},
    "discrepancy": {"oneOf": [
        {"const": "not_comparable"},
        {"type": "object", "additionalProperties": False, "minProperties": 1,
         "properties": {"abs": {"type": "number", "minimum": 0}, "pct": {"type": "number", "minimum": 0}}}]},
    "coverage": {"type": "object", "required": ["slot_min", "min_fraction"], "additionalProperties": False,
                 "properties": {"slot_min": {"type": "number", "exclusiveMinimum": 0},
                                "min_fraction": {"type": "number", "minimum": 0, "maximum": 1}}},
    # Wave 2 (design 1.0 point 2)
    # {device: [sync_path, ...]}: that device's rows count for this metric only
    # from those paths (for example {whoop: [whoop_live]}).
    "sync_paths": {"type": "object", "propertyNames": {"minLength": 1},
                   "additionalProperties": {"type": "array", "minItems": 1, "uniqueItems": True,
                                            "items": {"enum": list(SYNC_PATHS)}}},
    "merge": {"enum": list(MERGES)},
}
METRIC_KEYS = frozenset(METRIC_PROPERTIES)

_SOURCE = {"type": "object", "required": ["key", "path"], "additionalProperties": False,
           "properties": {"key": {"type": "string", "minLength": 1}, "label": {"type": "string"},
                          "path": {"type": "string", "minLength": 1},
                          "cadence_hours": {"type": "number", "exclusiveMinimum": 0},
                          "ts_field": {"type": "string"}, "event_field": {"type": "string"},
                          "ingest": {"enum": ["events"]}, "notify": {"type": "boolean"},
                          "fix": {"type": "string"}}}


def _metric_schema(strict: bool) -> dict:
    s: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": METRIC_PROPERTIES}
    if strict:
        s["required"] = ["unit"]
    return s


def policy_schema(strict: bool) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "metrics": {"type": "object", "propertyNames": {"pattern": r"^[a-z][a-z0-9_]*$"},
                        "additionalProperties": _metric_schema(strict)},
            "baseline": {"type": "object", "additionalProperties": False, "properties": {
                "windows_days": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 1}},
                "default_window": {"type": "integer", "minimum": 1},
                "min_days": {"type": "integer", "minimum": 1},
                "mad_flag_multiplier": {"type": "number", "exclusiveMinimum": 0}}},
            "confidence": {"type": "object", "additionalProperties": False, "properties": {
                "weights": {"type": "object", "additionalProperties": {"type": "number"}},
                "grades": {"type": "object", "additionalProperties": {"type": "number"}},
                "agreement_tolerance_pct": {"type": "number", "minimum": 0}}},
            "sources": {"type": "array", "items": _SOURCE},
            "reporting_timezone": {"type": "string", "minLength": 1},
            "unknown_types": {"type": "object"},
            "workouts": {"type": "object"},
            "activity_rings": {"type": "object"},
            "ecg": {"type": "object"},
            "labs": {"type": "object"},
        },
    }


REGISTRY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "devices": {"type": "array", "items": {
            "type": "object", "required": ["key"], "additionalProperties": False,
            "properties": {"key": {"type": "string", "minLength": 1}, "label": {"type": "string"},
                           "patterns": {"type": "array", "items": {"type": "string"}},
                           "active": {"type": "boolean"}}}},
        "ignored": {"type": "array", "items": {"type": "string"}},
        "fallback_key": {"type": "string", "minLength": 1},
        "ignored_mode": {"enum": ["drop", "store"]},
    },
}


class PolicyError(ValueError):
    """Every problem found, one per line, each prefixed with its path."""

    def __init__(self, problems: list[str], what: str):
        self.problems = list(problems)
        super().__init__(f"{what}: {len(self.problems)} problem(s)\n  " + "\n  ".join(self.problems))


def normalise_dates(obj: Any) -> Any:
    """Recursively render YAML date and datetime values as ISO strings
    (dates as YYYY-MM-DD). Returns a deep copy; inputs are not mutated."""
    if isinstance(obj, dict):
        return {k: normalise_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalise_dates(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.date().isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    return copy.deepcopy(obj)


def _path(err) -> str:
    parts = [str(p) for p in err.absolute_path]
    return ".".join(parts) if parts else "<root>"


def _schema_problems(schema: dict, cfg: Any) -> list[str]:
    v = Draft202012Validator(schema)
    return sorted(f"{_path(e)}: {e.message}" for e in v.iter_errors(cfg))


def _device_key_problems(name: str, spec: dict) -> list[str]:
    """Device keys of one metric. In priority and corroboration a key is a
    device key or `<device>:healthkit` (a device before the colon, the one
    known qualifier after it); derive.devices and the keys of sync_paths take
    plain device keys only. Whether a device exists is the registry's question
    (checked where the registry is loaded), not this one's."""
    out: list[str] = []
    want = f"'<device>:{HEALTHKIT_QUALIFIER}'"
    for field in QUALIFIED_KEY_LISTS:
        keys = spec.get(field)
        for k in keys if isinstance(keys, list) else []:
            if not isinstance(k, str) or ":" not in k:
                continue
            base, qualifier = split_device_key(k)
            if not base:
                out.append(f"metrics.{name}.{field}: {k!r} has no device before the colon (expected {want})")
            elif qualifier not in DEVICE_QUALIFIERS:
                out.append(f"metrics.{name}.{field}: {k!r} has an unknown qualifier {qualifier!r} "
                           f"(the only qualifier is {HEALTHKIT_QUALIFIER!r}, as in {want})")
    d = spec.get("derive")
    sp = spec.get("sync_paths")
    for field, keys in (("derive.devices", d.get("devices") if isinstance(d, dict) else None),
                        ("sync_paths", list(sp) if isinstance(sp, dict) else None)):
        for k in keys if isinstance(keys, list) else []:
            if isinstance(k, str) and ":" in k:
                out.append(f"metrics.{name}.{field}: {k!r} is a qualified key; {field} takes plain device keys "
                           "(a qualified key belongs in priority or corroboration)")
    return out


def validate_policy(cfg: dict, strict: bool = True, what: str | None = None) -> dict:
    """Validate a metric policy. strict=False validates an overlay as a patch
    (allowed keys and shapes, nothing required); strict=True validates a
    complete policy. Returns the date-normalised copy; raises PolicyError."""
    what = what or ("metric policy" if strict else "metric policy overlay")
    if not isinstance(cfg, dict):
        raise PolicyError([f"<root>: expected a mapping, got {type(cfg).__name__}"], what)
    norm = normalise_dates(cfg)
    problems = _schema_problems(policy_schema(strict), norm)
    metrics = norm.get("metrics") if isinstance(norm.get("metrics"), dict) else {}
    # Duplicate HealthKit mappings: two metrics cannot own one identifier.
    by_hk: dict[str, list[str]] = {}
    for name, spec in metrics.items():
        if isinstance(spec, dict) and isinstance(spec.get("hk"), str):
            by_hk.setdefault(spec["hk"], []).append(name)
    for hk, names in sorted(by_hk.items()):
        if len(names) > 1:
            problems.append(f"metrics: hk {hk} is mapped by more than one metric ({', '.join(sorted(names))})")
    # A snooze date must be a real calendar date, not just digits in the right places.
    for name, spec in sorted(metrics.items()):
        sn = spec.get("snooze_until") if isinstance(spec, dict) else None
        if isinstance(sn, str):
            try:
                date.fromisoformat(sn)
            except ValueError:
                problems.append(f"metrics.{name}.snooze_until: {sn!r} is not a calendar date")
    # Qualified device keys: in a patch too, because the overlay carries the priority lists.
    for name, spec in sorted(metrics.items()):
        if isinstance(spec, dict):
            problems.extend(_device_key_problems(name, spec))
    if strict:
        for name, spec in sorted(metrics.items()):
            d = spec.get("derive") if isinstance(spec, dict) else None
            if isinstance(d, dict) and d.get("from") not in metrics:
                problems.append(f"metrics.{name}.derive.from: {d.get('from')!r} is not a metric in this policy")
        b = norm.get("baseline") or {}
        if isinstance(b, dict) and b.get("windows_days"):
            # The EFFECTIVE default window (30 when absent) must be computed.
            eff = b.get("default_window", DEFAULT_WINDOW)
            if eff not in b["windows_days"]:
                problems.append(f"baseline.default_window: {eff} is not one of windows_days {b['windows_days']}")
    tz = norm.get("reporting_timezone")
    if isinstance(tz, str):
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            problems.append(f"reporting_timezone: {tz!r} is not a known IANA zone")
    if problems:
        raise PolicyError(sorted(set(problems)), what)
    return norm


DEVICE_LISTS = ("priority", "corroboration", "exercise_priority")   # lists of arbitration keys


def registry_problems(policy, registry) -> list[str]:
    """Problems between a merged policy and a merged source registry (design
    B15: the loader validates the real files, so a typo can never silently
    drop a device). `policy` is a MetricPolicy or a policy dict, `registry` a
    SourceRegistry or a registry dict. Checked:
    - every key in priority, corroboration, exercise_priority, derive.devices
      and the keys of sync_paths names a registry device (a qualified key by
      the device before its colon);
    - a qualified key `<device>:healthkit` comes with sync_paths for its
      device (which paths are the device's own; the rest is the copy);
    - derive.from is a daily metric with the same unit, and every derive
      device is in the metric's priority or corroboration list (else it is
      never read);
    - a history-only device (`active: false`) heads no metric that is not
      optional (a current day would wait on a device nobody wears).
    Returns the problems, one line each with its path; [] when they agree."""
    from heliosd.trust.policy import MetricPolicy          # here: policy.py imports this module
    from heliosd.trust.registry import SourceRegistry
    if not isinstance(policy, MetricPolicy):
        policy = MetricPolicy(policy)
    if not isinstance(registry, SourceRegistry):
        registry = SourceRegistry(registry)
    devices = {d["key"] for d in registry.devices} | {registry.fallback}
    out: list[str] = []

    def unknown(path: str, key: str, base: str | None = None) -> None:
        base = base or key
        near = difflib.get_close_matches(base, sorted(devices), n=1)
        hint = f"; did you mean {near[0]!r}?" if near else ""
        what = f"{key!r} names {base!r}, which" if base != key else repr(key)
        out.append(f"{path}: {what} is not a device in the source registry{hint}")

    for name, spec in sorted(policy.metrics.items()):
        sync = policy.sync_paths(name)
        for field in DEVICE_LISTS:
            for k in spec.get(field) or []:
                base, qualifier = split_device_key(k)
                if base not in devices:
                    unknown(f"metrics.{name}.{field}", k, base)
                if qualifier and base not in sync:
                    out.append(f"metrics.{name}.{field}: {k!r} needs sync_paths for {base!r} (the paths that are "
                               f"{base}'s own; its rows from other paths are the {qualifier} copy)")
        for k in sync:
            if k not in devices:
                unknown(f"metrics.{name}.sync_paths", k)
        der = policy.derive(name)
        if der:
            parent = der["from"]
            if parent not in policy.metrics or not policy.daily(parent):
                out.append(f"metrics.{name}.derive.from: {parent!r} is not a daily metric of this policy")
            elif policy.unit(parent) != policy.unit(name):
                out.append(f"metrics.{name}.derive.from: {parent!r} is in {policy.unit(parent)!r}, "
                           f"this metric in {policy.unit(name)!r}")
            listed = set(policy.priority(name)) | set(policy.corroboration(name) or [])
            for k in der["devices"]:
                if k not in devices:
                    unknown(f"metrics.{name}.derive.devices", k)
                elif k not in listed:
                    out.append(f"metrics.{name}.derive.devices: {k!r} is in neither priority nor corroboration, "
                               "so its rows are never read")
        prio = policy.priority(name)
        if prio and not spec.get("optional") and split_device_key(prio[0])[0] in registry.inactive:
            out.append(f"metrics.{name}.priority: its head {prio[0]!r} is a history-only device (active: false); "
                       "a metric that is not optional needs a current device first")
    return sorted(set(out))


def validate_policy_against_registry(policy, registry) -> list[str]:
    """registry_problems as a gate: raises PolicyError naming every problem
    (the daemon's startup refuses with the list; the rebuild tool stops);
    returns [] when the policy and the registry agree."""
    problems = registry_problems(policy, registry)
    if problems:
        raise PolicyError(problems, "metric policy against the source registry")
    return []


def validate_registry(cfg: dict, what: str = "source registry") -> dict:
    """Validate a source registry (default, overlay or merged: the shape is
    the same because its lists replace rather than merge). Returns a copy."""
    if not isinstance(cfg, dict):
        raise PolicyError([f"<root>: expected a mapping, got {type(cfg).__name__}"], what)
    norm = normalise_dates(cfg)
    problems = _schema_problems(REGISTRY_SCHEMA, norm)
    keys = [d.get("key") for d in norm.get("devices", []) if isinstance(d, dict)]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        problems.append(f"devices: duplicate key(s) {dupes}")
    if problems:
        raise PolicyError(sorted(set(problems)), what)
    return norm
