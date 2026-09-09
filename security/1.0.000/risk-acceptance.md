# Security Risk Acceptance Statement — kkkdata/kkfieldlogger:1.0.000

Prepared by K&K Data Service Inc. for the TerraMaster App Center review
of KK Field Logger 1.0.000 (image digest
`sha256:5ccf6755293bcce3d7c3eba286c538f11e6c3ab2e7efa8034d3e6d6503b0d52d`,
linux/amd64 manifest of the multi-arch tag). Companion artifacts submitted
with this statement: full Trivy scan (`kkfieldlogger-1.0.000-trivy.json`)
and CycloneDX SBOM (`kkfieldlogger-1.0.000-sbom.cdx.json`).

## 1. Remediation completed since the 0.9.001 review

| Review finding | Action taken |
|---|---|
| pillow fixable HIGHs | upgraded to >=12.3.0 |
| weasyprint fixable HIGHs | upgraded to >=68 |
| pip < 26.1.2 | build-stage pip pinned >=26.1.2; **pip removed from the runtime image entirely** (nothing installs packages at runtime), which also removes pip's vendored copies of setuptools 70.3.0 and msgpack 1.1.2 — the last two scanner-flagged fixable HIGHs |
| stale base image | rebuilt from the current python:3.12-slim (Debian 13.6) with `apt-get upgrade` applied |

**Result: the current Trivy scan reports zero vulnerabilities with an
available fix, at any severity.** Every remaining finding is a Debian
package CVE marked `affected` or `fix_deferred` with no fixed version
published by Debian at scan time (2026-09-02).

## 2. Residual CRITICAL findings accepted (5 unique CVEs)

| CVE | Component | Debian status | Exposure in this application |
|---|---|---|---|
| CVE-2026-13221 | perl / perl-base / libperl5.40 / perl-modules | affected, no fix | perl is present only as a dependency of `postgresql-client` (used for automatic pre-upgrade `pg_dump` backups). No perl code is executed by the application at runtime; no network-facing perl exists. |
| CVE-2026-42496 | same perl packages | fix deferred | same as above |
| CVE-2026-8376 | same perl packages | affected, no fix | same as above |
| CVE-2026-58016 | libglib2.0 | affected, no fix | present as a shared-library dependency of the PDF rendering stack (pango/gdk-pixbuf). It processes only server-generated report content, never untrusted user input, and the D-Bus introspection code path in the CVE is not exercised. |
| CVE-2026-6653 | libxml2 | affected, no fix | linked by the PDF stack; the application parses no untrusted XML (uploads are images/video/audio validated by Pillow/ffprobe). The DoS path requires attacker-controlled XML input that does not exist here. |

## 3. Mitigating architecture

- Application containers run as a non-root user (uid 1000).
- The runtime image contains no package manager (pip removed) and no
  compiler toolchain (multi-stage build).
- The only network listener is the Python application (uvicorn); perl,
  libxml2, and glib are never exposed to network input.
- Database credentials and session secrets are generated randomly at
  install time; no fixed credentials ship in the image or compose file.
- We rebuild and re-scan images on every release and will pick up Debian
  fixes for the CVEs above as soon as they are published, shipping them
  in the next point release.

Ray (Rui Liang) — K&K Data Service Inc. — ray@kkdatasvc.com — 2026-09-02
