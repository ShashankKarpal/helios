#!/bin/bash
# helios_ca.sh: the private Helios root CA and the two TLS server certificates it signs.
#
# Why: a general-purpose local root (mkcert) can sign a certificate for ANY website, so a copy of
# its key is a master key for every TLS connection on each device that trusts it. This root carries
# critical name constraints: it can only vouch for the Helios machines by NAME (this Mac, the relay
# Mac, and the tailnet's MagicDNS names). Its only IP subtree is 127.0.0.1/32, so it can sign for no
# other address either: every client reaches Helios by name, and the leaves carry no IP address.
# A certificate it signs for any other name or address fails on the phone and on the Mac, even if
# the key leaks.
#
# Usage (run on the serving Mac; every name is read live from `tailscale status --json`):
#   helios_ca.sh init [--relay <host>] [--label <text>]  new root (refuses if one exists);
#                                            the label names it (default: today's date)
#   helios_ca.sh issue mac|relay             new leaf and key into <certs>/staged/
#   helios_ca.sh renew mac|relay             the same as issue (a fresh key every time)
#   helios_ca.sh days-left                   days left on the root and the live leaves in <certs>
#   helios_ca.sh canary [dir]                prove the constraints: sign out-of-scope name and IP leaves
#
# Folders: CA folder  $HELIOS_CA_DIR    (default ~/Library/Application Support/helios-ca, mode 700,
#                                        excluded from Time Machine; root-key.pem mode 400)
#          certs      $HELIOS_CERTS_DIR (default ~/Helios/certs; issue writes only to staged/)
# The script never prints key material and never writes the live certificate files: putting a
# staged leaf into place, and restarting the server that reads it, is a separate, deliberate step
# (SETUP.md, "Certificates").
#
# Exit codes: 0 ok; 1 days-left found a certificate under 45 days, or canary was ACCEPTED (the root
# does not limit names); 2 usage, precondition or OpenSSL failure, or a canary that proves nothing.
# Test hooks: HELIOS_CA_TAILSCALE_JSON (a saved `tailscale status --json`), HELIOS_CA_NO_TMUTIL=1,
# HELIOS_OPENSSL (OpenSSL 3 or newer; LibreSSL cannot write these extensions).
set -u
set -o pipefail   # a failed tailscale read must fail the whole pipeline, not hide behind sed
umask 077
PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:$PATH

CA_DIR="${HELIOS_CA_DIR:-$HOME/Library/Application Support/helios-ca}"
CERTS_DIR="${HELIOS_CERTS_DIR:-$HOME/Helios/certs}"
STAGED="$CERTS_DIR/staged"
ROOT="$CA_DIR/root.pem"
ROOT_KEY="$CA_DIR/root-key.pem"
ROOT_DAYS=3652
LEAF_DAYS=820        # Apple: TLS server certificates must be valid for 825 days or fewer.
WARN_DAYS=45
IP_SUBTREE="127.0.0.1/255.255.255.255"   # loopback only: without an IP subtree, IP SANs are unconstrained

die() { echo "helios_ca: $*" >&2; exit 2; }

# Key material is made inside the folder it ends up in (never in the system temp folder), in a
# private temp subfolder that this one exit trap removes, also after a failure or Ctrl-C.
TMP_LIST=""
CANARY_OUT=""
cleanup() {
  printf '%s\n' "$TMP_LIST" | while IFS= read -r d; do [ -n "$d" ] && rm -rf "$d"; done
  [ -z "$CANARY_OUT" ] || rm -f "$CANARY_OUT/control-key.pem" "$CANARY_OUT/canary-key.pem" "$CANARY_OUT/ipcanary-key.pem"
  return 0
}
trap cleanup EXIT

find_openssl() {
  local c v
  for c in "${HELIOS_OPENSSL:-}" /opt/homebrew/bin/openssl /usr/local/bin/openssl "$(command -v openssl 2>/dev/null)"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    v=$("$c" version 2>/dev/null)
    case "$v" in
      "OpenSSL "[3-9].*|"OpenSSL "[1-9][0-9].*) echo "$c"; return 0 ;;
    esac
  done
  return 1
}
OPENSSL=$(find_openssl) || die "OpenSSL 3 or newer is required (brew install openssl@3, or set HELIOS_OPENSSL)"
PY=$(command -v python3) || die "python3 is required"

