#!/usr/bin/env bash
# Phase 2 §1 probe: capture statusline JSON payloads.
# Reads the JSON Claude Code passes on stdin, writes it to disk for
# inspection, and emits a one-line statusline on stdout.
#
# Outputs:
#   /tmp/uas_statusline_probe.json          latest payload (whole JSON)
#   /tmp/uas_statusline_probes/<ts>-<pid>.json   per-invocation history
#   /tmp/uas_statusline_probe.log           one line per invocation
#
# This script is owned by Phase 2 of the UAS roadmap and is expected
# to be removed (along with the statusLine entry it is wired up to)
# at Phase 2 close.

set -u

probe_dir="/tmp/uas_statusline_probes"
mkdir -p "$probe_dir"

ts="$(date -u +%Y%m%dT%H%M%S.%3NZ)"
payload="$(cat)"

printf '%s' "$payload" > "$probe_dir/${ts}-pid${PPID}.json"
printf '%s' "$payload" > /tmp/uas_statusline_probe.json
printf '%s pid=%s ppid=%s\n' "$ts" "$$" "$PPID" >> /tmp/uas_statusline_probe.log

printf 'uas-probe'
