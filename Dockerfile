# syntax=docker/dockerfile:1

# One image, two roles. The same image runs as:
#   - the gateway      : nginx, serving traffic       (entrypoint overridden to `nginx`)
#   - the orchestrator : the control plane            (default entrypoint)
# in two separate containers that share the managed config directory. Using one image for
# both guarantees that the `nginx -t` the orchestrator runs is the very nginx that serves, and
# keeping them in separate containers keeps the rule that nginx goes on serving when the
# orchestrator stops. See compose.yaml.

# ---------------------------------------------------------------------------
# Panel stage: build the web panel (ui/) into nanio_orchestrator/web/dist. Node is
# needed only here; nothing of it reaches the runtime image.
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS ui

WORKDIR /src/ui
COPY ui/package.json ui/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY ui ./
# vite writes to ../nanio_orchestrator/web/dist (see ui/vite.config.ts)
RUN npm run build

# ---------------------------------------------------------------------------
# Build stage: install the package into a venv. Same base as the runtime stage
# so the venv's interpreter path resolves identically there.
# ---------------------------------------------------------------------------
FROM nginx:stable-bookworm AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /app/.venv

WORKDIR /src
# README.md and LICENSE are build inputs (`readme` / `license` in pyproject.toml).
COPY pyproject.toml README.md LICENSE ./
COPY nanio_orchestrator ./nanio_orchestrator
# The built panel is package data: it has to be in place before the package is installed.
COPY --from=ui /src/nanio_orchestrator/web/dist ./nanio_orchestrator/web/dist
RUN --mount=type=cache,target=/root/.cache/pip \
    /app/.venv/bin/pip install --disable-pip-version-check .

# ---------------------------------------------------------------------------
# Runtime stage: nginx + Python + rclone + the venv.
# ---------------------------------------------------------------------------
FROM nginx:stable-bookworm

LABEL org.opencontainers.image.title="nanio-orchestrator" \
      org.opencontainers.image.description="Nginx configuration manager and gateway orchestrator for nanio S3 storage clusters." \
      org.opencontainers.image.source="https://github.com/nunokisc/nanio-orchestrator" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/* \
    # the stock welcome site would answer on port 80
    && rm /etc/nginx/conf.d/default.conf \
    # The base image links the logs to /dev/stdout and /dev/stderr, which only works for the
    # process that owns the container's output. `nginx -t` run by the orchestrator has a
    # captured stderr and fails to open them. PID 1's output is the container log in both
    # roles (the orchestrator shares the gateway's process namespace), so link to that.
    && ln -sf /proc/1/fd/1 /var/log/nginx/access.log \
    && ln -sf /proc/1/fd/2 /var/log/nginx/error.log \
    && mkdir -p /etc/nginx/nanio/pools /etc/nginx/nanio/vhosts /data

# rclone drives migrations; a single static binary.
COPY --from=rclone/rclone:1 /usr/local/bin/rclone /usr/local/bin/rclone
COPY --from=build /app/.venv /app/.venv
COPY docker/nanio-orchestrator.conf /etc/nginx/conf.d/nanio-orchestrator.conf

# DEV=false: production mode without a config file — settings come from the environment.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DEV=false \
    NANIO_ORCHESTRATOR_DB_PATH=/data/orchestrator.db \
    NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR=/etc/nginx/nanio

WORKDIR /data
EXPOSE 8080

# The base image stops with SIGQUIT (nginx's graceful stop). The orchestrator wants
# SIGTERM; the gateway service sets SIGQUIT back in compose.yaml.
STOPSIGNAL SIGTERM

# Exec-form: the orchestrator is PID 1 and receives signals directly.
# `docker run <image> --help` and `docker run <image> config show` behave like the CLI.
ENTRYPOINT ["nanio-orchestrator"]
CMD ["serve"]
