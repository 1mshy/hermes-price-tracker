#!/usr/bin/env bash
# Agent-level regression smoke: drives the real LLM through the MCP + cron
# surface with one-shot prompts. Run after changing the engine, the skills,
# or the model endpoint. Each block prints the agent's final answer.
set -euo pipefail
cd "$(dirname "$0")/.."

run() {
  echo "──────────────────────────────────────────────────────────"
  echo "PROMPT: $1"
  echo "──────────────────────────────────────────────────────────"
  # exec, not run: the agent home has a single writer by design, and the
  # `hermes` container is already holding it open for the gateway.
  docker compose exec -T hermes hermes -z "$1" 2>/dev/null | tail -8
  echo
}

run "Use the cronjob tool to list my scheduled cron jobs and report what you find in one sentence."

run "Check recent reddit chatter about SUNLU AMS heater deals using the community_pulse tool and summarize in 2 sentences with any prices mentioned."

run "What am I tracking right now, and has anything moved? Use list_trackers and list_alert_events."
