# Dashboard themes

YAML files in this directory are copied into `$HERMES_HOME/dashboard-themes/` by
`entrypoint.sh` on every container start, which is where Hermes scans for user
themes (`hermes_cli/web_server.py::_discover_user_themes`). They then appear in
the dashboard's theme switcher next to the built-ins.

Pick the active one with `HERMES_DASHBOARD_THEME` in `.env` — `entrypoint.sh`
regenerates `config.yaml` on every start, so a theme chosen in the UI (which
writes `dashboard.theme` to that file) is reset on the next restart.

## Vendored

`boring-dark.yaml` and `boring-light.yaml` are verbatim copies from
[sorenisanerd/hermes-dashboard-themes](https://github.com/sorenisanerd/hermes-dashboard-themes)
(MIT), at commit `1bdd018` (2026-05-14). Kept byte-identical so re-syncing
upstream is a plain copy:

```bash
git clone --depth 1 https://github.com/sorenisanerd/hermes-dashboard-themes /tmp/hdt
cp /tmp/hdt/themes/*.yaml hermes/dashboard-themes/
```

Upstream's `scripts/install.sh` ends with `hermes config set
display.dashboard_theme <name>`; on hermes-agent 0.19.0 the real key is
`dashboard.theme`, so that line is a no-op. Use `HERMES_DASHBOARD_THEME`
instead.