tailnet_json() {
  if [ -n "${HELIOS_CA_TAILSCALE_JSON:-}" ]; then
    cat "$HELIOS_CA_TAILSCALE_JSON"
    return
  fi
  local ts
  ts=$(command -v tailscale 2>/dev/null) || ts=/Applications/Tailscale.app/Contents/MacOS/Tailscale
  [ -x "$ts" ] || die "tailscale not found"
  "$ts" status --json
}

# Prints three lines for a node: host name, MagicDNS name, tailnet IPv4. Or one line: the suffix.
# Every value is checked before it reaches an OpenSSL config file.
PY_NODE='
import ipaddress, json, re, sys
d = json.load(sys.stdin)
suffix = (d.get("MagicDNSSuffix") or "").strip(".").lower()
if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", suffix):
    sys.exit("no MagicDNS suffix (is MagicDNS on?)")
if sys.argv[1] == "suffix":
    print(suffix); sys.exit(0)
if sys.argv[1] == "self":
    node = d.get("Self") or {}
else:
    found = [p for p in (d.get("Peer") or {}).values() if (p.get("HostName") or "").lower() == sys.argv[2].lower()]
    if len(found) != 1:
        sys.exit("no single tailnet peer with that host name")
    node = found[0]
host = (node.get("HostName") or "").lower()
dns = (node.get("DNSName") or "").rstrip(".").lower()
v4 = [a for a in node.get("TailscaleIPs") or [] if "." in a]
if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", host):
    sys.exit("bad host name")
# A bare host name in the root also permits every name under it. Every public top-level domain is
# letters only (or an xn-- name), so a name with a digit or a hyphen can never be one.
if re.fullmatch(r"[a-z]+", host) or host.startswith("xn--"):
    sys.exit("host name " + host + " could be a public top-level domain; rename the machine (add a digit or a hyphen)")
if not dns.endswith("." + suffix) or not re.fullmatch(r"[a-z0-9.-]+", dns):
    sys.exit("MagicDNS name is not under the tailnet suffix")
if not v4 or ipaddress.ip_address(v4[0]) not in ipaddress.ip_network("100.64.0.0/10"):
    sys.exit("no tailnet IPv4 address")
print(host); print(dns); print(v4[0])
'
node() { tailnet_json | "$PY" -c "$PY_NODE" "$@"; }

# The root's permitted subtrees, one per line: "DNS <name>" or "IP <network>".
PY_PERMITTED='
import re, sys
t = sys.stdin.read()
m = re.search(r"Name Constraints: critical\s*\n\s*Permitted:\s*\n(.*?)(?:\n\s*Excluded:|\n\s*X509v3 |\n\s*Signature Algorithm|\Z)", t, re.S)
if not m:
    sys.exit("the root has no critical name constraints")
for line in m.group(1).splitlines():
    line = line.strip()
    if line.startswith("DNS:"):
        print("DNS", line[4:])
    elif line.startswith("IP:"):
        print("IP", line[3:])
'
permitted() { "$OPENSSL" x509 -in "$ROOT" -noout -text | "$PY" -c "$PY_PERMITTED"; }

# Exit 0 when every SAN (args "DNS:x" or "IP:y") falls inside the root's permitted subtrees.
PY_COVERS='
import ipaddress, sys
dns, nets = [], []
for line in sys.stdin.read().splitlines():
    kind, value = line.split(" ", 1)
    if kind == "DNS":
        dns.append(value.lower())
    else:
        addr, mask = value.split("/")
        nets.append(ipaddress.ip_network(addr + "/" + mask))
for san in sys.argv[1:]:
    kind, value = san.split(":", 1)
    if kind == "DNS":
        ok = any(value.lower() == c or value.lower().endswith("." + c) for c in dns)
    else:
        ok = any(ipaddress.ip_address(value) in n for n in nets)
    if not ok:
        sys.exit("a name is outside the constraints of the root")
'

