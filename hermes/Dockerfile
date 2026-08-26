FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HERMES_HOME=/root/.hermes

# hermes-agent shells out to git/node for skills and MCP servers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates nodejs npm ripgrep \
    && rm -rf /var/lib/apt/lists/*

# hermes-agent does not depend on `mcp`, but its HTTP MCP transport imports
# `streamablehttp_client` from mcp.client.streamable_http. That symbol was renamed
# in mcp 2.x, so the client must stay on the 1.x line for `transport: http` to work.
# (The pricewatch container serves MCP with 2.x — they are independent images.)
RUN pip install --no-cache-dir "hermes-agent==0.19.0" "mcp>=1.9,<2"

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY skills /opt/hermes-skills
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["hermes"]
