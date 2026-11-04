FROM debian:13-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    WORKSPACE_DIR=/workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates coreutils grep python3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 debian13 \
    && useradd --system --uid 10001 --gid debian13 --home-dir /workspace --no-create-home --shell /usr/sbin/nologin debian13 \
    && install -d -o debian13 -g debian13 -m 0750 /workspace

COPY --chown=root:root server.py /opt/debian13/system/server.py
COPY --chown=root:root integration_progress.py /opt/debian13/system/integration_progress.py
COPY --chown=root:root legacy_svl_kernel.py /opt/debian13/system/legacy_svl_kernel.py
COPY --chown=root:root svl_compat.py /opt/debian13/system/svl_compat.py
COPY --chown=root:root work_store.py /opt/debian13/system/work_store.py
COPY --chown=root:root chat_memory.py /opt/debian13/system/chat_memory.py
COPY --chown=root:root github_dashboard.py /opt/debian13/system/github_dashboard.py
COPY --chown=root:root ui_sync.py /opt/debian13/system/ui_sync.py
COPY --chown=root:root original_agent_handoff.py /opt/debian13/system/original_agent_handoff.py
COPY --chown=root:root ai_center_worker.py /opt/debian13/system/ai_center_worker.py
COPY --chown=root:root codex_status.py /opt/debian13/system/codex_status.py
COPY --chown=root:root mcp_launcher.py /opt/debian13/system/mcp_launcher.py
COPY --chown=root:root mcp_autoconfigure.py /opt/debian13/system/mcp_autoconfigure.py
COPY --chown=root:root readiness.py /opt/debian13/system/readiness.py
COPY --chown=root:root mcp_server.py /opt/debian13/system/mcp_server.py
COPY --chown=root:root alex_bridge.py /opt/debian13/system/alex_bridge.py
COPY --chown=root:root alex_handoff.py /opt/debian13/system/alex_handoff.py
COPY --chown=root:root original_agents.py /opt/debian13/system/original_agents.py
COPY --chown=root:root integration_smoke.py /opt/debian13/system/integration_smoke.py
COPY --chown=root:root gpt-actions.openapi.json /opt/debian13/system/gpt-actions.openapi.json
RUN chmod 0644 /opt/debian13/system/server.py /opt/debian13/system/legacy_svl_kernel.py /opt/debian13/system/svl_compat.py /opt/debian13/system/integration_progress.py /opt/debian13/system/chat_memory.py /opt/debian13/system/ai_center_worker.py /opt/debian13/system/codex_status.py /opt/debian13/system/mcp_launcher.py /opt/debian13/system/mcp_autoconfigure.py /opt/debian13/system/work_store.py /opt/debian13/system/readiness.py /opt/debian13/system/mcp_server.py /opt/debian13/system/alex_bridge.py /opt/debian13/system/alex_handoff.py /opt/debian13/system/original_agents.py /opt/debian13/system/gpt-actions.openapi.json /opt/debian13/system/integration_smoke.py /opt/debian13/system/github_dashboard.py /opt/debian13/system/ui_sync.py /opt/debian13/system/original_agent_handoff.py

# Catch missing runtime modules at image-build time, before deployment.
RUN PYTHONPATH=/opt/debian13/system python3 -c 'import server; import ai_center_worker'

USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python3", "/opt/debian13/system/server.py"]
