#!/usr/bin/env python3
"""Bring hermes-shopping back after the laptop sleeps, unplugs, or moves.

Runs from a launchd user agent (see install-wake-agent.sh) every INTERVAL
seconds. launchd runs a missed interval as soon as the Mac wakes, so this is
also the wake hook, and a gap between two runs longer than the interval means
the machine was asleep or off — that is when the catch-up happens.

Every run:
  1. Docker Desktop not running?  Start it — on AC power always, on battery
     only with HS_START_DOCKER_ON_BATTERY=1 (someone who quit Docker on
     battery probably meant it).
  2. `docker compose up -d --no-build --wait` the stack, so a container that
     was stopped comes back on the image that is already built (never a build
     from launchd; that is scripts/rebuild.sh's job).
  3. Wait for the engine's /api/health and for DNS to resolve *inside* the
     engine container — Docker's host-side resolver is the last thing back
     after a resume and the first push after a wake used to die on it.

After a gap (or with --catch-up):
  4. POST /api/refresh — one sweep now, joined with the scheduler's own
     catch-up if that is already running, and it re-sends every alert that
     went undelivered while the network was gone.
  5. Give hermes' cron ticker a moment to fire its own overdue jobs, then
     re-run once any job that failed *during this resume window* (its LLM
     call or MCP handshake landed before the network was back).

Quiet by design: in steady state with everything healthy it exits without a
log line. Log: ~/Library/Logs/hermes-shopping/wake.log.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

REPO = pathlib.Path(__file__).resolve().parents[2]
HOME = pathlib.Path.home()
LOG_DIR = HOME / "Library" / "Logs" / "hermes-shopping"
STATE_DIR = HOME / "Library" / "Application Support" / "hermes-shopping"
LOG_PATH = LOG_DIR / "wake.log"
STATE_PATH = STATE_DIR / "wake-state.json"
LOCK_PATH = STATE_DIR / "wake.lock"
DOCKER_APP = "/Applications/Docker.app"

os.environ["PATH"] = ":".join(dict.fromkeys(
    os.environ.get("PATH", "").split(":") + ["/usr/local/bin", "/opt/homebrew/bin",
                                             "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))

_verbose = False


# ── plumbing ─────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    if _verbose:
        print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
    except OSError:
        pass
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def sh(cmd: list[str], timeout: float = 60, cwd: pathlib.Path | None = None,
       env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=cwd or REPO, env=env, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def compose(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return sh(["docker", "compose", *args], timeout=timeout)


def read_env() -> dict[str, str]:
    """Just enough of .env: KEY=VALUE lines, comments and quotes dropped."""
    out: dict[str, str] = {}
    path = REPO / ".env"
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1]:
            value = value[1:-1]
        out[key.strip()] = value
    return out


def load_state() -> dict:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def hold_awake() -> subprocess.Popen | None:
    """Keep the Mac from idling back to sleep while we work (battery: 1 min).
    caffeinate exits with us. Cannot stop a lid-close sleep — nothing can."""
    if not shutil.which("caffeinate"):
        return None
    try:
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


# ── host facts ───────────────────────────────────────────────────────────
def on_ac_power() -> bool | None:
    out = sh(["pmset", "-g", "batt"], timeout=10)
    if out.returncode != 0 or not out.stdout:
        return None
    first = out.stdout.splitlines()[0]
    return "AC Power" in first


def last_wake() -> int | None:
    """macOS wake timestamp, including short sleeps between calendar ticks."""
    result = sh(["sysctl", "-n", "kern.waketime"], timeout=10)
    match = re.search(r"\bsec\s*=\s*(\d+)", result.stdout)
    return int(match.group(1)) if result.returncode == 0 and match else None


def docker_ready(timeout: float = 20) -> bool:
    out = sh(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=timeout)
    return out.returncode == 0 and bool(out.stdout.strip())


def start_docker(deadline: float) -> bool:
    if not pathlib.Path(DOCKER_APP).exists():
        log(f"Docker Desktop not found at {DOCKER_APP}")
        return False
    log("Docker daemon is not answering — starting Docker Desktop")
    sh(["open", "-g", "-j", "-a", DOCKER_APP], timeout=30)
    while time.monotonic() < deadline:
        if docker_ready(timeout=15):
            log("Docker daemon is up")
            return True
        time.sleep(5)
    log("Docker Desktop did not come up in time")
    return False


# ── the stack ────────────────────────────────────────────────────────────
def services() -> list[str]:
    """The stack, plus signal-cli when its profile has been enabled (its
    container exists) — naming it activates the profile for `up`."""
    wanted = ["pricewatch", "hermes"]
    out = compose("ps", "-a", "--services", timeout=60)
    if out.returncode == 0 and "signal-cli" in out.stdout.split():
        wanted.append("signal-cli")
    return wanted


def container_health() -> dict[str, tuple[str, str]]:
    """{service: (state, health)} for the containers compose knows about."""
    out = compose("ps", "-a", "--format", "json", timeout=60)
    found: dict[str, tuple[str, str]] = {}
    if out.returncode != 0:
        return found
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows = json.loads(line)
        except ValueError:
            continue
        for row in rows if isinstance(rows, list) else [rows]:
            found[row.get("Service", "")] = (row.get("State", ""), row.get("Health", ""))
    return found


def all_healthy(wanted: list[str]) -> bool:
    health = container_health()
    for svc in wanted:
        state, status = health.get(svc, ("missing", ""))
        if state != "running" or status not in ("healthy", ""):
            return False
    return True


def ensure_containers(wanted: list[str], dry_run: bool) -> bool:
    cmd = ["up", "-d", "--no-build", "--wait", "--wait-timeout", "300", *wanted]
    if dry_run:
        log("dry-run: docker compose " + " ".join(cmd))
        return True
    # `up` leaves a running but unhealthy container untouched. Restart only
    # those services; Docker's restart policy does not act on health failures.
    unhealthy = [svc for svc, (state, health) in container_health().items()
                 if svc in wanted and state == "running" and health == "unhealthy"]
    if unhealthy:
        restarted = compose("restart", *unhealthy, timeout=120)
        if restarted.returncode:
            log("could not restart unhealthy services: " + ", ".join(unhealthy))
            return False
    out = compose(*cmd, timeout=420)
    tail = (out.stderr or out.stdout).strip().splitlines()[-3:]
    if out.returncode != 0:
        log(f"docker compose up failed (rc={out.returncode}): {' | '.join(tail)}")
        return False
    log("containers up: " + ", ".join(wanted) + (f" — {' | '.join(tail)}" if tail else ""))
    return True


def engine_health(port: str, timeout: float = 10) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_for_engine(port: str, deadline: float) -> dict | None:
    while time.monotonic() < deadline:
        health = engine_health(port)
        if health and health.get("status") == "ok":
            return health
        time.sleep(5)
    return None


def wait_for_dns(host: str, deadline: float) -> bool:
    """Name resolution as the *engine container* sees it, not as the host does."""
    probe = ["exec", "-T", "pricewatch", "python", "-c",
             "import socket,sys; socket.getaddrinfo(sys.argv[1], 443)", host]
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        if compose(*probe, timeout=40).returncode == 0:
            if attempts > 1:
                log(f"DNS inside the engine container is back ({host}, probe {attempts})")
            return True
        time.sleep(5)
    log(f"DNS inside the engine container still cannot resolve {host}")
    return False


# ── catch-up ─────────────────────────────────────────────────────────────
def refresh_engine(port: str, dry_run: bool) -> bool:
    if dry_run:
        log("dry-run: POST /api/refresh")
        return True
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/refresh", method="POST",
                                 data=b"", headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            summary = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"catch-up sweep failed: {exc}")
        return False
    alerts = summary.get("alerts") or []
    log(f"catch-up sweep: checked={summary.get('checked')} updated={summary.get('updated')} "
        f"failed={summary.get('failed')} alerts={len(alerts)} in {time.monotonic() - started:.0f}s")
    for alert in alerts:
        delivered = any(v == "sent" for v in (alert.get("channels") or {}).values())
        log(f"  alert: {alert.get('title')!r} {alert.get('price')} at {alert.get('store')} "
            f"— {'delivered' if delivered else 'NOT delivered: ' + str(alert.get('channels'))}")
    return (not summary.get("failed") and not any(
        alert.get("channels") and not any(v == "sent" for v in alert["channels"].values())
        for alert in alerts))


def hermes_jobs() -> list[dict]:
    out = compose("exec", "-T", "hermes", "cat", "/opt/data/cron/jobs.json", timeout=60)
    if out.returncode != 0:
        return []
    try:
        return json.loads(out.stdout).get("jobs", [])
    except ValueError:
        return []


def rerun_failed_jobs(since: dt.datetime, dry_run: bool) -> int:
    """Re-run, once, each cron job whose last run failed inside this resume
    window. A job that fails again moves its last_run_at past the window, so
    no wake re-runs the same failure twice."""
    reran = 0
    for job in hermes_jobs():
        if not job.get("enabled") or job.get("fire_claim") or job.get("state") == "running":
            continue
        status = job.get("last_status") or "ok"
        if status == "ok" or not job.get("last_run_at"):
            continue
        try:
            last = dt.datetime.fromisoformat(job["last_run_at"])
        except ValueError:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
        if last < since:
            continue
        name, job_id = job.get("name"), job.get("id")
        error = str(job.get("last_error") or status)[:100]
        if dry_run:
            log(f"dry-run: would re-run hermes job {name!r} ({job_id}) — failed at "
                f"{last:%H:%M:%S} UTC: {error}")
            continue
        # -d: the LLM jobs take minutes on the local model; the cron runner
        # takes the same per-job lock the gateway ticker does.
        out = compose("exec", "-d", "-T", "hermes", "hermes", "cron", "run",
                      "--accept-hooks", str(job_id), timeout=60)
        if out.returncode == 0:
            reran += 1
            log(f"re-running hermes job {name!r} ({job_id}) — it failed at "
                f"{last:%H:%M:%S} UTC during the resume: {error}")
        else:
            log(f"could not re-run hermes job {name!r}: {(out.stderr or out.stdout).strip()[:200]}")
    return reran


# ── main ─────────────────────────────────────────────────────────────────
def main() -> int:
    global _verbose
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--interval", type=int, default=int(os.environ.get("HS_WAKE_INTERVAL", 120)),
                        help="seconds between launchd runs; a gap of 3x this counts as a sleep")
    parser.add_argument("--catch-up", action="store_true",
                        help="run the catch-up (sweep + failed-job re-run) even without a gap")
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    parser.add_argument("--verbose", "-v", action="store_true", help="also print the log lines")
    parser.add_argument("--settle", type=int, default=90,
                        help="seconds to give hermes' own cron catch-up before re-running failures")
    args = parser.parse_args()
    _verbose = args.verbose or args.dry_run

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if _verbose:
            print("another wake_recover is running; leaving it to it")
        return 0

    started_at = dt.datetime.now(dt.timezone.utc)
    started_mono = time.monotonic()
    state = load_state()
    last_run = state.get("last_run")
    try:
        gap = started_at.timestamp() - float(last_run) if last_run else None
    except (TypeError, ValueError):
        gap = None
    wake_at = last_wake()
    resumed = (gap is None or gap > max(3 * args.interval, 300)
               or wake_at is not None and wake_at != state.get("last_wake"))
    catch_up = resumed or args.catch_up or state.get("catch_up_pending", False)
    env = read_env()
    port = env.get("PW_PORT", "8077")
    probe_host = urlsplit(env.get("NTFY_SERVER") or "https://ntfy.sh").hostname or "ntfy.sh"
    start_on_battery = os.environ.get("HS_START_DOCKER_ON_BATTERY", "0") in ("1", "true", "yes")

    def finish(rc: int) -> int:
        finished_at = dt.datetime.now(dt.timezone.utc)
        state.update({"last_run": finished_at.timestamp(), "last_run_iso": finished_at.isoformat(),
                      "catch_up_pending": bool(catch_up),
                      "last_wake": wake_at,
                      "last_result": rc})
        if not args.dry_run:
            save_state(state)
        return rc

    wanted = services() if docker_ready(timeout=15) else ["pricewatch", "hermes"]
    docker_was_up = docker_ready(timeout=15)
    stack_healthy = docker_was_up and all_healthy(wanted)
    catch_up = catch_up or not stack_healthy

    # Steady state and all is well: say nothing, touch the state, leave.
    if not catch_up and stack_healthy:
        return finish(0)

    awake = hold_awake()
    try:
        ac = on_ac_power()
        power = {True: "AC power", False: "battery", None: "unknown power"}[ac]
        why = (f"resume after {gap / 60:.0f} min gap" if gap is not None and resumed
               else "first run" if gap is None else "forced catch-up" if args.catch_up
               else "stack not healthy")
        log(f"── {why}; on {power}; docker {'up' if docker_was_up else 'down'}")

        if not docker_was_up:
            if ac is False and not start_on_battery:
                log("on battery and Docker is not running — leaving it off "
                    "(set HS_START_DOCKER_ON_BATTERY=1 to change that)")
                return finish(0)
            if args.dry_run:
                log("dry-run: would start Docker Desktop")
                return finish(0)
            if not start_docker(deadline=started_mono + 300):
                return finish(1)
            wanted = services()

        stack_ready = ensure_containers(wanted, args.dry_run)

        if args.dry_run:
            log("dry-run: would wait for /api/health and in-container DNS, then "
                + ("catch up" if catch_up else "stop here"))
            rerun_failed_jobs(started_at - dt.timedelta(minutes=5), dry_run=True)
            return finish(0)

        health = wait_for_engine(port, deadline=time.monotonic() + 240)
        if health is None:
            log(f"engine did not answer /api/health on :{port} — no catch-up possible")
            return finish(1)
        dns_ok = wait_for_dns(probe_host, deadline=time.monotonic() + 120)

        if not catch_up:
            log("stack is back" + ("" if dns_ok else " (DNS still down)"))
            return finish(0)

        sweep = health.get("sweep") or {}
        log(f"engine up; last sweep {sweep.get('last_sweep_at') or 'never this boot'}, "
            f"next {sweep.get('next_sweep_at')}")
        if not dns_ok:
            log("catch-up remains pending; retrying at the next host interval")
            return finish(1)
        if not refresh_engine(port, args.dry_run):
            log("catch-up was incomplete; retrying at the next host interval")
            return finish(1)

        # hermes catches up its own overdue jobs within a minute of resuming;
        # the ones that fired into the blackout are the ones to run again.
        remaining = args.settle - (time.monotonic() - started_mono)
        if remaining > 0:
            time.sleep(remaining)
        window = started_at - dt.timedelta(minutes=5)
        reran = rerun_failed_jobs(window, args.dry_run)
        log(f"catch-up done: {reran} hermes job(s) re-run")
        catch_up = not stack_ready
        return finish(0 if stack_ready else 1)
    finally:
        if awake is not None:
            awake.terminate()


if __name__ == "__main__":
    sys.exit(main())