days_left_of() {   # prints whole days until notAfter
  "$OPENSSL" x509 -in "$1" -noout -enddate | "$PY" -c '
import sys, datetime as d
v = sys.stdin.read().strip().split("=", 1)[1]
end = d.datetime.strptime(v, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=d.timezone.utc)
print(int((end - d.datetime.now(d.timezone.utc)).total_seconds() // 86400))'
}

need_root() {
  [ -f "$ROOT" ] && [ -f "$ROOT_KEY" ] || die "no root in the CA folder; run init first"
}

# sign_leaf <outdir> <base name> <SAN list "DNS:a,DNS:b,IP:c"> : key and cert into outdir.
sign_leaf() {
  local out=$1 base=$2 sans=$3 first tmp
  first=${sans%%,*}; first=${first#*:}
  mkdir -p "$out" && chmod 700 "$out" || die "cannot create $out"
  tmp=$(mktemp -d "$out/.helios-ca-new.XXXXXX") || die "mktemp failed"
  TMP_LIST="$TMP_LIST
$tmp"
  cat > "$tmp/leaf.cnf" <<EOF
[leaf]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
subjectAltName = $sans
EOF
  "$OPENSSL" genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$tmp/key.pem" 2>/dev/null \
    && "$OPENSSL" req -new -key "$tmp/key.pem" -subj "/O=Helios/CN=$first" -out "$tmp/leaf.csr" 2>/dev/null \
    && "$OPENSSL" x509 -req -in "$tmp/leaf.csr" -CA "$ROOT" -CAkey "$ROOT_KEY" -days "$LEAF_DAYS" -sha256 \
         -extfile "$tmp/leaf.cnf" -extensions leaf -out "$tmp/cert.pem" 2>/dev/null \
    || { rm -rf "$tmp"; die "OpenSSL could not sign the leaf"; }
  chmod 600 "$tmp/key.pem"; chmod 644 "$tmp/cert.pem"
  mv -f "$tmp/key.pem" "$out/$base-key.pem" && mv -f "$tmp/cert.pem" "$out/$base.pem" \
    || { rm -f "$out/$base-key.pem" "$out/$base.pem"; rm -rf "$tmp"; die "cannot write into $out; the pair was removed"; }
  rm -rf "$tmp"
  LEAF_FIRST=$first
}

cmd_init() {
  local relay_arg="" label me relay suffix mhost rhost="" relay_lines="" tmp fp
  label=$(date -u +%Y-%m-%d)
  while [ $# -gt 0 ]; do
    case "$1" in
      --relay) [ -n "${2:-}" ] || die "usage: init [--relay <host>] [--label <text>]"; relay_arg=$2; shift 2 ;;
      --label) [ -n "${2:-}" ] || die "usage: init [--relay <host>] [--label <text>]"; label=$2; shift 2 ;;
      *) die "usage: init [--relay <host>] [--label <text>]" ;;
    esac
  done
  case "$label" in *[!A-Za-z0-9.-]*) die "the label may hold only letters, digits, dots and hyphens" ;; esac
  [ -e "$ROOT" ] || [ -e "$ROOT_KEY" ] && die "a root already exists in the CA folder; it is never replaced in place"
  me=$(node self) || die "cannot read this Mac from tailscale"
  suffix=$(node suffix) || die "cannot read the tailnet suffix"
  mhost=$(echo "$me" | sed -n 1p)
  if [ -n "$relay_arg" ]; then
    relay=$(node peer "$relay_arg") || die "cannot read the relay peer from tailscale"
    rhost=$(echo "$relay" | sed -n 1p)
    [ "$rhost" != "$mhost" ] || die "the relay must be another machine"
    relay_lines="permitted;DNS.3 = ${rhost}.local
permitted;DNS.4 = $rhost"
  fi

  mkdir -p "$CA_DIR" && chmod 700 "$CA_DIR" || die "cannot create the CA folder"
  if [ -z "${HELIOS_CA_NO_TMUTIL:-}" ] && command -v tmutil >/dev/null 2>&1; then
    tmutil addexclusion "$CA_DIR" >/dev/null 2>&1
    case "$(tmutil isexcluded "$CA_DIR" 2>/dev/null)" in
      *"[Excluded]"*) ;;
      *) die "Time Machine still includes the CA folder; nothing written" ;;
    esac
  fi

  tmp=$(mktemp -d "$CA_DIR/.helios-ca-new.XXXXXX") || die "mktemp failed"
  TMP_LIST="$TMP_LIST
$tmp"
  cat > "$tmp/root.cnf" <<EOF
