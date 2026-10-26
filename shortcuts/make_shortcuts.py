#!/usr/bin/env python3
"""Build the two iPhone Shortcuts of the 09:05 brief check as unsigned
shortcut files (shortcuts/BRIEF-CHECK.md has the design and the steps).

    python3 shortcuts/make_shortcuts.py [--config FILE] [--out DIR]

Helios Brief Check (09:05) writes "pending" to helios-check.txt, asks the Mac
for GET /api/brief-check, then writes "<date> <answer>" over it. Helios Brief
Guard (09:10) needs no network: unless today's line says "ok" or "early" it
shows a notification and sets a Clock alarm two minutes ahead, so a late
brief, a wrong answer and a Mac that never answered all ring.

The Mac's address comes from a local config file that git ignores
(shortcuts/local.json; copy local.example.json and put the Mac's Tailscale
MagicDNS name and port in it), so it never lands in a tracked file. The
built files carry the address too, which is why the default output folder,
shortcuts/build/, is ignored as well. Sign the output with `shortcuts sign`
on a Mac before importing it.

Standard library only, Python 3.9 compatible (macOS system Python).
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "local.json"
DEFAULT_OUT = HERE / "build"
EXAMPLE = "local.example.json"

CHECK_NAME = "Helios Brief Check"
GUARD_NAME = "Helios Brief Guard"
FILE = "helios-check.txt"            # in the Shortcuts folder of iCloud Drive
ALARM_LABEL = "Helios brief check"
ALARM_MINUTES = 2
# Shortcuts marks where a variable sits in a text field with this character.
OBJ = "￼"
# name[:port] of the Mac: letters, digits, dots and hyphens, then the port.
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?:[0-9]{1,5}$")


class ConfigError(Exception):
    """The local config is missing, unreadable, or still the example."""


def load_host(path: Path) -> str:
    """The Mac's "name:port" from the local config file. Refuses a missing
    file, the example's placeholder, a scheme, a path or a missing port: a
    shortcut built from a wrong address would fail every morning and ring."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{path} not found: copy {EXAMPLE} to local.json beside this script "
                          f"and put the Mac's Tailscale MagicDNS name and port in it") from None
    except OSError as e:
        raise ConfigError(f"{path}: cannot read it ({e.strerror})") from None
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: not valid JSON ({e.msg}, line {e.lineno})") from None
    host = cfg.get("host") if isinstance(cfg, dict) else None
    if not isinstance(host, str) or not host.strip():
        raise ConfigError(f'{path}: needs a "host" string, for example "name.example.ts.net:8420"')
    host = host.strip()
    if "<" in host or ">" in host:
        raise ConfigError(f"{path}: \"host\" is still the placeholder from {EXAMPLE}")
    if "://" in host or "/" in host:
        raise ConfigError(f'{path}: "host" is the name and port only, with no https:// and no path')
    if not HOST_RE.match(host):
        raise ConfigError(f'{path}: "host" must be name:port, for example "name.example.ts.net:8420"')
    if not 1 <= int(host.rsplit(":", 1)[1]) <= 65535:
        raise ConfigError(f'{path}: the port in "host" must be between 1 and 65535')
    return host


def brief_check_url(host: str) -> str:
    return f"https://{host}/api/brief-check"


def _uuid() -> str:
    return str(uuid.uuid4()).upper()


def _output(out_uuid: str, name: str) -> dict:
    return {"Type": "ActionOutput", "OutputUUID": out_uuid, "OutputName": name}


def _variable(out_uuid: str, name: str) -> dict:
    return {"Value": _output(out_uuid, name), "WFSerializationType": "WFTextTokenAttachment"}


def _tokens(parts: list) -> dict:
    """A text field: plain strings and variables (output dicts) in order."""
    text, ranges = "", {}
    for p in parts:
        if isinstance(p, str):
            text += p
        else:
            ranges["{%d, 1}" % len(text)] = p
            text += OBJ
    return {"Value": {"string": text, "attachmentsByRange": ranges}, "WFSerializationType": "WFTextTokenString"}


CURRENT_DATE = {"Type": "CurrentDate"}


def _action(ident: str, **params) -> dict:
    return {"WFWorkflowActionIdentifier": ident, "WFWorkflowActionParameters": params}


def _format_date(uid: str) -> dict:
    return _action("is.workflow.actions.format.date", UUID=uid, WFDateFormatStyle="Custom",
                   WFDateFormat="yyyy-MM-dd", WFDate=_tokens([CURRENT_DATE]))


def _text(uid: str, value) -> dict:
    return _action("is.workflow.actions.gettext", UUID=uid, WFTextActionText=value)


def _save(uid: str, src_uuid: str) -> dict:
    return _action("is.workflow.actions.documentpicker.save", UUID=uid, WFInput=_variable(src_uuid, "Text"),
                   WFAskWhereToSave=False, WFFileDestinationPath=FILE, WFSaveFileOverwrite=True)


