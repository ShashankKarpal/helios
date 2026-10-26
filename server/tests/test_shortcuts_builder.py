"""The 09:05 brief check shortcuts (shortcuts/make_shortcuts.py, design in
shortcuts/BRIEF-CHECK.md): the Check writes "pending" and then "<date>
<answer>"; the Guard rings unless today's line says ok or early. The Mac's
address comes only from the ignored local config, never from a tracked file.
Synthetic hosts only."""

from __future__ import annotations

import importlib.util
import json
import plistlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHORTCUTS = ROOT / "shortcuts"
HOST = "mac.example.ts.net:8420"
URL = "https://mac.example.ts.net:8420/api/brief-check"


def _load():
    spec = importlib.util.spec_from_file_location("make_shortcuts", SHORTCUTS / "make_shortcuts.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_shortcuts"] = mod
    spec.loader.exec_module(mod)
    return mod


ms = _load()


def _config(tmp_path: Path, content) -> Path:
    p = tmp_path / "local.json"
    p.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return p


def _ids(workflow: dict) -> list[str]:
    return [a["WFWorkflowActionIdentifier"] for a in workflow["WFWorkflowActions"]]


def _params(workflow: dict) -> list[dict]:
    return [a["WFWorkflowActionParameters"] for a in workflow["WFWorkflowActions"]]


def _text_parts(field: dict) -> list:
    """A token string back as its parts: plain text and (output name, uuid)."""
    value = field["Value"]
    s, ranges = value["string"], value["attachmentsByRange"]
    at = {int(re.match(r"\{(\d+), 1\}", k).group(1)): v for k, v in ranges.items()}
    parts, buf = [], ""
    for i, ch in enumerate(s):
        if ch == ms.OBJ:
            if buf:
                parts.append(buf)
                buf = ""
            v = at[i]
            parts.append((v.get("OutputName") or v["Type"], v.get("OutputUUID")))
        else:
            buf += ch
    if buf:
        parts.append(buf)
    assert len(at) == s.count(ms.OBJ)
    return parts


# ---- the local config

def test_a_missing_config_is_refused_and_names_the_example(tmp_path):
    with pytest.raises(ms.ConfigError, match="local.example.json"):
        ms.load_host(tmp_path / "local.json")


def test_the_tracked_example_holds_only_the_placeholder():
    example = SHORTCUTS / "local.example.json"
    assert set(json.loads(example.read_text(encoding="utf-8"))) == {"host"}
    with pytest.raises(ms.ConfigError, match="placeholder"):
        ms.load_host(example)


@pytest.mark.parametrize("content, why", [
    ({"host": "https://mac.example.ts.net:8420"}, "no https://"),
    ({"host": "mac.example.ts.net:8420/api/brief-check"}, "no https://"),
    ({"host": "mac.example.ts.net"}, "name:port"),
    ({"host": "mac example:8420"}, "name:port"),
    ({"host": "mac.example.ts.net:0"}, "between 1 and 65535"),
    ({"host": "mac.example.ts.net:70000"}, "between 1 and 65535"),
    ({"host": ""}, '"host" string'),
    ({"host": 8420}, '"host" string'),
    ({"hosts": HOST}, '"host" string'),
    ([HOST], '"host" string'),
    ("{not json", "not valid JSON"),
])
def test_a_wrong_host_is_refused(tmp_path, content, why):
    with pytest.raises(ms.ConfigError, match=re.escape(why)):
        ms.load_host(_config(tmp_path, content))


def test_a_good_host_is_read_and_trimmed(tmp_path):
    assert ms.load_host(_config(tmp_path, {"host": f"  {HOST} "})) == HOST
    assert ms.brief_check_url(HOST) == URL


# ---- the two shortcuts

