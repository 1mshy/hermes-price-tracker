#!/usr/bin/env bash
# Rebuild and redeploy the stack. Source is baked into the images (pricewatch's
# Dockerfile does `COPY src` + `pip install .`), so `docker compose restart`
# silently keeps running the old code — that is the failure this script exists
# to prevent. After every build it diffs the installed package against the
# working tree and refuses to report success on a mismatch.
#
#   ./scripts/rebuild.sh                 # pricewatch (the usual case)
#   ./scripts/rebuild.sh agent           # the hermes container (hermes/ changes)
#   ./scripts/rebuild.sh all             # everything
#   ./scripts/rebuild.sh --skip-tests    # skip the offline suite
#   ./scripts/rebuild.sh --smoke         # also drive the LLM through MCP + cron
set -euo pipefail
cd "$(dirname "$0")/.."

skip_tests=false
smoke=false
services=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-tests) skip_tests=true ;;
    --smoke)      smoke=true ;;
    -h|--help)    sed -n '2,12p' "$0" | sed 's/^#//; s/^ //'; exit 0 ;;
    all)                    services+=(pricewatch hermes) ;;
    agent|hermes)           services+=(hermes) ;;
    dashboard|gateway)      services+=(hermes) ;;   # both live in `hermes` now
    -*)                     echo "unknown flag: $1" >&2; exit 2 ;;
    *)                      services+=("$1") ;;
  esac
  shift
done
[[ ${#services[@]} -eq 0 ]] && services=(pricewatch)

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
step "build + recreate: ${services[*]}"
docker compose up -d --build --wait "${services[@]}"

# ── 3. prove the new code is what is actually running ───────────────────────
# Hashes every .py in the package on both sides and diffs the manifests, so a
# cached layer or a forgotten --build shows up as named files, not a guess.
read -r -d '' HASH_PY <<'PY' || true
import hashlib, os, sys
root = sys.argv[1]
rows = []
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    for name in filenames:
        if not name.endswith(".py"):
            continue
        path = os.path.join(dirpath, name)
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        rows.append((os.path.relpath(path, root), digest))
for rel, digest in sorted(rows):
    print(rel, digest)
PY

if rebuilding pricewatch; then
  step "verifying deployed source matches working tree"
  installed=$(docker compose exec -T pricewatch \
    python -c 'import pricewatch, os; print(os.path.dirname(pricewatch.__file__))' | tr -d '\r')

  if drift=$(diff -u \
      <(python3 -c "$HASH_PY" pricewatch/src/pricewatch) \
      <(docker compose exec -T pricewatch python -c "$HASH_PY" "$installed" | tr -d '\r')); then
    echo "container source == working tree"
  else
    echo "STALE OR DIVERGED — the container is not running your working tree:" >&2
    echo "$drift" | sed -n '3,40p' >&2
    exit 1
  fi

  step "engine health"
  curl -fsS "localhost:${PW_PORT:-8077}/api/health" && echo
fi

# ── 4. reconnect the agent ──────────────────────────────────────────────────
# A live dashboard session holds an MCP stream to the pricewatch container we
# just replaced; restarting it forces a fresh handshake against the new engine.
if rebuilding pricewatch && ! rebuilding hermes; then
  if docker compose ps --status running --services | grep -qx hermes; then
    step "restarting the agent for a fresh MCP handshake"
    docker compose up -d --force-recreate --wait hermes
  fi
fi

# ── 5. optional agent-level smoke ───────────────────────────────────────────
if [[ $smoke == true ]]; then
  step "agent smoke (real LLM)"
  ./scripts/agent-smoke.sh
fi

step "done"
docker compose ps
echo
echo "dashboard: http://localhost:${HERMES_DASHBOARD_PORT:-9119}"
echo "terminal agent: docker compose exec hermes hermes"
