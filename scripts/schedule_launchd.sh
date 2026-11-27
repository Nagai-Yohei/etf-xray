#!/usr/bin/env bash
# Manage a local macOS launchd periodic refresh agent for etf-xray.
# Generates the LaunchAgent plist dynamically using the current checkout path
# so no local paths or credentials are ever stored in the repository.

set -euo pipefail

LABEL="dev.etf-xray.refresh"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_SH="$ROOT_DIR/run.sh"
LOG_DIR="$ROOT_DIR/data/logs"
LAUNCHD_LOG="$LOG_DIR/launchd.log"
AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${AGENTS_DIR}/${LABEL}.plist"

usage() {
  cat <<'EOF'
Usage: ./run.sh schedule <subcommand> [options]

Subcommands:
  install    Generate and load the macOS LaunchAgent (default: Sunday 07:00, check-only)
  status     Show LaunchAgent registration status, schedule, and recent refresh log
  run-now    Trigger the installed LaunchAgent immediately
  uninstall  Unload and remove the macOS LaunchAgent plist

Install options:
  --weekday <0-6>          Day of week (0=Sun, 1=Mon, ..., 6=Sat; default: 0)
  --daily                  Run every day instead of weekly
  --hour <0-23>            Local hour to run (default: 7)
  --minute <0-59>          Local minute to run (default: 0)
  --xbrl-max-age-days <N>  Refresh active XBRL older than N days (default: 14)
  --deploy                 Auto-deploy to Cloudflare Pages when changes pass src.check
  --dry-run                Print generated plist XML without installing
EOF
}

require_macos() {
  if [ "$(uname -s)" != "Darwin" ]; then
    echo "Error: launchd scheduling is only supported on macOS." >&2
    exit 1
  fi
}

is_uint() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

weekday_name() {
  case "$1" in
    0|7) echo "Sunday" ;;
    1) echo "Monday" ;;
    2) echo "Tuesday" ;;
    3) echo "Wednesday" ;;
    4) echo "Thursday" ;;
    5) echo "Friday" ;;
    6) echo "Saturday" ;;
    *) echo "Day $1" ;;
  esac
}

render_plist() {
  local mode="$1"
  local weekday="$2"
  local hour="$3"
  local minute="$4"
  local xbrl_days="$5"
  local do_deploy="$6"

  local deploy_arg=""
  if [ "$do_deploy" -eq 1 ]; then
    deploy_arg="        <string>--deploy</string>"
  fi

  local weekday_xml=""
  if [ "$mode" = "weekly" ]; then
    weekday_xml="        <key>Weekday</key>
        <integer>${weekday}</integer>"
  fi

  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>WorkingDirectory</key>
    <string>${ROOT_DIR}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${RUN_SH}</string>
        <string>refresh</string>
        <string>--xbrl-max-age-days</string>
        <string>${xbrl_days}</string>
EOF
  if [ -n "$deploy_arg" ]; then
    printf '%s\n' "$deploy_arg"
  fi
  cat <<EOF
    </array>
    <key>StartCalendarInterval</key>
    <dict>
EOF
  if [ -n "$weekday_xml" ]; then
    printf '%s\n' "$weekday_xml"
  fi
  cat <<EOF
        <key>Hour</key>
        <integer>${hour}</integer>
        <key>Minute</key>
        <integer>${minute}</integer>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${LAUNCHD_LOG}</string>
    <key>StandardErrorPath</key>
    <string>${LAUNCHD_LOG}</string>
</dict>
</plist>
EOF
}

