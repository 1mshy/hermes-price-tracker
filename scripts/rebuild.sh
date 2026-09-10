#!/usr/bin/env bash
# Rebuild and redeploy the stack. Source is baked into the images (pricewatch's
# Dockerfile does `COPY src` + `pip install .`), so `docker compose restart`
# silently keeps running the old code — that is the failure this script exists
# to prevent. After every build it diffs what is inside the container against
# the working tree and refuses to report success on a mismatch.
#
#   ./scripts/rebuild.sh                 # pricewatch (the usual case)
#   ./scripts/rebuild.sh agent           # the hermes container (hermes/ changes)
#   ./scripts/rebuild.sh all             # pricewatch + hermes (+ signal-cli if enabled)
#   ./scripts/rebuild.sh all --force     # all-in-one: rebuild both images, restart every container
#   ./scripts/rebuild.sh --force         # restart the targets even if nothing changed
#   ./scripts/rebuild.sh --skip-tests    # skip the offline suite
#   ./scripts/rebuild.sh --smoke         # also drive the LLM through MCP + cron
#   ./scripts/rebuild.sh --dry-run       # show what compose would do; no build, no restart
set -euo pipefail
cd "$(dirname "$0")/.."

skip_tests=false
smoke=false
force=false
dry_run=false
targets=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-tests)     skip_tests=true ;;
    --smoke)          smoke=true ;;
    --force|--restart) force=true ;;
    --dry-run)        dry_run=true ;;
    -h|--help)        awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
    all)                    targets+=(all) ;;
    agent|hermes)           targets+=(hermes) ;;
    dashboard|gateway)      targets+=(hermes) ;;   # both live in `hermes` now
    -*)                     echo "unknown flag: $1" >&2; exit 2 ;;
    *)                      targets+=("$1") ;;
  esac
  shift