[req]
distinguished_name = dn
prompt = no
[dn]
O = Helios
CN = Helios Private Root $label
[root]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
nameConstraints = critical, @permitted
[permitted]
permitted;DNS.0 = ${mhost}.local
permitted;DNS.1 = $mhost
permitted;DNS.2 = $suffix
$relay_lines
permitted;IP.0 = $IP_SUBTREE
EOF
  "$OPENSSL" genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$tmp/root-key.pem" 2>/dev/null \
    && "$OPENSSL" req -x509 -new -key "$tmp/root-key.pem" -config "$tmp/root.cnf" -extensions root \
         -days "$ROOT_DAYS" -sha256 -out "$tmp/root.pem" 2>/dev/null \
    || { rm -rf "$tmp"; die "OpenSSL could not make the root"; }
  chmod 400 "$tmp/root-key.pem"; chmod 644 "$tmp/root.pem"
  mv "$tmp/root-key.pem" "$ROOT_KEY" && mv "$tmp/root.pem" "$ROOT" \
    || { rm -f "$ROOT_KEY" "$ROOT"; rm -rf "$tmp"; die "cannot write the root; nothing kept"; }
  rm -rf "$tmp"
  permitted >/dev/null || die "the new root has no critical name constraints"
  fp=$("$OPENSSL" x509 -in "$ROOT" -noout -fingerprint -sha256 | cut -d= -f2)
  echo "root made: $(permitted | grep -c '^DNS') DNS and $(permitted | grep -c '^IP') IP subtrees, critical; $ROOT_DAYS days"
  echo "root sha256 $fp"
  echo "next: issue mac, issue relay; trust root.pem (the public certificate only) on the phone"
}

cmd_issue() {
  local which=${1:-} line host dns base sans rhost
  need_root
  case "$which" in
    mac)
      line=$(node self) || die "cannot read this Mac from tailscale"
      host=$(echo "$line" | sed -n 1p); dns=$(echo "$line" | sed -n 2p)
      base="${host}.local"; sans="DNS:${host}.local,DNS:$dns" ;;
    relay)
      # The relay is the one bare host name in the root that is not this Mac.
      line=$(node self) || die "cannot read this Mac from tailscale"
      host=$(echo "$line" | sed -n 1p)
      rhost=$(permitted | awk -v me="$host" '$1 == "DNS" && $2 !~ /\./ && $2 != me {print $2}')
      [ "$(echo "$rhost" | grep -c .)" = 1 ] || die "this root names no relay host (init --relay makes one that does)"
      line=$(node peer "$rhost") || die "cannot read the relay peer from tailscale"
      dns=$(echo "$line" | sed -n 2p)
      base="$rhost-relay"; sans="DNS:$dns,DNS:$rhost" ;;
    *) die "usage: issue mac|relay" ;;
  esac
  # A leaf the root cannot cover would fail on every client: refuse to make it.
  permitted | "$PY" -c "$PY_COVERS" $(echo "$sans" | tr ',' ' ') \
    || die "the root does not cover this machine's current names (tailnet renamed?); make a new root"
  sign_leaf "$STAGED" "$base" "$sans"
  "$OPENSSL" verify -x509_strict -purpose sslserver -CAfile "$ROOT" -verify_hostname "$LEAF_FIRST" \
      "$STAGED/$base.pem" >/dev/null 2>&1 \
    || { rm -f "$STAGED/$base.pem" "$STAGED/$base-key.pem"; die "the new leaf does not verify against the root; removed"; }
  echo "$which leaf: $(echo "$sans" | tr ',' '\n' | grep -c .) names, $LEAF_DAYS days, verified against the root"
  echo "staged: $STAGED/$base.pem and $base-key.pem (mode 600); the live files are unchanged"
}

