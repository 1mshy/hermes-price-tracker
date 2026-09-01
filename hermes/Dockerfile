# Upstream retired the PyPI path. `pip install hermes-agent` is now listed as
# unsupported alongside Homebrew and the AUR — "may already be broken right now,
# break more in future", fixes for it are not merged, and the dashboard nags
# about it on every load. Docker is Tier 1, so instead of building our own image
# around a pip install we layer this project's config onto the official one.
# https://hermes-agent.nousresearch.com/docs/getting-started/platform-support
#
# Pin a date tag: `latest` moves near-daily, and a Docker install has no
# `hermes update` — upgrading means bumping this line and rebuilding
# (`./scripts/rebuild.sh agent`).
FROM nousresearch/hermes-agent:v2026.8.31

# Nothing to apt-get: the base already ships git, curl, node, npm, ripgrep,
# ffmpeg and Playwright's Chromium. It also seals /opt/hermes root-owned and
# sets HERMES_DISABLE_LAZY_INSTALLS=1, so anything the agent needs at runtime
# has to be baked in at build time — which is what these two COPYs are for.
COPY skills /opt/hermes-skills
COPY dashboard-themes /opt/hermes-dashboard-themes

# s6-overlay runs /etc/cont-init.d/* as root before the dashboard service and
# the container command start; that is where config.yaml gets rendered from the
# environment. See the header of the script for why it is numbered 018.
COPY cont-init.d/018-hermes-shopping /etc/cont-init.d/018-hermes-shopping
RUN chmod +x /etc/cont-init.d/018-hermes-shopping

# ENTRYPOINT, CMD, WORKDIR, HERMES_HOME=/opt/data and the /opt/data volume are
# all inherited. The entrypoint hands PID 1 to s6, which supervises the gateway
# and the dashboard and restarts either one if it crashes; the compose
# `command:` runs alongside them as s6's main program. Overriding ENTRYPOINT
# here would throw all of that away.