done
[[ ${#targets[@]} -eq 0 ]] && targets=(pricewatch)

compose=(docker compose)
if [[ $dry_run == true ]]; then compose=(docker compose --dry-run); fi

# `docker compose ps` is read-only, so it never takes the --dry-run flag.
exists()  { docker compose ps -a --services | grep -qx "$1"; }
running() { docker compose ps --status running --services | grep -qx "$1"; }

# `all` means every container in the stack. signal-cli sits behind a compose
# profile, so it is only included when it has been enabled (its container
# exists); naming it explicitly is enough for compose to activate the profile.
services=()
for t in "${targets[@]}"; do
  if [[ $t == all ]]; then
    services+=(pricewatch hermes)
    if exists signal-cli; then services+=(signal-cli); fi
  else
    services+=("$t")
  fi
done

rebuilding() { [[ " ${services[*]} " == *" $1 "* ]]; }

step() { echo; echo "── $* ──────────────────────────────────────────────" | cut -c1-72; }

# ── 1. offline suite ────────────────────────────────────────────────────────
if rebuilding pricewatch && [[ $skip_tests == false ]]; then
  step "pricewatch unit tests"
  if [[ ! -x pricewatch/.venv/bin/python ]]; then
    echo "bootstrapping pricewatch/.venv"
    ( cd pricewatch && uv venv .venv && uv pip install -p .venv/bin/python -e . pytest )
  fi
  ( cd pricewatch && .venv/bin/python -m pytest tests/ -q )
fi

# ── 2. build and recreate ───────────────────────────────────────────────────
# `up --build` only recreates a container whose image or config changed;
# --force restarts the targets regardless. Compose starts them in dependency
# order (pricewatch healthy before hermes), so one `up` covers the whole stack.
step "build + recreate: ${services[*]}"
up=("${compose[@]}" up -d --wait --build)
if [[ $force == true ]]; then up+=(--force-recreate); fi

if rebuilding pricewatch; then
  "${up[@]}" "${services[@]}"
else
  # hermes depends on pricewatch and compose builds dependencies too, so
  # without --no-deps `rebuild.sh agent` would rebuild — and possibly recreate —
  # pricewatch with no tests and no drift check. --no-deps also skips the
  # service_healthy wait, so make sure the engine is up before hermes starts.
  if ! running pricewatch; then
    step "pricewatch is not running — starting it from its existing image"
    "${compose[@]}" up -d --wait --no-build pricewatch
  fi
  "${up[@]}" --no-deps "${services[@]}"
fi

# ── 3. prove the new code is what is actually running ───────────────────────
# Hashes every file under a path on both sides and diffs the manifests, so a
# cached layer or a forgotten --build shows up as named files, not a guess.
# Takes a directory or a single file, plus an optional filename suffix filter.
read -r -d '' HASH_PY <<'PY' || true
import hashlib, os, sys
root = sys.argv[1]
suffix = sys.argv[2] if len(sys.argv) > 2 else ""
def digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()
if os.path.isfile(root):
    print(os.path.basename(root), digest(root))
    sys.exit(0)
rows = []
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    for name in filenames:
        if name == ".DS_Store" or not name.endswith(suffix):
            continue
        path = os.path.join(dirpath, name)
        rows.append((os.path.relpath(path, root), digest(path)))
for rel, digest_ in sorted(rows):
    print(rel, digest_)
PY

# verify_tree <service> <working-tree path> <path inside the container> [suffix]
verify_tree() {
  local svc=$1 local_path=$2 remote=$3 suffix=${4:-} drift
  if drift=$(diff -u \
      <(python3 -c "$HASH_PY" "$local_path" "$suffix") \
      <(docker compose exec -T "$svc" python3 -c "$HASH_PY" "$remote" "$suffix" | tr -d '\r')); then
    echo "$local_path == $svc:$remote"
  else
    echo "STALE OR DIVERGED — $svc:$remote is not your working tree's $local_path:" >&2
    echo "$drift" | sed -n '3,40p' >&2
    return 1
  fi
}

if [[ $dry_run == true ]]; then
  step "dry run — skipping source verification, health check and smoke"
else
  if rebuilding pricewatch; then
    step "verifying deployed source matches working tree"
    installed=$(docker compose exec -T pricewatch \
      python -c 'import pricewatch, os; print(os.path.dirname(pricewatch.__file__))' | tr -d '\r')
    verify_tree pricewatch pricewatch/src/pricewatch "$installed" .py

    step "engine health"
    curl -fsS "localhost:${PW_PORT:-8077}/api/health" && echo
  fi

  # The hermes image is the official one plus three COPYs (see hermes/Dockerfile);
  # check each of them the same way, so a stale skill or init hook cannot hide.
  if rebuilding hermes; then
    step "verifying deployed agent files match working tree"
    verify_tree hermes hermes/skills /opt/hermes-skills
    verify_tree hermes hermes/dashboard-themes /opt/hermes-dashboard-themes
    verify_tree hermes hermes/cont-init.d/018-hermes-shopping /etc/cont-init.d/018-hermes-shopping
  fi
fi

# ── 4. reconnect the agent ──────────────────────────────────────────────────
# A live dashboard session holds an MCP stream to the pricewatch container we
# just replaced; restarting it forces a fresh handshake against the new engine.
if rebuilding pricewatch && ! rebuilding hermes; then
  if running hermes; then
    step "restarting the agent for a fresh MCP handshake"
    "${compose[@]}" up -d --force-recreate --wait hermes
  fi
fi

# ── 5. optional agent-level smoke ───────────────────────────────────────────
if [[ $smoke == true && $dry_run == false ]]; then
  step "agent smoke (real LLM)"
  ./scripts/agent-smoke.sh
fi

step "done"
docker compose ps
echo
echo "dashboard: http://localhost:${HERMES_DASHBOARD_PORT:-9119}"
echo "terminal agent: docker compose exec hermes hermes"
