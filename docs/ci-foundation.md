# Foundation CI

This repository must be testable without paid AI calls before Japanese short-drama features are added.

## What the workflow checks

- Python 3.11 dependency installation
- Python byte-code compilation for `src/`
- Model catalog validation using the existing catalog validator
- FastAPI startup and `/health` / `/openapi.json` responses
- Frontend dependency installation, type checking, unit tests, and production build
- Docker Compose configuration parsing
- FFmpeg generation of a three-shot 9:16 MP4 with Japanese burned-in subtitles and an audio stream

## External API policy

The workflow sets `DISABLE_EXTERNAL_AI=true` and does not provide provider credentials. It must never call DashScope, MuleRouter, Kling, Vidu, Gemini, or any other paid generation endpoint.

The mock episode is deterministic local media generated from FFmpeg color and sine-wave sources. Its purpose is to prove that video concatenation, Japanese subtitle rendering, audio muxing, and final MP4 inspection work in the CI environment.

## Output artifact

The `offline-mock-episode` artifact contains:

- `mock_episode.mp4`
- `captions.srt`
- `manifest.json`
- the three source shot MP4 files

## Scope

This is a foundation smoke test. A later change will add application-level mock providers that exercise the full LumenX project pipeline without paid APIs.
