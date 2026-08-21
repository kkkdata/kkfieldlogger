from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import requests


def _record_clip(stream_url: str, output_path: Path, *, duration_seconds: int, timeout_seconds: int) -> None:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is not installed or not available on PATH")

    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-rtsp_transport",
        "tcp",
        "-i",
        stream_url,
        "-t",
        str(duration_seconds),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        str(output_path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0 or not output_path.is_file():
        raise RuntimeError((completed.stderr or completed.stdout or "ffmpeg capture failed").strip())


def _upload_file(
    cloud_base_url: str,
    *,
    token: str,
    company_id: str,
    source: str,
    clip_path: Path,
    metadata_json: dict[str, object],
    chunk_size_mb: int,
) -> dict[str, object]:
    chunk_size_bytes = max(1, chunk_size_mb) * 1024 * 1024
    file_size = clip_path.stat().st_size
    total_chunks = max(1, int(math.ceil(file_size / chunk_size_bytes)))
    upload_id = str(uuid4())
    endpoint = cloud_base_url.rstrip("/") + "/api/v2/media/upload"
    final_response: dict[str, object] | None = None

    with clip_path.open("rb") as handle:
        for chunk_index in range(total_chunks):
            payload = handle.read(chunk_size_bytes)
            response = requests.post(
                endpoint,
                headers={"X-Webhook-Token": token},
                data={
                    "media_type": "video",
                    "source": source,
                    "company_id": company_id,
                    "upload_id": upload_id,
                    "chunk_index": str(chunk_index),
                    "total_chunks": str(total_chunks),
                    "filename": clip_path.name,
                    "metadata_json": json.dumps(metadata_json),
                },
                files={"upload": (clip_path.name, payload, "video/mp4")},
                timeout=120,
            )
            response.raise_for_status()
            final_response = response.json()
    assert final_response is not None
    return final_response


def main() -> None:
    parser = argparse.ArgumentParser(description="Edge agent for RTSP camera capture and upload.")
    parser.add_argument("--cloud-base-url", required=True)
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--stream-url", required=True)
    parser.add_argument("--camera-name", required=True)
    parser.add_argument("--webhook-token", required=True)
    parser.add_argument("--duration-seconds", type=int, default=15)
    parser.add_argument("--timeout-seconds", type=int, default=45)
    parser.add_argument("--chunk-size-mb", type=int, default=8)
    parser.add_argument("--source", default="edge_gateway")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="edge-camera-agent-") as temp_dir:
        clip_path = Path(temp_dir) / f"{args.camera_name.replace(' ', '-').lower()}-{uuid4().hex[:8]}.mp4"
        _record_clip(
            args.stream_url,
            clip_path,
            duration_seconds=args.duration_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        response_payload = _upload_file(
            args.cloud_base_url,
            token=args.webhook_token,
            company_id=args.company_id,
            source=args.source,
            clip_path=clip_path,
            metadata_json={
                "camera_name": args.camera_name,
                "stream_url": args.stream_url,
                "protocol": "rtsp",
                "capture_mode": "edge_gateway",
            },
            chunk_size_mb=args.chunk_size_mb,
        )
        print(json.dumps(response_payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
