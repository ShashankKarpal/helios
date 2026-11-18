"""tools/helios_ca.sh: the private Helios root and its leaves.

The root's name constraints are the point: a copy of the root key can only sign
certificates for the Helios machines by name, never for another site, and for no
address but 127.0.0.1 (every client reaches Helios by name). These tests build
a root from a made-up tailnet (a fake `tailscale status --json`), so they never
read the real tailnet, the real CA folder or ~/Helios. They run OpenSSL 3 or
newer; LibreSSL (macOS /usr/bin/openssl) cannot write these extensions.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "server" / "tools" / "helios_ca.sh"

# A made-up tailnet: no real name or address of any machine.
SUFFIX = "example-tailnet.ts.net"
MAC, RELAY = "mac-name", "relay-name"
MAC_IP, RELAY_IP = "100.101.102.103", "100.90.80.70"
STATUS = {
    "MagicDNSSuffix": SUFFIX,
    "Self": {"HostName": MAC, "DNSName": f"{MAC}.{SUFFIX}.", "TailscaleIPs": [MAC_IP, "fd7a:115c:a1e0::1"]},
    "Peer": {
        "nodekey:a": {"HostName": RELAY, "DNSName": f"{RELAY}.{SUFFIX}.", "TailscaleIPs": [RELAY_IP, "fd7a:115c:a1e0::2"]},
        "nodekey:b": {"HostName": "phone", "DNSName": f"phone.{SUFFIX}.", "TailscaleIPs": ["100.64.0.9"]},
    },
}


def _openssl() -> str:
    for cand in (os.environ.get("HELIOS_OPENSSL"), "/opt/homebrew/bin/openssl", shutil.which("openssl")):
        if cand and Path(cand).is_file():
            out = subprocess.run([cand, "version"], capture_output=True, text=True).stdout
            m = re.match(r"OpenSSL (\d+)\.", out)
            if m and int(m.group(1)) >= 3:
                return cand
    pytest.fail("these tests need OpenSSL 3 or newer (set HELIOS_OPENSSL)")


OPENSSL = None


@pytest.fixture()
def env(tmp_path):
    global OPENSSL
    OPENSSL = OPENSSL or _openssl()
    status = tmp_path / "status.json"
    status.write_text(json.dumps(STATUS))
    e = dict(os.environ)
    e.update({
        "HELIOS_CA_DIR": str(tmp_path / "ca"),
        "HELIOS_CERTS_DIR": str(tmp_path / "certs"),
        "HELIOS_CA_TAILSCALE_JSON": str(status),
        "HELIOS_CA_NO_TMUTIL": "1",
        "HELIOS_OPENSSL": OPENSSL,
        "TMPDIR": str(tmp_path / "tmp"),
    })
    (tmp_path / "tmp").mkdir()
    return e, tmp_path


def run(e, *args, check=True):
    r = subprocess.run(["bash", str(SCRIPT), *args], env=e, capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert "PRIVATE KEY" not in out, "the script printed key material"
    if check:
        assert r.returncode == 0, out
    return r


def text(path: Path) -> str:
    return subprocess.run([OPENSSL, "x509", "-in", str(path), "-noout", "-text"],
                          capture_output=True, text=True, check=True).stdout


def verify(root: Path, leaf: Path, name: str | None = None) -> subprocess.CompletedProcess:
    args = [OPENSSL, "verify", "-x509_strict", "-purpose", "sslserver", "-CAfile", str(root)]
    if name:
        args += ["-verify_hostname", name]
    return subprocess.run(args + [str(leaf)], capture_output=True, text=True)


def days(leaf: Path) -> float:
    def at(flag):
        v = subprocess.run([OPENSSL, "x509", "-in", str(leaf), "-noout", flag],
                           capture_output=True, text=True, check=True).stdout.split("=", 1)[1].strip()
        return datetime.strptime(v, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return (at("-enddate") - at("-startdate")).total_seconds() / 86400


def test_script_exists_and_is_bash_32_safe():
    assert SCRIPT.is_file(), "tools/helios_ca.sh is missing"
    src = SCRIPT.read_text()
    assert "mapfile" not in src and "declare -A" not in src, "macOS bash 3.2 has no mapfile or associative arrays"
    assert not re.search(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b", src.replace("100.64.0.0", "")), \
        "no tailnet address may be written into the script"
    assert ".ts.net" not in src.replace("*.ts.net", ""), "no tailnet name may be written into the script"


def test_root_name_constraints_are_critical_and_exact(env):
    e, tmp = env
    run(e, "init", "--relay", RELAY)
    root = tmp / "ca" / "root.pem"
    t = text(root)
    assert re.search(r"X509v3 Name Constraints: critical", t), "nameConstraints must be critical"
    permitted = t.split("Permitted:", 1)[1].split("X509v3", 1)[0]
    dns = sorted(re.findall(r"DNS:(\S+)", permitted))
    ips = re.findall(r"IP:(\S+)", permitted)
    assert dns == sorted([f"{MAC}.local", MAC, f"{RELAY}.local", RELAY, SUFFIX])
    assert ips == ["127.0.0.1/255.255.255.255"], "the IP subtree is required: without one, IP SANs are unconstrained"
    assert "Excluded:" not in t
    assert re.search(r"X509v3 Basic Constraints: critical\s+CA:TRUE, pathlen:0", t)
    assert re.search(r"X509v3 Key Usage: critical\s+Certificate Sign, CRL Sign", t)
    assert "ASN1 OID: prime256v1" in t
    assert 3650 <= days(root) <= 3653


def test_a_root_without_a_relay_names_only_this_mac(env):
    e, tmp = env
    run(e, "init")
    permitted = text(tmp / "ca" / "root.pem").split("Permitted:", 1)[1].split("X509v3", 1)[0]
    assert sorted(re.findall(r"DNS:(\S+)", permitted)) == sorted([f"{MAC}.local", MAC, SUFFIX])
    assert re.findall(r"IP:(\S+)", permitted) == ["127.0.0.1/255.255.255.255"]
    r = run(e, "issue", "relay", check=False)
    assert r.returncode != 0 and "names no relay" in r.stderr
    run(e, "issue", "mac")


@pytest.mark.parametrize("bad", ["app", "dev", "xn--p1ai"])
def test_init_refuses_a_bare_name_that_could_be_a_public_tld(env, bad):
    e, tmp = env
    status = json.loads(Path(e["HELIOS_CA_TAILSCALE_JSON"]).read_text())
    status["Self"] = dict(status["Self"], HostName=bad, DNSName=f"{bad}.{SUFFIX}.")
    Path(e["HELIOS_CA_TAILSCALE_JSON"]).write_text(json.dumps(status))
    r = run(e, "init", check=False)
    assert r.returncode != 0 and "top-level domain" in r.stderr
    assert not (tmp / "ca" / "root.pem").exists()


def test_canary_refuses_an_existing_or_protected_folder(env):
    e, tmp = env
    run(e, "init", "--relay", RELAY)
    existing = tmp / "exists"
    existing.mkdir(mode=0o755)
    r = run(e, "canary", str(existing), check=False)
    assert r.returncode != 0 and stat.S_IMODE(existing.stat().st_mode) == 0o755
    (tmp / "certs").mkdir(mode=0o700)
    r = run(e, "canary", str(tmp / "certs" / "canary"), check=False)
    assert r.returncode != 0 and "never goes into the CA or certs folder" in r.stderr
    assert not (tmp / "certs" / "canary").exists()


def test_days_left_flags_a_file_that_is_not_a_certificate(env):
    e, tmp = env
    run(e, "init", "--relay", RELAY)
    (tmp / "certs").mkdir(mode=0o700)
    (tmp / "certs" / "broken.pem").write_text("not a certificate\n")
    r = run(e, "days-left", check=False)
    assert r.returncode == 1 and re.search(r"broken\.pem\s+UNREADABLE", r.stdout), r.stdout


def test_no_key_is_ever_made_in_the_system_temp_folder(env):
    e, tmp = env
    run(e, "init", "--relay", RELAY)
    run(e, "issue", "mac")
    run(e, "canary", str(tmp / "canary"))
    assert list((tmp / "tmp").rglob("*")) == [], "TMPDIR must stay empty: keys are made in their own folder"
    assert not list((tmp / "ca").glob(".helios-ca-new.*")) and not list((tmp / "certs" / "staged").glob(".helios-ca-new.*"))


def test_init_label_names_the_root_and_is_checked(env):
    e, tmp = env
    r = run(e, "init", "--relay", RELAY, "--label", "2026-11-17b", check=False)
    assert r.returncode == 0, r.stderr
    assert "CN=Helios Private Root 2026-11-17b" in subprocess.run(
        [OPENSSL, "x509", "-in", str(tmp / "ca" / "root.pem"), "-noout", "-subject", "-nameopt", "compat"],
        capture_output=True, text=True, check=True).stdout
    shutil.rmtree(tmp / "ca")
    r = run(e, "init", "--label", "bad label/x", check=False)
    assert r.returncode != 0 and not (tmp / "ca" / "root.pem").exists()


def test_ca_folder_and_key_modes(env):
    e, tmp = env
    run(e, "init", "--relay", RELAY)
    ca = tmp / "ca"
    assert stat.S_IMODE(ca.stat().st_mode) == 0o700
    assert stat.S_IMODE((ca / "root-key.pem").stat().st_mode) == 0o400
    assert sorted(p.name for p in ca.iterdir()) == ["root-key.pem", "root.pem"]


def test_init_refuses_to_replace_a_root(env):
    e, tmp = env
    run(e, "init", "--relay", RELAY)
    before = (tmp / "ca" / "root.pem").read_bytes()
    r = run(e, "init", "--relay", RELAY, check=False)
    assert r.returncode != 0
    assert (tmp / "ca" / "root.pem").read_bytes() == before


def test_init_refuses_an_unknown_relay_peer(env):
    e, tmp = env
    r = run(e, "init", "--relay", "no-such-peer", check=False)
    assert r.returncode != 0
    assert not (tmp / "ca" / "root.pem").exists()


def test_leaves_carry_todays_names_and_verify(env):
    e, tmp = env
    run(e, "init", "--relay", RELAY)
    run(e, "issue", "mac")
    run(e, "issue", "relay")
    root, staged = tmp / "ca" / "root.pem", tmp / "certs" / "staged"
    mac, relay = staged / f"{MAC}.local.pem", staged / f"{RELAY}-relay.pem"
    t = text(mac)
    assert re.findall(r"(?:DNS|IP Address):[^,\s]+", t.split("Subject Alternative Name:", 1)[1].split("\n")[1]) == \
        [f"DNS:{MAC}.local", f"DNS:{MAC}.{SUFFIX}"]
    t2 = text(relay)
    assert re.findall(r"(?:DNS|IP Address):[^,\s]+", t2.split("Subject Alternative Name:", 1)[1].split("\n")[1]) == \
        [f"DNS:{RELAY}.{SUFFIX}", f"DNS:{RELAY}"]
    for leaf, k in ((mac, f"{MAC}.local-key.pem"), (relay, f"{RELAY}-relay-key.pem")):
        lt = text(leaf)
        assert "TLS Web Server Authentication" in lt
        assert "IP Address:" not in lt, "leaves carry no IP address: every client uses a name"
        assert re.search(r"X509v3 Basic Constraints: critical\s+CA:FALSE", lt)
        assert "ASN1 OID: prime256v1" in lt
        assert 819 <= days(leaf) <= 820.01, "Apple caps TLS server certificates at 825 days"
        assert stat.S_IMODE((staged / k).stat().st_mode) == 0o600
    assert verify(root, mac, f"{MAC}.local").returncode == 0
    assert verify(root, mac, f"{MAC}.{SUFFIX}").returncode == 0
    assert verify(root, relay, f"{RELAY}.{SUFFIX}").returncode == 0
    # issue never touches the live file names beside staged/
    assert not (tmp / "certs" / f"{MAC}.local.pem").exists()


def test_a_canary_with_an_out_of_scope_name_is_rejected(env):
    e, tmp = env
    run(e, "init", "--relay", RELAY)
    out = tmp / "canary"
    r = run(e, "canary", str(out))
    assert "REJECTED" in r.stdout and "IP canary REJECTED" in r.stdout and "control leaf passed" in r.stdout
    for k in ("canary-key.pem", "control-key.pem", "ipcanary-key.pem"):
        assert not (out / k).exists(), "canary must never leave a usable key behind"
    ipleaf = out / "ipcanary.pem"
    assert f"IP Address:{MAC_IP}" in text(ipleaf)
    v = verify(tmp / "ca" / "root.pem", ipleaf, f"{MAC}.local")
    assert v.returncode != 0 and "permitted subtree violation" in (v.stdout + v.stderr).lower(), \
        "a tailnet address (inside 100.64.0.0/10) must be refused by the root"
    leaf = out / "canary.pem"
    assert "DNS:www.example.com" in text(leaf)
    v = verify(tmp / "ca" / "root.pem", leaf, f"{MAC}.local")
    # OpenSSL error 47, X509_V_ERR_PERMITTED_VIOLATION: the reason must be the constraint itself.
    assert v.returncode != 0 and "permitted subtree violation" in (v.stdout + v.stderr).lower()


def _control_root(tmp):
    key, root = tmp / "ctl-key.pem", tmp / "ctl.pem"
    subprocess.run([OPENSSL, "req", "-x509", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(key), "-out", str(root), "-days", "30", "-subj", "/CN=control",
                    "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign"], check=True, capture_output=True)
    return root, key


def test_control_root_without_constraints_accepts_the_same_names(env):
    """Proves the rejection above comes from the name constraints, not from anything else."""
    e, tmp = env
    ctl, ctl_key = _control_root(tmp)
    key, csr = tmp / "leaf-key.pem", tmp / "leaf.csr"
    subprocess.run([OPENSSL, "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(key), "-subj", "/CN=x", "-out", str(csr)], check=True, capture_output=True)
    ext = tmp / "ext.cnf"
    ext.write_text("[x]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                   "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n"
                   f"subjectAltName=DNS:{MAC}.local,DNS:www.example.com,IP:{MAC_IP}\n")
    leaf = tmp / "ctl-leaf.pem"
    subprocess.run([OPENSSL, "x509", "-req", "-in", str(csr), "-CA", str(ctl), "-CAkey", str(ctl_key), "-days", "30",
                    "-extfile", str(ext), "-extensions", "x", "-out", str(leaf)], check=True, capture_output=True)
    assert verify(ctl, leaf, f"{MAC}.local").returncode == 0


def test_canary_reports_accepted_for_a_root_without_constraints(env):
    e, tmp = env
    ca = tmp / "ca"
    ca.mkdir(mode=0o700)
    ctl, ctl_key = _control_root(tmp)
    shutil.copy(ctl, ca / "root.pem"); shutil.copy(ctl_key, ca / "root-key.pem")
    r = run(e, "canary", str(tmp / "canary"), check=False)
    assert r.returncode == 1 and "ACCEPTED" in r.stdout and "REJECTED" not in r.stdout


def test_canary_never_claims_success_when_tailscale_cannot_be_read(env):
    e, tmp = env
    run(e, "init", "--relay", RELAY)
    e = dict(e, HELIOS_CA_TAILSCALE_JSON=str(tmp / "missing.json"))
    r = run(e, "canary", str(tmp / "canary"), check=False)
    assert r.returncode != 0 and "REJECTED" not in r.stdout
    r = run(e, "issue", "relay", check=False)
    assert r.returncode != 0 and "cannot read this Mac" in r.stderr


def test_issue_refuses_when_the_root_does_not_cover_the_names(env):
    e, tmp = env
    run(e, "init", "--relay", RELAY)
    moved = dict(STATUS, MagicDNSSuffix="other.ts.net")
    moved["Self"] = dict(STATUS["Self"], DNSName=f"{MAC}.other.ts.net.")
    Path(e["HELIOS_CA_TAILSCALE_JSON"]).write_text(json.dumps(moved))
    r = run(e, "issue", "mac", check=False)
    assert r.returncode != 0
    assert not (tmp / "certs" / "staged" / f"{MAC}.local.pem").exists()


def test_days_left_reads_the_live_leaves(env):
    e, tmp = env
    run(e, "init", "--relay", RELAY)
    run(e, "issue", "mac")
    certs = tmp / "certs"
    shutil.copy(certs / "staged" / f"{MAC}.local.pem", certs / f"{MAC}.local.pem")
    r = run(e, "days-left")
    assert re.search(rf"{re.escape(MAC)}\.local\.pem\s+(819|820)\b", r.stdout), r.stdout
    assert re.search(r"root\.pem\s+365[0-3]\b", r.stdout), r.stdout


def test_no_doc_tells_anyone_to_install_the_mkcert_root():
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for rel in tracked:
        if rel.endswith((".md", ".toml", ".yml", ".swift", ".py")) and rel != "CHANGELOG.md":
            p = REPO / rel
            if p.is_file() and re.search(r"mkcert\s+-install|mkcert -CAROOT", p.read_text(errors="ignore")):
                hits.append(rel)
    hits = [h for h in hits if h != "server/tests/test_helios_ca.py"]
    assert hits == [], f"still tells people to trust the mkcert root: {hits}"
