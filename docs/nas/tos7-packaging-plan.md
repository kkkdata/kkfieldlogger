# TOS 7 (TerraMaster) App Packaging Plan

Status: research complete 2026-08-19. Source of truth:
<https://github.com/TerraMasterOfficial/app-pkg-tools> — "TOS 7 Application
Development Guide" (20 chapters, v2.7) plus `TOS 7-template-docker/`.
Submission goes to pm@terra-master.com with their "Third Party Application
Key Info Table".

## What a TOS 7 docker app is

A `<appid>.tar.gz` containing exactly four root-level files:

```
kkfieldlogger.tar.gz
├── config.ini            # JSON manifest (application_type: "docker")
├── kkfieldlogger.lang    # localized names/descriptions (zh + en)
├── kkfieldlogger.svg     # app icon
└── docker-compose.yml    # Compose Spec 3.8+
```

TOS runs the compose file itself (project name from `compose_project`),
checks Docker Engine on install, shows the app "Abnormal" after 3 failed
healthchecks, and opens the web UI from `path: "http://${ip}:<port>"` plus a
trailing `x-app-meta: {web: {port, protocol}}` block in the compose file.

## Hard rules from the review standards

- **Images must come from Docker Hub, fixed tags, never `:latest`.**
  Non-Docker Hub registries (ghcr.io, quay.io, private) are rejected outright.
- **Non-root**: `user: "1000:1000"` required; `privileged` and
  `network_mode: host` are prohibited.
- **Ports**: host ports in 8000–19999; 22/80/443/8181/5050 forbidden.
- **Volumes**: all state under `/Volume*/DockerAppData/<appid>/...`
  (volume number chosen by the user at install).
- **Healthcheck required for every service**, `restart: unless-stopped`,
  explicit `TZ`, no secrets hardcoded in the compose file.

## Gap analysis for KK Field Logger

| # | Requirement | Current state | Work |
|---|---|---|---|
| 1 | Docker Hub image, fixed tag | Image built locally from source (3.03GB) | Create `kkdatasvc/…` Docker Hub repo; CI build+push versioned tags; slim the image (whisper/opencv → optional) |
| 2 | Non-root containers | Dockerfile runs as root | Add app user, chown volume paths, verify uploads/thumbnails still write |
| 3 | No 80/443, no nginx/certbot | Prod fronted by nginx with TLS | NAS profile drops nginx; app serves directly on e.g. 8090 with `x-app-meta`. Proxy-dependent logic (X-Real-IP) must degrade cleanly |
| 4 | Volumes under DockerAppData | `/opt/...` and `/mnt/movies/kkdata` | Parameterize compose: db data, media root, logs, `.env` under `/Volume*/DockerAppData/kkfieldlogger/` |
| 5 | db image from Docker Hub | `pgvector/pgvector:pg16` (community) | Acceptable tier is "well-known community"; keep, note as review risk, fallback = publish our own db image |
| 6 | Healthcheck every service | app has `/api/v2/healthz`; worker has none | Add worker heartbeat (touch file + `test` command); db uses `pg_isready` |
| 7 | First-run without platform admin | Multi-tenant SaaS bootstrap via CLI | Single-tenant profile: first-run wizard (create company+admin), hide platform surfaces, autogenerate `KK_SESSION_SECRET` into `.env` |
| 8 | Migration safety on "update" click | Forward-only migrations, no downgrade | Add pre-upgrade `pg_dump` into the app's data dir before `alembic upgrade head`; test migrations against a restored production dump |
| 9 | Disk growth on appliance | ~71 derived rows/photo, DB 190KB/photo, media 2.7MB/photo | Retention/compaction: ai_analysis_logs superseded-run archival, audit log rolling, recycle-bin auto-expiry |

## Suggested build order

1. **Phase A — containers ship clean** (pure infra): non-root Dockerfile,
   image slimming, Docker Hub publishing, NAS compose file + config.ini +
   icon + lang file, worker healthcheck. Testable on any Docker host.
2. **Phase B — single-tenant product profile**: first-run bootstrap wizard,
   platform-admin surfaces hidden behind a deployment-profile setting,
   secret autogeneration, pre-upgrade dump hook.
3. **Phase C — package and submit**: build with `makeapp_x64`, local TOS 7
   test, Key Info Table to pm@terra-master.com.

## Corrected size numbers (2026-08-19, after audit)

The first 3.03GB figure included ~104MB of committed demo content
(`app/static/public/test-site`, a dental-clinic demo with videos) that is
now excluded via `.dockerignore` → image is **2.81GB**. Layer breakdown:
pip dependencies 1.05GB, apt layer 797MB (includes build-essential, only
needed at build time), python:3.12-slim base ~150MB, app code ~12MB.
Phase A result: multi-stage build + AI extras split + static ffmpeg →
**slim 866MB / full 1.68GB** (was 3.03GB).

DB (312MB, 119k derived rows): ~85% of rows belong to the real tenant, so
test data is not the driver. Part of the per-photo AI-log volume comes from
reprocess experiments, but the ~38 evidence_observations per photo are
structural (evidence bundle design) — the retention/compaction conclusion
stands. Idle RSS ~470MiB full stack. Clean-install migration 0001→0044
verified against `pgvector/pgvector:pg16`.