cmd_days_left() {
  need_root
  local f d status=0
  for f in "$ROOT" "$CERTS_DIR"/*.pem; do
    [ -f "$f" ] || continue
    case "$f" in *-key.pem) continue ;; esac
    if ! d=$(days_left_of "$f" 2>/dev/null) || [ -z "$d" ]; then
      printf '%-28s %s\n' "$(basename "$f")" "UNREADABLE (not one certificate)"; status=1; continue
    fi
    if [ "$d" -lt "$WARN_DAYS" ]; then
      printf '%-28s %s  RENEW (under %s days)\n' "$(basename "$f")" "$d" "$WARN_DAYS"; status=1
    else
      printf '%-28s %s\n' "$(basename "$f")" "$d"
    fi
  done
  return $status
}

# verdicts <leaf> <name>: one word per check: PASS, CONSTRAINT (refused because of the root's name
# constraints, and for nothing else) or OTHER (refused for another reason).
verdicts() {
  local leaf=$1 name=$2 out
  if out=$("$OPENSSL" verify -x509_strict -purpose sslserver -CAfile "$ROOT" -verify_hostname "$name" "$leaf" 2>&1); then
    echo PASS
  else
    case "$out" in *"permitted subtree violation"*) echo CONSTRAINT ;; *) echo OTHER ;; esac
  fi
  if [ "$(uname -s)" = Darwin ] && [ -x /usr/bin/security ]; then
    if out=$(/usr/bin/security verify-cert -v -N -L -p ssl -n "$name" -c "$leaf" -r "$ROOT" 2>&1); then
      echo PASS
    else
      case "$out" in *"[NameConstraints]"*) echo CONSTRAINT ;; *) echo OTHER ;; esac
    fi
  fi
}

count() { echo "$1" | grep -c "^$2\$"; }

cmd_canary() {
  need_root
  local out=${1:-} line host ip ctl can ipc n parent real_ca real_certs
  if [ -z "$out" ]; then
    out=$(mktemp -d "${TMPDIR:-/tmp}/helios-canary.XXXXXX") || die "mktemp failed"
  else
    [ ! -e "$out" ] || die "the canary folder must not exist yet: $out"
    parent=$(cd "$(dirname "$out")" 2>/dev/null && pwd -P) || die "no such parent folder for $out"
    out="$parent/$(basename "$out")"
    real_ca=$(cd "$CA_DIR" 2>/dev/null && pwd -P) || real_ca=$CA_DIR
    real_certs=$(cd "$CERTS_DIR" 2>/dev/null && pwd -P) || real_certs=$CERTS_DIR
    case "$out/" in "$real_ca"/*|"$real_certs"/*) die "the canary never goes into the CA or certs folder" ;; esac
    mkdir -m 700 "$out" || die "cannot create $out"
  fi
  CANARY_OUT=$out   # the exit trap removes every canary key, whatever happens next
  line=$(node self) || die "cannot read this Mac from tailscale"
  host=$(echo "$line" | sed -n 1p); ip=$(echo "$line" | sed -n 3p)
  [ -n "$host" ] && [ -n "$ip" ] || die "cannot read this Mac from tailscale"
  # A control leaf inside the constraints must pass, or a refusal of a canary proves nothing.
  # The name canary adds www.example.com; the IP canary adds this Mac's own tailnet address,
  # which the root must refuse too (its only IP subtree is 127.0.0.1/32).
  sign_leaf "$out" control "DNS:${host}.local"
  sign_leaf "$out" canary "DNS:${host}.local,DNS:www.example.com"
  sign_leaf "$out" ipcanary "DNS:${host}.local,IP:$ip"
  ctl=$(verdicts "$out/control.pem" "${host}.local")
  can=$(verdicts "$out/canary.pem" "${host}.local")
  ipc=$(verdicts "$out/ipcanary.pem" "${host}.local")
  rm -f "$out/control-key.pem" "$out/canary-key.pem" "$out/ipcanary-key.pem"   # no usable canary key is left
  n=$(echo "$ctl" | grep -c .)
  if [ "$(count "$ctl" PASS)" != "$n" ]; then
    echo "canary INCONCLUSIVE: a leaf inside the constraints failed $((n - $(count "$ctl" PASS))) of $n checks"
    return 2
  fi
  if [ "$(count "$can" CONSTRAINT)" = "$n" ] && [ "$(count "$ipc" CONSTRAINT)" = "$n" ]; then
    echo "canary REJECTED by $n of $n checks for its name, IP canary REJECTED by $n of $n for its address: the root's name constraints hold (control leaf passed)"
    echo "canary certificates (no keys): $out"
    return 0
  fi
  if [ "$(count "$can" PASS)" != 0 ] || [ "$(count "$ipc" PASS)" != 0 ]; then
    echo "canary ACCEPTED: name canary by $(count "$can" PASS), IP canary by $(count "$ipc" PASS) of $n checks: the root does NOT limit names"
    return 1
  fi
  echo "canary INCONCLUSIVE: refused, but not for the name constraints, by $(count "$can" OTHER) and $(count "$ipc" OTHER) of $n checks"
  return 2
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  issue|renew) shift; cmd_issue "$@" ;;
  days-left) cmd_days_left ;;
  canary) shift; cmd_canary "$@" ;;
  *) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
