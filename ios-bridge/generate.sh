#!/bin/bash
# Generate HeliosBridge.xcodeproj with the Apple Team ID. The value comes from
# HELIOS_DEVELOPMENT_TEAM in the environment if set, otherwise from the repo
# root's .env (gitignored; copy .env.example), or from HELIOS_ENV_FILE. The
# file is read as KEY=value text, never run as shell code. xcodegen accepts a
# missing, empty or placeholder value without complaint (an unset one even
# lands as the literal text ${HELIOS_DEVELOPMENT_TEAM}), and the build then
# fails late at signing, so check it here first. The value is never printed.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
if [ -n "${HELIOS_ENV_FILE:-}" ] && [ ! -f "$HELIOS_ENV_FILE" ]; then
  echo "HELIOS_ENV_FILE does not name a file: $HELIOS_ENV_FILE" >&2
  exit 2
fi
ENV_FILE=${HELIOS_ENV_FILE:-$HERE/../.env}   # a relative HELIOS_ENV_FILE is the caller's
TEAM=${HELIOS_DEVELOPMENT_TEAM:-}
SOURCE="the environment"
if [ -z "$TEAM" ] && [ -f "$ENV_FILE" ]; then
  SOURCE=$ENV_FILE
  # The last non-empty assignment wins; a byte-order mark, CR line ends, an
  # export prefix, quotes and a trailing " # comment" are tolerated.
  TEAM=$(LC_ALL=C sed -e "1s/^$(printf '\357\273\277')//" -e 's/\r$//' "$ENV_FILE" \
    | LC_ALL=C sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?HELIOS_DEVELOPMENT_TEAM[[:space:]]*=//p' \
    | LC_ALL=C sed -E -e 's/[[:space:]]+#.*$//' -e 's/^[[:space:]]+//' -e 's/[[:space:]]+$//' \
                     -e "s/^[\"']//" -e "s/[\"']\$//" \
    | awk 'NF' | tail -n 1) || TEAM=""
fi
if [ -z "$TEAM" ] || [ "$TEAM" = "YOURTEAMID" ]; then
  echo "HELIOS_DEVELOPMENT_TEAM is not set in $SOURCE: copy .env.example to .env in the repo root and put your Apple Team ID in it" >&2
  exit 2
fi
if ! [[ "$TEAM" =~ ^[A-Z0-9]{10}$ ]]; then
  echo "HELIOS_DEVELOPMENT_TEAM from $SOURCE is not a Team ID (10 capital letters or digits)" >&2
  exit 2
fi
export HELIOS_DEVELOPMENT_TEAM=$TEAM
cd "$HERE"
exec xcodegen generate "$@"
