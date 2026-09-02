# NAS packaging (TerraMaster TOS 7)

This directory is the TOS 7 App Center package payload. Background and
review rules: `docs/nas/tos7-packaging-plan.md`; work plan:
`docs/nas/dual-track-plan.md`.

## Package contents

| file | purpose |
|---|---|
| `docker-compose.yml` | 3 services: app (`kkfieldlogger`, port 8090), worker, pgvector db |
| `config.ini` | TOS manifest (`application_type: docker`, beta) |
| `kkfieldlogger.lang` | 14-language store listing (zh/en/es translated, rest English) |
| `kkfieldlogger.svg` | App Center icon |

## Build and publish images (blocked on: Docker Hub org)

```bash
# slim image (NAS default): no in-image AI, vision AI stays on external
# backends; voice transcription and video keyframes disabled gracefully
docker build -f docker/Dockerfile --build-arg INSTALL_AI=false \
  -t kkkdata/kkfieldlogger:1.0.000 .
docker push kkkdata/kkfieldlogger:1.0.000
```

Measured 2026-08-19 (static ffmpeg): slim 866MB, full (INSTALL_AI=true, production) 1.68GB.
Both variants boot-tested: non-root (`kkapp`), clean migrations 0001→head
against `pgvector/pgvector:pg16`, healthz OK, guarded degradation confirmed.

## Pack for the App Center

```bash
# tools from https://github.com/TerraMasterOfficial/app-pkg-tools
cd deploy/nas
tar czf kkfieldlogger.tar.gz config.ini kkfieldlogger.lang kkfieldlogger.svg docker-compose.yml
# then run their makeapp_x64 / submit the Key Info Table to pm@terra-master.com
```

## Release flow

`scripts/release_nas.sh <version>` builds both variants, pushes
`kkkdata/kkfieldlogger:<version>` (slim) and `:<version>-full`, and packs
`deploy/nas/kkfieldlogger.tar.gz` for App Center submission.
Docker Hub account: `kkkdata` (login lives on the 5820 host).

## Open TODOs before submission

1. First-run bootstrap wizard and `KK_DEPLOYMENT_PROFILE=private` gating (M2).
2. `KK_DB_PASSWORD` / `KK_SESSION_SECRET` autogeneration at first run (M2);
   beta ships with documented defaults, acceptable for LAN appliances only.
3. Verify `pgvector/pgvector:pg16` passes image-source review; fallback is
   publishing our own db image under the account.
