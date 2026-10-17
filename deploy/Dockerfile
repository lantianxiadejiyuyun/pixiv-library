FROM node:24-alpine AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
ARG INSTALL_AI=false
ARG AI_RUNTIME=cpu
ARG TARGETARCH
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    LIBRARY_MIHOMO_PATH=/usr/local/bin/mihomo LIBRARY_MIHOMO_DIR=/library/proxy
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends apksigner aapt default-jre-headless && rm -rf /var/lib/apt/lists/*
COPY scripts/install_mihomo.py /tmp/install_mihomo.py
COPY scripts/mihomo-LICENSE.txt /usr/share/doc/mihomo/LICENSE
# Install a pinned, SHA-256 verified official release during image build only.
# TARGETARCH supports native/cross builds for Linux amd64 and arm64 NAS hosts.
RUN python /tmp/install_mihomo.py --platform linux-${TARGETARCH} --output /usr/local/bin/mihomo
COPY server/requirements*.txt /tmp/
COPY scripts/install_intel_opencl.py /tmp/install_intel_opencl.py
# Intel is opt-in and uses one ORT distribution, never two conflicting wheels.
# Debian 13 has no intel-opencl-icd package. Install a SHA-256 verified official
# Intel release with matching IGC/GMM, then resolve system dependencies via APT.
RUN if [ "$INSTALL_AI" = "true" ] && [ "$AI_RUNTIME" = "intel" ]; then \
        test "$TARGETARCH" = "amd64" && \
        python /tmp/install_intel_opencl.py && \
        rm -rf /var/lib/apt/lists/*; \
    elif [ "$AI_RUNTIME" != "cpu" ]; then \
        echo 'AI_RUNTIME must be cpu, or intel with INSTALL_AI=true on amd64' >&2; exit 1; \
    fi
RUN pip install --no-cache-dir -r /tmp/requirements.txt && \
    if [ "$INSTALL_AI" = "true" ]; then \
        if [ "$AI_RUNTIME" = "intel" ]; then pip install --no-cache-dir -r /tmp/requirements-ai-intel.txt; \
        else pip install --no-cache-dir -r /tmp/requirements-ai.txt; fi; \
    fi
COPY server/ /app/server/
COPY deploy/compose.ai-node*.yaml deploy/Dockerfile.ai-controller deploy/Dockerfile.ai-controller.dockerignore deploy/ai-node.env.example /app/deploy/
COPY docs/AI_NODES.md /app/docs/AI_NODES.md
COPY scripts/container-start.sh /app/container-start.sh
COPY scripts/smoke_mihomo.py scripts/install_mihomo.py scripts/install_intel_opencl.py scripts/backup.py scripts/compose.py /app/scripts/
COPY scripts/ai_node.py scripts/bootstrap_ai_node.py /app/scripts/
COPY --from=web /web/dist /app/web/dist
RUN useradd --create-home --uid 10001 library && \
    mkdir -p /library/data /library/imports /library/models /library/proxy && \
    chown -R library:library /library /app && chmod 700 /library/proxy
USER library
WORKDIR /app/server
EXPOSE 8000
CMD ["sh", "/app/container-start.sh"]
