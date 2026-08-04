#!/usr/bin/env python3
"""Start the LumenX FastAPI server and validate local health responses.

The script does not configure or call any external AI provider. It owns the
server process lifecycle so the GitHub Actions workflow stays simple and the
same check can be run locally.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "backend.log"
HEALTH_PATH = ROOT / "health.json"
OPENAPI_PATH = ROOT / "openapi.json"
BASE_URL = "http://127.0.0.1:17177"


def prepare_directories() -> None:
    directories = [
        "output/uploads",
        "output/video",
        "output/assets",
        "output/storyboard",
        "output/audio",
        "output/export",
        "output/video_inputs",
        "output/outputs/videos",
        "output/playground/images",
        "output/playground/videos",
    ]
    for relative in directories:
        (ROOT / relative).mkdir(parents=True, exist_ok=True)


def request_json(path: str, timeout: float = 3.0) -> dict[str, Any]:
    request = urllib.request.Request(f"{BASE_URL}{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError(f"{path} did not return a JSON object")
    return value


def validate(health: dict[str, Any], openapi: dict[str, Any]) -> None:
    if health.get("ok") is not True:
        raise ValueError(f"health response is not OK: {health}")
    info = openapi.get("info")
    if not isinstance(info, dict) or info.get("title") != "AI Comic Gen API":
        raise ValueError(f"unexpected OpenAPI info: {info}")
    paths = openapi.get("paths")
    if not isinstance(paths, dict) or "/health" not in paths:
        raise ValueError("OpenAPI document does not expose /health")


def print_log() -> None:
    if LOG_PATH.exists():
        print("--- backend.log ---", file=sys.stderr)
        print(LOG_PATH.read_text(encoding="utf-8", errors="replace")[-20000:], file=sys.stderr)


def main() -> int:
    prepare_directories()
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "DISABLE_EXTERNAL_AI": "true",
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost,127.0.0.1",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )

    process: subprocess.Popen[str] | None = None
    try:
        with LOG_PATH.open("w", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "src.apps.comic_gen.api:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "17177",
                ],
                cwd=ROOT,
                env=environment,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )

            health: dict[str, Any] | None = None
            last_error: Exception | None = None
            for _ in range(120):
                if process.poll() is not None:
                    break
                try:
                    health = request_json("/health")
                    break
                except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
                    last_error = exc
                    time.sleep(1)

            if health is None:
                raise RuntimeError(f"server did not become healthy: {last_error}")

            openapi = request_json("/openapi.json", timeout=10)
            validate(health, openapi)
            HEALTH_PATH.write_text(json.dumps(health, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            OPENAPI_PATH.write_text(json.dumps(openapi, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("BACKEND HEALTH OK")
            print(json.dumps(health, ensure_ascii=False, sort_keys=True))
            return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        print(f"BACKEND SERVER SMOKE FAILED: {exc}", file=sys.stderr)
        print_log()
        return 1
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