def test_the_check_writes_pending_then_the_date_and_the_answer():
    wf = ms.build_check(URL)
    assert _ids(wf) == ["is.workflow.actions.format.date", "is.workflow.actions.gettext",
                        "is.workflow.actions.documentpicker.save", "is.workflow.actions.downloadurl",
                        "is.workflow.actions.gettext", "is.workflow.actions.documentpicker.save"]
    date, pending, save1, get, line, save2 = _params(wf)
    assert date["WFDateFormatStyle"] == "Custom" and date["WFDateFormat"] == "yyyy-MM-dd"
    assert pending["WFTextActionText"] == "pending"
    assert get["WFURL"] == URL and get["WFHTTPMethod"] == "GET" and "WFHTTPHeaders" not in get
    assert _text_parts(line["WFTextActionText"]) == [("Formatted Date", date["UUID"]), " ",
                                                     ("Contents of URL", get["UUID"])]
    for save, src in ((save1, pending), (save2, line)):
        assert save["WFFileDestinationPath"] == "helios-check.txt"
        assert save["WFSaveFileOverwrite"] is True and save["WFAskWhereToSave"] is False
        assert save["WFInput"]["Value"]["OutputUUID"] == src["UUID"]
    # "pending" is on disk before the Mac is asked: a run that cannot reach it
    # stops at the request and leaves "pending" for the Guard.
    assert _ids(wf).index("is.workflow.actions.documentpicker.save") < _ids(wf).index(
        "is.workflow.actions.downloadurl")


def test_the_guard_rings_unless_today_says_ok_or_early():
    wf = ms.build_guard()
    assert _ids(wf) == ["is.workflow.actions.format.date", "is.workflow.actions.documentpicker.open",
                        "is.workflow.actions.gettext",
                        "is.workflow.actions.conditional", "is.workflow.actions.conditional",
                        "is.workflow.actions.conditional", "is.workflow.actions.conditional",
                        "is.workflow.actions.notification", "is.workflow.actions.adjustdate",
                        "com.apple.mobiletimer.MobileTimerSupport.CreateAlarmIntent",
                        "is.workflow.actions.conditional", "is.workflow.actions.conditional"]
    p = _params(wf)
    date, get_file, text = p[0], p[1], p[2]
    assert get_file["WFGetFilePath"] == "helios-check.txt"
    # No picker and no error on a missing file: no file reads as empty, and the Guard rings.
    assert get_file["WFShowFilePicker"] is False and get_file["WFFileErrorIfNotFound"] is False
    assert _text_parts(text["WFTextActionText"]) == [("File", get_file["UUID"])]
    if_ok, else_ok, if_early, else_early = p[3:7]
    end_early, end_ok = p[10], p[11]
    for cond, word in ((if_ok, " ok"), (if_early, " early")):
        assert cond["WFControlFlowMode"] == 0 and cond["WFCondition"] == 99      # contains
        assert cond["WFInput"]["Variable"]["Value"]["OutputUUID"] == text["UUID"]
        assert _text_parts(cond["WFConditionalActionString"]) == [("Formatted Date", date["UUID"]), word]
    # The early test, the notification and the alarm sit in the Otherwise part
    # of the ok test; the notification and the alarm in the Otherwise part of
    # the early test; End If closes the inner block first.
    assert if_ok["GroupingIdentifier"] == else_ok["GroupingIdentifier"] == end_ok["GroupingIdentifier"]
    assert if_early["GroupingIdentifier"] == else_early["GroupingIdentifier"] == end_early["GroupingIdentifier"]
    assert if_ok["GroupingIdentifier"] != if_early["GroupingIdentifier"]
    assert [else_ok["WFControlFlowMode"], else_early["WFControlFlowMode"]] == [1, 1]
    assert [end_early["WFControlFlowMode"], end_ok["WFControlFlowMode"]] == [2, 2]
    note, adjust, alarm = p[7], p[8], p[9]
    assert note["WFNotificationActionSound"] is True
    assert _text_parts(note["WFNotificationActionBody"]) == [
        "The morning check did not pass. Last result: ", ("Text", text["UUID"])]
    assert adjust["WFAdjustOperation"] == "Add"
    assert adjust["WFDuration"]["Value"] == {"Magnitude": "2", "Unit": "min"}
    assert _text_parts(alarm["dateComponents"]) == [("Adjusted Date", adjust["UUID"])]
    assert alarm["name"] == "Helios brief check"
    assert alarm["AppIntentDescriptor"]["BundleIdentifier"] == "com.apple.mobiletimer.MobileTimerSupport"


