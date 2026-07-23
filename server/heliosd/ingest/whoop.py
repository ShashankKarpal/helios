"""Whoop puller (core, owner-approved): recovery, strain, sleep architecture,
sleep need, respiratory rate, rMSSD. OAuth against the owner's own free Whoop
developer app; tokens stored locally; ingress only, nothing leaves.

Whoop API v2 (v1 was removed 2025-10-01; see developer.whoop.com v1-v2 migration
guide). v2 keeps the score payload shapes this module reads; record ids moved to
UUIDs, which we do not depend on.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx

from heliosd.store import db

API = "https://api.prod.whoop.com/developer/v2"
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
SCOPES = "read:recovery read:sleep read:cycles read:profile offline"


def _local_naive(ts: str) -> datetime:
    """Parse a Whoop ISO8601 UTC timestamp, convert it to the Mac's local
    timezone, and drop tzinfo. Bucketing by local day makes Whoop recovery and
    sleep land on the same calendar day the rest of Helios calls 'today'. The
    old code bucketed by the raw UTC day, which for any timezone east of UTC
    fell on the previous calendar date and left the Today screen empty."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


class WhoopClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.token_path = Path(cfg["token_path"])

    # ---- OAuth ----
    def login_url(self, state: str = "helios-whoop-oauth") -> str:
        # Whoop requires state to be >= 8 characters for CSRF entropy, otherwise
        # it rejects the request with invalid_state before the consent screen.
        q = {"client_id": self.cfg["client_id"], "redirect_uri": self.cfg["redirect_uri"],
             "response_type": "code", "scope": SCOPES, "state": state}
        return f"{AUTH_URL}?{urlencode(q)}"

    def exchange_code(self, code: str) -> None:
        r = httpx.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code,
            "client_id": self.cfg["client_id"], "client_secret": self.cfg["client_secret"],
            "redirect_uri": self.cfg["redirect_uri"]}, timeout=30)
        r.raise_for_status()
        self._save_tokens(r.json())

    def _save_tokens(self, tokens: dict) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tokens["saved_at"] = datetime.now().isoformat()
        self.token_path.write_text(json.dumps(tokens))

    def _tokens(self) -> dict | None:
        if self.token_path.exists():
            return json.loads(self.token_path.read_text())
        return None

    def _access_token(self) -> str | None:
        t = self._tokens()
        if not t:
            return None
        age = (datetime.now() - datetime.fromisoformat(t["saved_at"])).total_seconds()
        if age > t.get("expires_in", 3600) - 300:
            r = httpx.post(TOKEN_URL, data={
                "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                "client_id": self.cfg["client_id"], "client_secret": self.cfg["client_secret"],
                "scope": "offline"}, timeout=30)
            r.raise_for_status()
            t = r.json() | {"saved_at": datetime.now().isoformat()}
            self._save_tokens(t)
        return t["access_token"]

    def _get(self, path: str, params: dict) -> dict:
        tok = self._access_token()
        if not tok:
            raise RuntimeError("Whoop not authorized. Visit /whoop/login first.")
        r = httpx.get(f"{API}{path}", params=params,
                      headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        r.raise_for_status()
        return r.json()

    def _paged(self, path: str, start: datetime, end: datetime) -> list[dict]:
        records, token = [], None
        while True:
            params = {"start": start.isoformat() + "Z", "end": end.isoformat() + "Z", "limit": 25}
            if token:
                params["nextToken"] = token
            page = self._get(path, params)
            records += page.get("records", [])
            token = page.get("next_token")
            if not token:
                return records


def _insert_sample(conn, metric: str, day: date, value: float, unit: str,
                   start: datetime, end: datetime) -> None:
    sid = f"wh:{metric}:{day.isoformat()}"
    db.execute(conn, """
        INSERT OR REPLACE INTO samples
          (sample_id, metric, hk_type, value, text_value, unit, start_ts, end_ts,
           source_name, device_key, sync_path)
        VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, 'WHOOP', 'whoop', 'whoop_live')""",
        [sid, metric, value, unit, start, end])


def pull(conn, client: WhoopClient, days: int = 8) -> dict:
    """Fetch trailing window; store native metrics as samples + raw cache."""
    end = datetime.now()
    start = end - timedelta(days=days)
    n = {"recovery": 0, "sleep": 0, "cycle": 0}

    for rec in client._paged("/recovery", start, end):
        sc = rec.get("score") or {}
        created = _local_naive(rec["created_at"])
        day = created.date()
        if sc.get("recovery_score") is not None:
            _insert_sample(conn, "recovery_score", day, float(sc["recovery_score"]), "%", created, created)
        if sc.get("hrv_rmssd_milli") is not None:
            _insert_sample(conn, "hrv_rmssd", day, float(sc["hrv_rmssd_milli"]), "ms", created, created)
        db.execute(conn, "INSERT OR REPLACE INTO whoop_cache (date, kind, payload) VALUES (?, 'recovery', ?)",
                   [day, json.dumps(rec)])
        n["recovery"] += 1

    for rec in client._paged("/activity/sleep", start, end):
        if rec.get("nap"):
            continue
        sc = rec.get("score") or {}
        s = _local_naive(rec["start"])
        e = _local_naive(rec["end"])
        day = e.date()
        stages = sc.get("stage_summary") or {}
        asleep_ms = sum(stages.get(k, 0) for k in
                        ("total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli",
                         "total_rem_sleep_time_milli"))
        if asleep_ms:
            _insert_sample(conn, "sleep_duration", day, round(asleep_ms / 3.6e6, 2), "h", s, e)
        if sc.get("respiratory_rate") is not None:
            _insert_sample(conn, "respiratory_rate", day, float(sc["respiratory_rate"]), "count/min", s, e)
        need = (sc.get("sleep_needed") or {}).get("baseline_milli")
        if need:
            _insert_sample(conn, "sleep_need", day, round(need / 3.6e6, 2), "h", s, e)
        db.execute(conn, "INSERT OR REPLACE INTO whoop_cache (date, kind, payload) VALUES (?, 'sleep', ?)",
                   [day, json.dumps(rec)])
        n["sleep"] += 1

    for rec in client._paged("/cycle", start, end):
        sc = rec.get("score") or {}
        s = _local_naive(rec["start"])
        day = s.date()
        if sc.get("strain") is not None:
            _insert_sample(conn, "strain", day, float(sc["strain"]), "score", s, s)
        db.execute(conn, "INSERT OR REPLACE INTO whoop_cache (date, kind, payload) VALUES (?, 'cycle', ?)",
                   [day, json.dumps(rec)])
        n["cycle"] += 1
    return n
