#!/usr/bin/env python3
"""Validate response files produced by the offline FastAPI smoke test."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def validate(health_path: Path, openapi_path: Path) -> None:
    health = read_json(health_path)
    openapi = read_json(openapi_path)

    if health.get("ok") is not True:
        raise ValueError(f"health response is not OK: {health}")

    info = openapi.get("info")
    if not isinstance(info, dict) or info.get("title") != "AI Comic Gen API":
        raise ValueError(f"unexpected OpenAPI info: {info}")

    paths = openapi.get("paths")
    if not isinstance(paths, dict) or "/health" not in paths:
        raise ValueError("OpenAPI document does not expose /health")

    print("BACKEND HEALTH OK")
    print(json.dumps(health, ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--health", type=Path, default=Path("health.json"))
    parser.add_argument("--openapi", type=Path, default=Path("openapi.json"))
    args = parser.parse_args()

    try:
        validate(args.health, args.openapi)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"BACKEND SMOKE INVALID: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
