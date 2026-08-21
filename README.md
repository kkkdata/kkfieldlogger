# KK Field Logger

**Jobsite photo evidence for construction and field teams.**
工地影像证据系统：工人手机拍照上传，每条记录自动带上项目、工号、GPS 和时间；经理在线审核、批注、审批并生成报告。

Crews shoot from their phones; every upload carries the project, employee,
GPS, and time. Managers review, annotate, approve, and build reports.
Runs entirely on your own server or NAS — evidence stays on your drives.

> **Status: free beta (0.9.x).** Built and battle-tested on our own
> data-center construction projects in the US. Feedback:
> Michelle@kkdatasvc.com

## Features

- **Mobile capture** — photos, video, voice notes, receipts; offline queue
  with automatic retry; duplicate-upload protection; structured,
  localized error codes (EN/中文/Español)
- **Manager portal** — a "today" workbench that answers *what needs my
  attention*; filter/annotate/approve; client-visibility curation;
  audit log for every action
- **AI assist** — photo summaries and tags via external backends
  (e.g. Ollama); shadow-mode validation so unverified output never
  reaches users; PDF report generation
- **Self-hosted** — FastAPI + PostgreSQL(pgvector) + Docker Compose;
  single-tenant NAS profile with a first-run setup wizard, automatic
  pre-upgrade database backups, and disk-retention policies

## Quick start (Docker)

```bash
# see deploy/nas/docker-compose.yml for a complete, commented example
docker compose up -d
# first boot applies all database migrations, then visit http://<host>:8090
# the setup wizard creates your company and admin account
```

Images: [`kkkdata/kkfieldlogger`](https://hub.docker.com/r/kkkdata/kkfieldlogger)
(slim, ~220 MB compressed) and `:-full` (adds in-image voice transcription
and video keyframes). `linux/amd64` today; `arm64` planned.

## NAS editions

The `deploy/nas/` directory contains the TerraMaster TOS 7 App Center
package (compose file, manifest, icon, 14-language listing). The same
compose file works on any Docker-capable NAS or soft router.

## Architecture notes

- PostgreSQL 16 + pgvector (bundled compose service or external)
- Database-backed job queue with heartbeat-monitored workers
- Multi-tenant schema; the NAS profile simply hides platform surfaces
  (`KK_DEPLOYMENT_PROFILE=private`), so data migrates cleanly between
  self-hosted and hosted deployments
- Vision-model calls go to configurable external backends; the server
  itself stays lean

## Development

```bash
pip install -e ".[ai,dev]"
pytest
```

## License

[AGPL-3.0](LICENSE). © K&K Data Service Inc.
The mobile applications are not part of this repository.