@pytest.mark.parametrize("build", [lambda: ms.build_check(URL), ms.build_guard])
def test_every_variable_points_at_an_earlier_action(build):
    """A variable wired to a missing or later action shows as an empty field
    on the phone, and the Guard would then compare against nothing."""
    seen: set[str] = set()

    def refs(obj):
        if isinstance(obj, dict):
            if obj.get("Type") == "ActionOutput":
                yield obj["OutputUUID"]
            for v in obj.values():
                yield from refs(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from refs(v)

    n = 0
    for params in _params(build()):
        for ref in refs(params):
            assert ref in seen, f"{ref} is not the output of an earlier action"
            n += 1
        if "UUID" in params:
            assert params["UUID"] not in seen
            seen.add(params["UUID"])
    assert n >= 3


# ---- the command

def test_main_writes_two_binary_shortcut_files(tmp_path, capsys):
    out = tmp_path / "build"
    assert ms.main(["--config", str(_config(tmp_path, {"host": HOST})), "--out", str(out)]) == 0
    files = sorted(p.name for p in out.iterdir())
    assert files == ["Helios Brief Check.unsigned.shortcut", "Helios Brief Guard.unsigned.shortcut"]
    for name in files:
        raw = (out / name).read_bytes()
        assert raw.startswith(b"bplist00")
        wf = plistlib.loads(raw)
        assert wf["WFWorkflowMinimumClientVersion"] == 900 and wf["WFWorkflowActions"]
    check = plistlib.loads((out / files[0]).read_bytes())
    assert _params(check)[3]["WFURL"] == URL
    assert "shortcuts sign" in capsys.readouterr().out


def test_main_without_a_usable_config_writes_nothing(tmp_path, capsys):
    out = tmp_path / "build"
    assert ms.main(["--config", str(tmp_path / "local.json"), "--out", str(out)]) == 2
    assert not out.exists()
    assert "local.example.json" in capsys.readouterr().err
    example = str(SHORTCUTS / "local.example.json")
    assert ms.main(["--config", example, "--out", str(out)]) == 2
    assert not out.exists()


# ---- the Mac's address stays out of tracked files

def test_the_local_config_and_the_built_files_are_ignored_by_git():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "shortcuts/local.json" in lines and "shortcuts/build/" in lines
    assert ms.DEFAULT_CONFIG == SHORTCUTS / "local.json" and ms.DEFAULT_OUT == SHORTCUTS / "build"


MAGICDNS = re.compile(r"[A-Za-z0-9.-]*[A-Za-z0-9-]\.ts\.net\b")
TAILNET_V4 = re.compile(r"\b100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b")


def _real_magicdns(text: str) -> list[str]:
    """MagicDNS names other than the synthetic ones under example.ts.net."""
    return [m for m in MAGICDNS.findall(text) if not m.endswith(".example.ts.net")]


def test_the_magicdns_check_tells_a_real_name_from_an_example():
    assert _real_magicdns('for example "name.example.ts.net:8420"') == []
    assert _real_magicdns("https://mac.tail0a1b2c.ts.net:8420/x") == ["mac.tail0a1b2c.ts.net"]
    assert _real_magicdns("host mac.example.ts.net.attacker.ts.net") != []
    assert TAILNET_V4.search("at 100.101.102.103 now") and not TAILNET_V4.search("100.200.1.1")


def test_no_shortcuts_file_outside_the_ignored_ones_names_a_tailnet_host():
    """The repo is public: a MagicDNS name or a tailnet address in shortcuts/
    would publish the Mac's address. Only synthetic names under
    example.ts.net may appear (the generator's error messages use one)."""
    ignored = {SHORTCUTS / "local.json"}
    read = 0
    for path in sorted(SHORTCUTS.rglob("*")):
        if not path.is_file() or path in ignored or (SHORTCUTS / "build") in path.parents:
            continue
        if path.suffix not in {".md", ".py", ".json", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8")
        read += 1
        assert not _real_magicdns(text), f"{path.name} names a MagicDNS host"
        assert not TAILNET_V4.search(text), f"{path.name} names a tailnet address"
    assert read >= 4        # BRIEF-CHECK.md, LOG-TO-HELIOS.md, make_shortcuts.py, local.example.json
