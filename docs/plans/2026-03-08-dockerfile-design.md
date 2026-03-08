# Dockerfile and Docker Compose Design

## Goal

Create Dockerfile, docker-compose.yml, and .dockerignore for the Scrapeyard service.

## Architecture

- Multi-stage Dockerfile: Stage 1 exports Poetry deps to requirements.txt, Stage 2 installs and copies source
- Base image: `python:3.12-slim` (no Playwright/browser support — basic fetcher only)
- Single docker-compose service with named volume at `/data`, all `SCRAPEYARD_*` env vars listed explicitly
- Port 8420

## Decisions

- **Basic fetcher only** — no Playwright/Chromium install, keeps image lean (~200MB vs ~1.5GB+)
- **All env vars explicit** — docker-compose.yml lists every `SCRAPEYARD_*` variable with defaults for operator documentation
- **Multi-stage build** — Poetry stays in builder stage, final image uses plain pip

## Files

- `Dockerfile` — multi-stage build
- `docker-compose.yml` — single service, named volume, env vars
- `.dockerignore` — excludes dev artifacts
