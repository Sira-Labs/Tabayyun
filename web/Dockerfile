# syntax=docker/dockerfile:1.7
# Stay on the LTS line: Node 25+ no longer ships Corepack, which provides the pinned pnpm.
FROM node:22-bookworm-slim AS build
RUN corepack enable
WORKDIR /app
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY web ./
RUN pnpm build

# Caddy serves the static SPA with security headers and proxies /api to the API container.
FROM caddy:2-alpine AS web
COPY deploy/caddy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
EXPOSE 80
