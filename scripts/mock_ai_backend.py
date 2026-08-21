from __future__ import annotations

import argparse
import json
import math
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def build_mock_embedding(text: str) -> list[float]:
    vector = [0.0] * 768
    for token in (text or "").lower().replace("\n", " ").split():
        index = sum(ord(char) for char in token) % 768
        vector[index] += 1.0
    if not any(vector):
        vector[0] = 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


class MockAIHandler(BaseHTTPRequestHandler):
    server_version = "MockAIBackend/1.0"

    def _send_json(self, payload: dict, status_code: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        content_length = int(self.headers.get("Content-Length") or "0")
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        return json.loads(raw_body.decode("utf-8") or "{}")

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/tags":
            self._send_json(
                {
                    "models": [
                        {"name": "mock-vision:latest"},
                        {"name": "mock-embed:latest"},
                    ]
                }
            )
            return
        self._send_json({"detail": "Not found"}, status_code=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/embeddings":
            payload = self._read_json()
            prompt = str(payload.get("prompt") or "")
            self._send_json({"embedding": build_mock_embedding(prompt)})
            return

        if self.path == "/api/generate":
            payload = self._read_json()
            if payload.get("format") == "json":
                self._send_json(
                    {
                        "response": json.dumps(
                            {
                                "ai_summary": "Mock field photo",
                                "labels": ["project", "construction"],
                                "defects": [],
                            }
                        )
                    }
                )
                return

            prompt = str(payload.get("prompt") or "")
            self._send_json(
                {
                    "response": "\n".join(
                        [
                            "# Mock Inspection Report",
                            "",
                            "## Executive Summary",
                            "A concise automated report generated from the selected field photos.",
                            "",
                            "## Key Findings",
                            "- Mock field photo evidence was reviewed.",
                            f"- Prompt context: {prompt[:80]}",
                            "",
                            "## Recommended Actions",
                            "- Review the cited defects and validate remediation timing.",
                        ]
                    )
                }
            )
            return

        self._send_json({"detail": "Not found"}, status_code=HTTPStatus.NOT_FOUND)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock AI backend for local smoke tests")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), MockAIHandler)
    print(f"Mock AI backend listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
