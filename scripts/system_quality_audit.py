from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import requests


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCAN_DIRS = ("app", "docs", "scripts", "tests")
TEXT_SUFFIXES = {".py", ".html", ".css", ".js", ".md", ".yml", ".yaml", ".toml", ".ini", ".sh", ".ps1"}
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".data", "logs"}


@dataclass
class ScanFinding:
    kind: str
    path: str
    line: int
    text: str


def _iter_text_files() -> Iterable[Path]:
    for directory in SCAN_DIRS:
        root = ROOT / directory
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if any(part in EXCLUDED_PARTS for part in path.parts):
                continue
            yield path


def scan_risk_patterns(limit: int) -> list[ScanFinding]:
    patterns: list[tuple[str, re.Pattern[str]]] = [
        ("todo_marker", re.compile(r"\b(TODO|FIXME|HACK|XXX)\b", re.IGNORECASE)),
        ("broad_exception", re.compile(r"except Exception\b")),
        ("debug_print", re.compile(r"\bprint\(")),
        ("plaintext_secret_marker", re.compile(r"(password|secret|api[_-]?key)\s*[:=]", re.IGNORECASE)),
        ("non_https_url", re.compile(r"http://(?!localhost|127\.0\.0\.1)")),
    ]
    findings: list[ScanFinding] = []
    for path in _iter_text_files():
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            for kind, pattern in patterns:
                if pattern.search(stripped):
                    findings.append(
                        ScanFinding(
                            kind=kind,
                            path=str(path.relative_to(ROOT)).replace("\\", "/"),
                            line=index,
                            text=stripped[:220],
                        )
                    )
                    break
            if len(findings) >= limit:
                return findings
    return findings


def collect_route_summary() -> dict[str, object]:
    with contextlib.redirect_stdout(io.StringIO()):
        from app.main import app

    method_counter: Counter[str] = Counter()
    prefixes: Counter[str] = Counter()
    for route in app.routes:
        methods = sorted(getattr(route, "methods", []) or [])
        path = getattr(route, "path", "")
        if methods:
            for method in methods:
                method_counter[method] += 1
        else:
            method_counter["MOUNT"] += 1
        prefix = "/" + path.strip("/").split("/", 1)[0] if path.strip("/") else "/"
        prefixes[prefix] += 1
    return {
        "route_count": len(app.routes),
        "methods": dict(sorted(method_counter.items())),
        "prefixes": dict(sorted(prefixes.items())),
    }


def collect_file_summary() -> dict[str, object]:
    suffix_counter: Counter[str] = Counter()
    total_files = 0
    for path in _iter_text_files():
        total_files += 1
        suffix_counter[path.suffix.lower()] += 1
    required_paths = [
        "app/main.py",
        "app/api/routes/api_v2.py",
        "app/api/routes/portal.py",
        "app/services/job_queue.py",
        "app/services/ai_pipeline.py",
        "app/templates/base.html",
        "docker-compose.yml",
        "alembic.ini",
        "docs/development-handbook.md",
    ]
    return {
        "text_file_count": total_files,
        "suffixes": dict(sorted(suffix_counter.items())),
        "required_paths": {path: (ROOT / path).exists() for path in required_paths},
        "alembic_version_count": len(list((ROOT / "alembic" / "versions").glob("*.py"))),
    }


def collect_git_summary() -> dict[str, object]:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return {"is_git_repository": False, "error": "git executable not found"}
    if result.returncode != 0:
        return {"is_git_repository": False, "error": result.stderr.strip()}
    return {
        "is_git_repository": True,
        "dirty_line_count": len([line for line in result.stdout.splitlines() if line.strip()]),
        "status": result.stdout.splitlines(),
    }


def collect_live_checks(base_url: str | None) -> dict[str, object] | None:
    if not base_url:
        return None
    base_url = base_url.rstrip("/")
    checks: dict[str, object] = {}
    for path in ("/api/v2/healthz", "/api/v2/readyz", "/", "/portal/login"):
        try:
            response = requests.get(f"{base_url}{path}", timeout=15)
            checks[path] = {
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type"),
                "ok": 200 <= response.status_code < 400,
            }
        except requests.RequestException as exc:
            checks[path] = {"ok": False, "error": str(exc)}
    return checks


def run_tests() -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return {
        "command": f"{sys.executable} -m pytest",
        "returncode": result.returncode,
        "passed": result.returncode == 0,
        "output_tail": result.stdout.splitlines()[-20:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only stability and usability quality snapshot.")
    parser.add_argument("--base-url", help="Optional running app base URL for live HTTP checks.")
    parser.add_argument("--run-tests", action="store_true", help="Run the full pytest suite as part of the audit.")
    parser.add_argument("--finding-limit", type=int, default=80)
    args = parser.parse_args()

    payload: dict[str, object] = {
        "root": str(ROOT),
        "files": collect_file_summary(),
        "routes": collect_route_summary(),
        "git": collect_git_summary(),
        "risk_findings": [asdict(item) for item in scan_risk_patterns(args.finding_limit)],
        "live_checks": collect_live_checks(args.base_url),
    }
    if args.run_tests:
        payload["tests"] = run_tests()
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if not payload.get("tests") or payload["tests"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
