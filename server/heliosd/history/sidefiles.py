"""The export's side files (single-source plan v2, Phase 2): ECG CSVs, GPX
routes and the export folder's sha256 manifest.

An ECG CSV is a header of key,value lines (name, date of birth, recorded
date, classification, symptoms, software version, device, sample rate, lead,
unit) and one voltage per line. The sampling rate is read per file, never
assumed (512 and 514.039 Hz both occur). The name and the date of birth are
read past and never returned. An ECG's identity is its recorded instant in
UTC; voltage_sha (the voltage lines as printed) tells whether two files hold
the same recording, as the June and July exports do with different printed
offsets.

A GPX route is one track of one segment of points with time, elevation and
the speed, course and accuracy extensions. Its identity is the sha256 of the
points as printed, so the same route under another file name (an export made
in another zone names files by local start time) is the same route.
Anything outside those shapes raises UnknownShape."""

from __future__ import annotations

import csv
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from heliosd.history.export_xml import UnknownShape, number, parse_ts

ECG_REQUIRED = ("Recorded Date", "Sample Rate", "Lead", "Unit")
ECG_KNOWN = frozenset({"Name", "Date of Birth", "Recorded Date", "Classification", "Symptoms", "Software Version",
                       "Device", "Sample Rate", "Lead", "Unit"})
ECG_PRIVATE = frozenset({"Name", "Date of Birth"})
_RATE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*hertz\s*$", re.IGNORECASE)
_VOLT_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
GPX_EXT = ("speed", "course", "hAcc", "vAcc")
MANIFEST_NAMES = ("MANIFEST*.sha256", "_manifest.sha256")


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def ecg_id_for(recorded_utc: datetime) -> str:
    return "ecg:" + recorded_utc.strftime("%Y%m%dT%H%M%SZ")


def read_ecg(path: str | Path) -> dict:
    header: dict[str, str] = {}
    volts: list[str] = []
    in_data = False
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            cells = [c.strip() for c in row]
            if not any(cells):
                if in_data:
                    raise UnknownShape(f"{Path(path).name}: an empty line inside the voltages")
                continue
            if _VOLT_RE.match(cells[0]):
                if any(cells[1:]):
                    raise UnknownShape(f"{Path(path).name}: more than one value on a voltage line")
                volts.append(cells[0])
                in_data = True
                continue
            if in_data:
                raise UnknownShape(f"{Path(path).name}: a header line after the voltages")
            key = cells[0]
            if key not in ECG_KNOWN:
                raise UnknownShape(f"{Path(path).name}: unknown header line {key[:30]!r}")
            header[key] = row[1].strip() if len(row) > 1 else ""
    missing = [k for k in ECG_REQUIRED if k not in header]
    if missing:
        raise UnknownShape(f"{Path(path).name}: header lacks {missing}")
    m = _RATE_RE.match(header["Sample Rate"])
    if not m:
        raise UnknownShape(f"{Path(path).name}: sample rate not in hertz")
    recorded_utc, offset = parse_ts(header["Recorded Date"])
    if recorded_utc is None:
        raise UnknownShape(f"{Path(path).name}: recorded date not parseable")
    if not volts:
        raise UnknownShape(f"{Path(path).name}: no voltages")
    return {"ecg_id": ecg_id_for(recorded_utc), "recorded_raw": header["Recorded Date"],
            "recorded_utc": recorded_utc, "print_offset_min": offset,
            "classification": header.get("Classification"), "symptoms": header.get("Symptoms"),
            "software_version": header.get("Software Version"), "device": header.get("Device"),
            "sample_rate_hz": float(m.group(1)), "sample_rate_raw": header["Sample Rate"],
            "lead": header["Lead"], "unit": header["Unit"], "n_samples": len(volts),
            "voltage_sha": hashlib.sha256("\n".join(volts).encode("utf-8")).hexdigest(),
            "microvolts": [float(v) for v in volts],
            "header_keys": sorted(k for k in header if k not in ECG_PRIVATE)}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _gpx_time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def read_gpx(path: str | Path) -> dict:
    root = ET.parse(str(path)).getroot()
    if _local(root.tag) != "gpx":
        raise UnknownShape(f"{Path(path).name}: root is not gpx")
    trks = [e for e in root if _local(e.tag) == "trk"]
    if len(trks) != 1:
        raise UnknownShape(f"{Path(path).name}: {len(trks)} tracks")
    segs = [e for e in trks[0] if _local(e.tag) == "trkseg"]
    if len(segs) != 1:
        raise UnknownShape(f"{Path(path).name}: {len(segs)} track segments")
    points: list[dict] = []
    h = hashlib.sha256()
    for seq, pt in enumerate(e for e in segs[0] if _local(e.tag) == "trkpt"):
        raw = {"lat": pt.get("lat"), "lon": pt.get("lon"), "ele": None, "time": None}
        raw.update({k: None for k in GPX_EXT})
        for c in pt:
            name = _local(c.tag)
            if name in ("ele", "time"):
                raw[name] = (c.text or "").strip()
            elif name == "extensions":
                for x in c:
                    xn = _local(x.tag)
                    if xn not in GPX_EXT:
                        raise UnknownShape(f"{Path(path).name}: unknown extension {xn}")
                    raw[xn] = (x.text or "").strip()
            else:
                raise UnknownShape(f"{Path(path).name}: unknown point child {name}")
        h.update(("|".join("" if raw[k] is None else raw[k] for k in ("lat", "lon", "ele", "time", *GPX_EXT)) + "\n")
                 .encode("utf-8"))
        points.append({"seq": seq, "time_raw": raw["time"], "time_utc": _gpx_time(raw["time"]),
                       "lat": number(raw["lat"]), "lon": number(raw["lon"]), "ele": number(raw["ele"]),
                       "speed": number(raw["speed"]), "course": number(raw["course"]),
                       "h_acc": number(raw["hAcc"]), "v_acc": number(raw["vAcc"])})
    if not points:
        raise UnknownShape(f"{Path(path).name}: no track points")
    times = [p["time_utc"] for p in points if p["time_utc"] is not None]
    return {"route_id": "rt:" + h.hexdigest()[:24], "points": points, "n_points": len(points),
            "first_utc": min(times) if times else None, "last_utc": max(times) if times else None}


def find_manifest(folder: str | Path) -> Path | None:
    folder = Path(folder)
    for pattern in MANIFEST_NAMES:
        hits = sorted(folder.glob(pattern))
        if hits:
            return hits[-1]
    return None


def verify_manifest(folder: str | Path) -> dict:
    """Check every file the folder's sha256 manifest lists. Returns counts and the paths that are missing
    or differ; the caller decides whether to stop. No manifest: {'manifest': None}."""
    folder = Path(folder)
    man = find_manifest(folder)
    if man is None:
        return {"manifest": None, "listed": 0, "ok": 0, "mismatch": [], "missing": [], "digests": {}}
    listed, ok, mismatch, missing, digests = 0, 0, [], [], {}
    for line in man.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([0-9a-f]{64})\s+\*?(.+)$", line)
        if not m:
            raise UnknownShape(f"{man.name}: a line that is not '<sha256>  <path>'")
        listed += 1
        rel = m.group(2).strip()
        p = folder / rel
        if not p.is_file():
            missing.append(rel)
            continue
        d = sha256_file(p)
        digests[rel] = d
        if d == m.group(1):
            ok += 1
        else:
            mismatch.append(rel)
    return {"manifest": man.name, "listed": listed, "ok": ok, "mismatch": mismatch, "missing": missing,
            "digests": digests}