def _if_contains(group: str, src_uuid: str, value_parts: list) -> dict:
    # WFCondition 99 is "contains".
    return _action("is.workflow.actions.conditional", GroupingIdentifier=group, WFControlFlowMode=0,
                   WFCondition=99, WFInput={"Type": "Variable", "Variable": _variable(src_uuid, "Text")},
                   WFConditionalActionString=_tokens(value_parts))


def _if_mode(group: str, mode: int) -> dict:
    # Mode 1 starts the Otherwise part, mode 2 is End If.
    return _action("is.workflow.actions.conditional", GroupingIdentifier=group, WFControlFlowMode=mode)


def _workflow(actions: list, color: int, glyph: int) -> dict:
    return {
        "WFWorkflowActions": actions,
        "WFWorkflowClientVersion": "2605.0.5",
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": color, "WFWorkflowIconGlyphNumber": glyph},
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": [],
    }


def build_check(url: str) -> dict:
    """Helios Brief Check: "pending" first, so a run that cannot reach the Mac
    (it stops at Get Contents of URL) leaves "pending" for the Guard; then
    "<date> <answer>" whatever the Mac answered."""
    fd, t_pending, s1, dl, t_line, s2 = (_uuid() for _ in range(6))
    return _workflow([
        _format_date(fd),
        _text(t_pending, "pending"),
        _save(s1, t_pending),
        _action("is.workflow.actions.downloadurl", UUID=dl, WFURL=url, WFHTTPMethod="GET", ShowHeaders=False),
        _text(t_line, _tokens([_output(fd, "Formatted Date"), " ", _output(dl, "Contents of URL")])),
        _save(s2, t_line),
    ], 4282601983, 59511)


def build_guard() -> dict:
    """Helios Brief Guard: quiet only when the file says "<today> ok" or
    "<today> early"; anything else (late, a wrong answer, "pending", an old
    date, no file) shows a notification and sets a Clock alarm."""
    fd, f, t, adj, g1, g2 = (_uuid() for _ in range(6))
    return _workflow([
        _format_date(fd),
        _action("is.workflow.actions.documentpicker.open", UUID=f, WFGetFilePath=FILE,
                WFShowFilePicker=False, WFFileErrorIfNotFound=False),
        _text(t, _tokens([_output(f, "File")])),
        _if_contains(g1, t, [_output(fd, "Formatted Date"), " ok"]),
        _if_mode(g1, 1),
        _if_contains(g2, t, [_output(fd, "Formatted Date"), " early"]),
        _if_mode(g2, 1),
        _action("is.workflow.actions.notification", WFNotificationActionTitle="Helios brief check",
                WFNotificationActionBody=_tokens(["The morning check did not pass. Last result: ",
                                                  _output(t, "Text")]),
                WFNotificationActionSound=True),
        _action("is.workflow.actions.adjustdate", UUID=adj, WFAdjustOperation="Add", WFDate=_tokens([CURRENT_DATE]),
                WFDuration={"Value": {"Magnitude": str(ALARM_MINUTES), "Unit": "min"},
                            "WFSerializationType": "WFQuantityFieldValue"}),
        # Clock's Create Alarm as an App Intent: proven on the owner's iPhone
        # (iOS 27) on 2026-10-23, and unattended on 2026-10-24 and 25.
        _action("com.apple.mobiletimer.MobileTimerSupport.CreateAlarmIntent", UUID=_uuid(),
                AppIntentDescriptor={"AppIntentIdentifier": "CreateAlarmIntent",
                                     "BundleIdentifier": "com.apple.mobiletimer.MobileTimerSupport",
                                     "Name": "Clock", "TeamIdentifier": "0000000000"},
                dateComponents=_tokens([_output(adj, "Adjusted Date")]), name=ALARM_LABEL, allowsSnooze=True),
        _if_mode(g2, 2),
        _if_mode(g1, 2),
    ], 4251333119, 59446)


def write(out_dir: Path, name: str, workflow: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.unsigned.shortcut"
    with open(path, "wb") as fh:
        plistlib.dump(workflow, fh, fmt=plistlib.FMT_BINARY)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the Helios brief check shortcuts (unsigned).")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                    help="local config with the Mac's host (default: shortcuts/local.json, ignored by git)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="output folder (default: shortcuts/build, ignored by git)")
    args = ap.parse_args(argv)
    try:
        host = load_host(args.config)
    except ConfigError as e:
        print(f"make_shortcuts: {e}", file=sys.stderr)
        return 2
    for name, workflow in ((CHECK_NAME, build_check(brief_check_url(host))), (GUARD_NAME, build_guard())):
        path = write(args.out, name, workflow)
        print(f"wrote {path} ({len(workflow['WFWorkflowActions'])} actions)")
    print("next: sign each file with `shortcuts sign --input <file> --output <name>.shortcut` "
          "on a Mac, then open the signed files (shortcuts/BRIEF-CHECK.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
