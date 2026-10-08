#!/usr/bin/env bash
# Install (or remove) the macOS launchd agent that brings the stack back after
# the laptop sleeps, is unplugged, or is moved: it runs scripts/host/
# wake_recover.py every INTERVAL seconds, and launchd fires a missed interval
# the moment the Mac wakes — that is the wake/plug-in hook.
#
#   ./scripts/host/install-wake-agent.sh                    # install or refresh, run once now
#   ./scripts/host/install-wake-agent.sh --interval 60      # poll faster (default 120 s)
#   ./scripts/host/install-wake-agent.sh --start-docker-on-battery
#   ./scripts/host/install-wake-agent.sh --wake-at 08:55    # + daily scheduled wake (sudo pmset)
#   ./scripts/host/install-wake-agent.sh --print-plist           # inspect generated config without installing
#   ./scripts/host/install-wake-agent.sh --status
#   ./scripts/host/install-wake-agent.sh --uninstall
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO=$(pwd)

LABEL=com.hermes-shopping.wake
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/hermes-shopping"
interval=120
on_battery=0
wake_at=""
action=install

while [[ $# -gt 0 ]]; do
  case "$1" in
    --interval)                 interval=$2; shift ;;
    --start-docker-on-battery)  on_battery=1 ;;
    --wake-at)                  wake_at=$2; shift ;;
    --status)                   action=status ;;
    --print-plist)               action=print ;;
    --uninstall)                action=uninstall ;;
    -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

domain="gui/$(id -u)"

status() {
  if launchctl print "$domain/$LABEL" >/dev/null 2>&1; then
    echo "agent $LABEL: loaded"
    launchctl print "$domain/$LABEL" | grep -E 'state =|last exit code|run interval|program =|event triggers =' | sed 's/^/  /'
  else
    echo "agent $LABEL: not loaded"
  fi
  echo "log: $LOG_DIR/wake.log"
  [[ -f $LOG_DIR/wake.log ]] && tail -n 5 "$LOG_DIR/wake.log" | sed 's/^/  /'
  echo "scheduled power events:"; pmset -g sched | sed 's/^/  /'
}

if [[ $action == status ]]; then status; exit 0; fi

if [[ $action == uninstall ]]; then
  launchctl bootout "$domain/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL (logs and state left in place)"
  exit 0
fi

python3=$(command -v python3)
# Calendar triggers coalesce missed runs on wake. StartInterval explicitly
# drops sleep-time events (see launchd.plist(5)). Generate XML with plistlib
# so a repository path containing ampersands is also safe.
if [[ $action != print ]]; then mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"; fi
"$python3" - "$PLIST" "$REPO" "$python3" "$interval" "$on_battery" "$action" <<'PYGEN'
import pathlib, plistlib, sys
path, repo, interpreter, raw_interval, on_battery, action = sys.argv[1:]
try:
    interval = int(raw_interval)
except ValueError:
    raise SystemExit("--interval must be a whole number of seconds")
if interval < 60 or interval > 3600 or interval % 60 or 3600 % interval:
    raise SystemExit("--interval must be a multiple of 60 that divides 3600 (e.g. 60, 120, 300)")
logs = pathlib.Path.home() / "Library/Logs/hermes-shopping"
config = {
    "Label": "com.hermes-shopping.wake",
    "ProgramArguments": [interpreter, repo + "/scripts/host/wake_recover.py",
                         "--interval", str(interval)],
    "WorkingDirectory": repo,
    "StartCalendarInterval": [{"Minute": minute} for minute in range(0, 60, interval // 60)],
    "RunAtLoad": True, "ProcessType": "Background", "LowPriorityIO": True,
    "StandardOutPath": str(logs / "launchd.out"),
    "StandardErrorPath": str(logs / "launchd.err"),
    "EnvironmentVariables": {
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HS_START_DOCKER_ON_BATTERY": on_battery,
        "HS_WAKE_INTERVAL": str(interval),
    },
}
data = plistlib.dumps(config)
if action == "print":
    sys.stdout.buffer.write(data)
else:
    pathlib.Path(path).write_bytes(data)
PYGEN
if [[ $action == print ]]; then exit 0; fi
plutil -lint "$PLIST" >/dev/null

launchctl bootout "$domain/$LABEL" 2>/dev/null || true
launchctl bootstrap "$domain" "$PLIST"
echo "installed $PLIST (every ${interval}s, RunAtLoad; start Docker on battery: $on_battery)"

if [[ -n $wake_at ]]; then
  # pmset repeat *replaces* any existing repeating event; show what was there.
  if pmset -g sched | grep -qi 'repeating'; then
    echo "note: replacing the existing repeating power event:"; pmset -g sched | sed 's/^/  /'
  fi
  echo "scheduling a daily wake at $wake_at (needs sudo)"
  sudo pmset repeat wakeorpoweron MTWRFSU "$wake_at:00"
fi

echo
status