cmd_install() {
  local mode="weekly"
  local weekday="0"
  local hour="7"
  local minute="0"
  local xbrl_days="14"
  local do_deploy=0
  local dry_run=0

  while [ $# -gt 0 ]; do
    case "$1" in
      --weekday)
        shift
        [ $# -gt 0 ] || { echo "Error: --weekday requires a value (0-6)." >&2; exit 1; }
        weekday="$1"
        mode="weekly"
        ;;
      --daily)
        mode="daily"
        ;;
      --hour)
        shift
        [ $# -gt 0 ] || { echo "Error: --hour requires a value (0-23)." >&2; exit 1; }
        hour="$1"
        ;;
      --minute)
        shift
        [ $# -gt 0 ] || { echo "Error: --minute requires a value (0-59)." >&2; exit 1; }
        minute="$1"
        ;;
      --xbrl-max-age-days)
        shift
        [ $# -gt 0 ] || { echo "Error: --xbrl-max-age-days requires a number." >&2; exit 1; }
        xbrl_days="$1"
        ;;
      --deploy)
        do_deploy=1
        ;;
      --dry-run)
        dry_run=1
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Error: unknown option '$1'." >&2
        usage >&2
        exit 1
        ;;
    esac
    shift
  done

  if [ "$mode" = "weekly" ]; then
    if ! is_uint "$weekday" || [ "$weekday" -gt 7 ]; then
      echo "Error: --weekday must be an integer between 0 (Sun) and 6 (Sat)." >&2
      exit 1
    fi
  fi
  if ! is_uint "$hour" || [ "$hour" -gt 23 ]; then
    echo "Error: --hour must be an integer between 0 and 23." >&2
    exit 1
  fi
  if ! is_uint "$minute" || [ "$minute" -gt 59 ]; then
    echo "Error: --minute must be an integer between 0 and 59." >&2
    exit 1
  fi
  if ! is_uint "$xbrl_days"; then
    echo "Error: --xbrl-max-age-days must be a non-negative integer." >&2
    exit 1
  fi

  if [ "$dry_run" -eq 1 ]; then
    render_plist "$mode" "$weekday" "$hour" "$minute" "$xbrl_days" "$do_deploy"
    return 0
  fi

  require_macos
  mkdir -p "$AGENTS_DIR" "$LOG_DIR"

  local domain="gui/$(id -u)"
  launchctl bootout "$domain" "$PLIST_PATH" 2>/dev/null || true

  render_plist "$mode" "$weekday" "$hour" "$minute" "$xbrl_days" "$do_deploy" > "$PLIST_PATH"
  plutil -lint "$PLIST_PATH" >/dev/null
  launchctl bootstrap "$domain" "$PLIST_PATH"

  local sched_desc
  if [ "$mode" = "daily" ]; then
    sched_desc="$(printf 'Daily at %02d:%02d local time' "$hour" "$minute")"
  else
    sched_desc="$(printf 'Every %s at %02d:%02d local time' "$(weekday_name "$weekday")" "$hour" "$minute")"
  fi

  echo "LaunchAgent installed and loaded:"
  echo "  Label:       $LABEL"
  echo "  Plist:       $PLIST_PATH"
  echo "  Schedule:    $sched_desc"
  echo "  XBRL max age: ${xbrl_days} day(s)"
  if [ "$do_deploy" -eq 1 ]; then
    echo "  Auto-deploy: ENABLED (deploys only when changes occur and src.check passes)"
  else
    echo "  Auto-deploy: DISABLED (check-only; pass --deploy to enable)"
  fi
  echo "  Logs:        $LOG_DIR"
}

cmd_status() {
  require_macos
  local domain="gui/$(id -u)"

  if [ ! -f "$PLIST_PATH" ]; then
    echo "LaunchAgent is NOT installed ($PLIST_PATH not found)."
  else
    echo "LaunchAgent plist: $PLIST_PATH"
    if grep -Fq "<string>--deploy</string>" "$PLIST_PATH"; then
      echo "Auto-deploy: ENABLED"
    else
      echo "Auto-deploy: DISABLED (check-only)"
    fi
    if launchctl print "${domain}/${LABEL}" >/dev/null 2>&1; then
      echo "launchd state: LOADED"
      launchctl print "${domain}/${LABEL}" 2>/dev/null \
        | grep -E 'state =|last exit code =|runs =' | sed 's/^/  /' || true
    else
      echo "launchd state: NOT LOADED (plist exists on disk)"
    fi
  fi

  local latest_log=""
  if [ -d "$LOG_DIR" ]; then
    latest_log="$(ls -1t "$LOG_DIR"/refresh-*.log 2>/dev/null | head -n 1 || true)"
  fi
  if [ -n "$latest_log" ]; then
    echo ""
    echo "Latest refresh log ($latest_log):"
    tail -n 25 "$latest_log"
  elif [ -f "$LAUNCHD_LOG" ]; then
    echo ""
    echo "Latest launchd log ($LAUNCHD_LOG):"
    tail -n 25 "$LAUNCHD_LOG"
  fi
}

cmd_run_now() {
  require_macos
  local domain="gui/$(id -u)"
  if ! launchctl print "${domain}/${LABEL}" >/dev/null 2>&1; then
    echo "Error: LaunchAgent '$LABEL' is not loaded. Run './run.sh schedule install' first." >&2
    exit 1
  fi
  launchctl kickstart -k "${domain}/${LABEL}"
  echo "Triggered '$LABEL'. Follow logs with: tail -f \"$LAUNCHD_LOG\""
}

cmd_uninstall() {
  require_macos
  local domain="gui/$(id -u)"
  launchctl bootout "$domain" "$PLIST_PATH" 2>/dev/null || true
  if [ -f "$PLIST_PATH" ]; then
    rm -f "$PLIST_PATH"
    echo "Removed LaunchAgent: $PLIST_PATH"
  else
    echo "LaunchAgent plist was not present ($PLIST_PATH)."
  fi
}

SUBCMD="${1:-help}"
shift || true

case "$SUBCMD" in
  install)
    cmd_install "$@"
    ;;
  status)
    cmd_status "$@"
    ;;
  run-now)
    cmd_run_now "$@"
    ;;
  uninstall)
    cmd_uninstall "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "Error: unknown subcommand '$SUBCMD'." >&2
    usage >&2
    exit 1
    ;;
esac
