#!/usr/bin/env bash
#
# cursor-automation-memory-append.sh -- record one Cursor Cloud Agent automation run.
#
# Usage:
#   cursor-automation-memory-append.sh <automation_id> <one-line summary>
#   cursor-automation-memory-append.sh <automation_id> --name "Name" --kind cloud-agent <line>
#
# Ensures ~/.cursor/automations/<id>/ exists, creates automation.toml on first
# run, appends a dated bullet to memory.md. Next `bourdon cursor-automations
# export` picks it up and federates it.
#
# Exits 0 silently on success. Never fails the calling automation.

set -u
[ "$#" -ge 2 ] || { echo "Usage: $0 <automation_id> <summary...>" >&2; exit 64; }

automation_id="$1"; shift
name="" rrule="" kind="cursor-cloud-agent" cwd=""
summary_parts=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --name) name="$2"; shift 2 ;; --rrule) rrule="$2"; shift 2 ;;
    --kind) kind="$2"; shift 2 ;; --cwd) cwd="$2"; shift 2 ;;
    --) shift; summary_parts+=("$@"); break ;; *) summary_parts+=("$1"); shift ;;
  esac
done
[ "${#summary_parts[@]}" -eq 0 ] && { echo "$0: missing summary" >&2; exit 64; }

summary="${summary_parts[*]}"
[ -n "$name" ] || name="$automation_id"
[ -n "$cwd" ] || cwd="$(pwd)"

case "$automation_id" in *[!A-Za-z0-9._-]*)
  echo "$0: bad id chars: $automation_id" >&2; exit 64 ;; esac

base="${CURSOR_AUTOMATIONS_DIR:-$HOME/.cursor/automations}"
dir="$base/$automation_id"
toml="$dir/automation.toml" memory="$dir/memory.md"
mkdir -p "$dir" || exit 1

if [ ! -f "$toml" ]; then
  printf 'version = 1\nid = "%s"\nname = "%s"\nstatus = "ACTIVE"\nkind = "%s"\nrrule = "%s"\ncwds = ["%s"]\n' \
    "$automation_id" "${name//\"/\\\"}" "$kind" "$rrule" "${cwd//\"/\\\"}" > "$toml"
fi

today="$(date -u +%Y-%m-%d)"
last_header=""; [ -f "$memory" ] && last_header="$(grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}' "$memory" | tail -1 || true)"
nonempty=0; [ -s "$memory" ] && nonempty=1
{
  if [ -z "$last_header" ] || ! printf '%s' "$last_header" | grep -q "^$today"; then
    [ "$nonempty" -eq 1 ] && printf '\n'; printf '%s\n' "$today"
  fi
  printf -- '- %s\n' "$summary"
} >> "$memory"
exit 0
