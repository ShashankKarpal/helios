"""Hardening round 2026-10-28, H6: no tracked file names the owner's machines.
The docs, the example config, an MCP comment and the Bridge's default host and
ATS exception named the M4's Bonjour (.local) hostname; they now use the
placeholder mac-name.local. The tip only: history is not rewritten.

This test reads every tracked text file (git ls-files) and holds no real name
itself: any Bonjour name (a word, then dot local) must be a listed placeholder, any MagicDNS tailnet
suffix (tail plus six hex digits, .ts.net) must be a listed synthetic one, and
no word may hash to one of the owner's machine names (sha256, first 16 hex)."""

from __future__ import annotations

import hashlib
import re
import subprocess

from heliosd.config import REPO_ROOT

LOCAL_HOST = re.compile(r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9-]*)\.local\b")
TAILNET = re.compile(r"tail[0-9a-f]{6}\.ts\.net", re.I)
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
PLACEHOLDER_LOCAL = {"mac-name"}
SYNTHETIC_TAILNET = {"tail0a1b2c.ts.net"}           # the shortcut builder's own tests
OWNER_NAME_SHA = {"7c6d77c7513fd78f", "83a54b9615e7c78e"}


def _sha(word: str) -> str:
    return hashlib.sha256(word.lower().encode()).hexdigest()[:16]


def owner_names_in(text: str) -> list[str]:
    """What in `text` names the owner's machines (the matched text)."""
    found = [m.group(0) for m in LOCAL_HOST.finditer(text) if m.group(1).lower() not in PLACEHOLDER_LOCAL]
    found += [m.group(0) for m in TAILNET.finditer(text) if m.group(0).lower() not in SYNTHETIC_TAILNET]
    found += [w for w in WORD.findall(text) if _sha(w) in OWNER_NAME_SHA]
    return found


# Outside a git checkout (a source archive) every file is read instead,
# without the folders that tools fill (never skipped silently: the count of
# files read is asserted below).
GENERATED = {".git", "node_modules", "dist", ".venv", ".build", "build", "__pycache__", ".pytest_cache"}


def _tracked():
    if (REPO_ROOT / ".git").exists():
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True)
        return [p for p in out.stdout.decode().split("\0") if p]
    files = []
    for path in sorted(REPO_ROOT.rglob("*")):
        rel = path.relative_to(REPO_ROOT)
        if path.is_file() and not GENERATED.intersection(rel.parts):
            files.append(str(rel))
    return files


def test_no_tracked_file_names_the_owner_s_machines():
    files = _tracked()
    assert len(files) > 100                           # the whole tree was listed, not an empty listing
    hits, read = [], 0
    for rel in files:
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue                                  # binaries (icons, fonts) and removed files
        read += 1
        for n, line in enumerate(text.splitlines(), 1):
            if owner_names_in(line):
                hits.append(f"{rel}:{n}")
    assert read > 100
    assert hits == [], hits


def test_the_check_finds_what_it_must():
    assert owner_names_in("serves https://somebody-mac" + ".local:8420")       # built, so this file stays clean
    assert owner_names_in("https://x.tail9f8e7d" + ".ts.net:8420")
    assert not owner_names_in("https://mac-name.local:8420, ~/.local/bin, com.shanky.helios.heliosd")
    assert not owner_names_in("https://mac.tail0a1b2c.ts.net:8420")
