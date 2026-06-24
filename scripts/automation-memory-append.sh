#!/usr/bin/env bash
#
# automation-memory-append.sh -- record one Claude Code automation run.
#
# Usage:
#   automation-memory-append.sh <automation_id> <one-line summary>
#   automation-memory-append.sh <automation_id> --name "Pretty Name" --rrule "FREQ=DAILY" <line>
#   automation-memory-append.sh <automation_id> --kind cron --cwd /path <line>
#
# What it does:
#   1. Ensures ~/.claude/automations/<id>/ exists (or CLAUDE_AUTOMATIONS_DIR/<id>).
#   2. Creates a minimal automation.toml if missing (id, name, status=ACTIVE,
#      kind, cwds), populated from --name/--rrule/--kind/--cwd flags or sane
#      defaults.
#   3. Appends a dated bullet to memory.md. If a section for TODAY already
#      exists at the bottom of the file, the bullet is appended to it;
#      otherwise a new dated section is opened.
#
# After this runs, the next `bourdon claude-code-automations export` picks up
# the entry and federates it. Designed to be called from CronCreate-fired
# prompts, /loop wake-ups, GitHub Action `claude-code-action` post-steps, and
# the /schedule skill's local mirror.
#
# Exits 0 silently on success. Logs warnings to stderr on bad input but never
# fails the calling automation -- this is observability, not gating.

set -u

usage() {
  sed -n '3,28p' "$0" | sed 's/^# \{0,1\}//'
  exit 64
}

[ "$#" -ge 2 ] || usage

automation_id="$1"; shift

name=""
rrule=""
kind="claude-code-automation"
cwd=""
summary_parts=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --name) name="$2"; shift 2 ;;
    --rrule) rrule="$2"; shift 2 ;;
    --kind) kind="$2"; shift 2 ;;
    --cwd) cwd="$2"; shift 2 ;;
    --) shift; summary_parts+=("$@"); break ;;
    *) summary_parts+=("$1"); shift ;;
  esac
done

if [ "${#summary_parts[@]}" -eq 0 ]; then
  echo "automation-memory-append.sh: missing summary text" >&2
  exit 64
fi

summary="${summary_parts[*]}"
[ -n "$name" ] || name="$automation_id"
[ -n "$cwd" ] || cwd="$(pwd)"

# Sanitize id: only allow [A-Za-z0-9._-]
case "$automation_id" in
  *[!A-Za-z0-9._-]*)
    echo "automation-memory-append.sh: refusing id with disallowed chars: $automation_id" >&2
    exit 64
    ;;
esac

base="${CLAUDE_AUTOMATIONS_DIR:-$HOME/.claude/automations}"
dir="$base/$automation_id"
toml="$dir/automation.toml"
memory="$dir/memory.md"

mkdir -p "$dir" || { echo "automation-memory-append.sh: cannot mkdir $dir" >&2; exit 1; }

if [ ! -f "$toml" ]; then
  {
    printf 'version = 1\n'
    printf 'id = "%s"\n' "$automation_id"
    printf 'name = "%s"\n' "${name//\"/\\\"}"
    printf 'status = "ACTIVE"\n'
    printf 'kind = "%s"\n' "$kind"
    printf 'rrule = "%s"\n' "$rrule"
    printf 'cwds = ["%s"]\n' "${cwd//\"/\\\"}"
  } > "$toml"
fi

today="$(date -u +%Y-%m-%d)"
last_header=""
if [ -f "$memory" ]; then
  # Find the last line that starts with a YYYY-MM-DD pattern
  last_header="$(grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}' "$memory" | tail -1 || true)"
fi

memory_nonempty_before=0
[ -s "$memory" ] && memory_nonempty_before=1

{
  if [ -z "$last_header" ] || ! printf '%s' "$last_header" | grep -q "^$today"; then
    # New dated section -- prepend blank line only if the file already had content.
    [ "$memory_nonempty_before" -eq 1 ] && printf '\n'
    printf '%s\n' "$today"
  fi
  printf -- '- %s\n' "$summary"
} >> "$memory"

exit 0
