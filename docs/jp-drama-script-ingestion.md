# PR10 Design: Normal Japanese Script Ingestion

## Goal

Accept a normal Japanese TXT, Markdown file, or pasted script and produce the
existing strict `EpisodePackage` contract.

```text
normal Japanese script
  -> text normalization
  -> LLM StructuredScriptDraft
  -> Pydantic validation
  -> deterministic EpisodePackage compiler
  -> existing domain validation
  -> episode_package.json
```

The LLM does not generate the full production package directly. It produces a
small intermediate contract containing characters, scenes, beats, actions, and
dialogue. IDs, timing, shot duration, camera defaults, cost coverage, domain
links, and production metadata are compiled deterministically.

## Input

Exactly one input source is required:

```bash
python -m src.apps.jp_drama.workflows.ingest_script \
  --input script.md \
  --output-dir output/jp_drama/ingestion \
  --llm-provider dashscope
```

or:

```bash
python -m src.apps.jp_drama.workflows.ingest_script \
  --text "美緒は雨の夜、店へ入ろうとした。..." \
  --output-dir output/jp_drama/ingestion \
  --llm-provider dashscope
```

Zero-cost fixture execution:

```bash
python -m src.apps.jp_drama.workflows.ingest_script \
  --input examples/jp_drama/script_ingestion/sample_script.md \
  --output-dir output/jp_drama/ingestion \
  --llm-provider fixture
```

## Intermediate contract

`StructuredScriptDraft` contains:

- title and synopsis
- target duration
- characters with stable ASCII IDs
- scenes and continuity rules
- ordered beats
- action and camera hints
- dialogue with speaker IDs
- ambience and sound effects
- unresolved non-blocking items

The contract rejects unknown fields, duplicate IDs, missing references,
non-contiguous beat order, dialogue speakers absent from a beat, and too few
beats for the requested duration.

## Bounded repair

The service performs at most two LLM calls:

1. initial structuring
2. one repair containing the prior payload and exact validation errors

There is no open-ended agent loop. A second invalid result creates a failed
`ingestion_report.json` and exits with a validation error.

## Deterministic compiler

Given the same normalized script, structured draft, and compilation options,
the compiler produces the same package.

It deterministically creates:

- package/source/beat/adaptation/episode IDs from the source digest
- one shot per beat
- exact target-duration distribution
- dialogue timings that fit each shot
- camera defaults
- continuity notes
- complete shot cost coverage
- provider/model placeholders compatible with the existing offline model catalog

Dialogue shots use `video_plus_tts`; non-dialogue shots use `still_motion`.
Actual Wan or Seedance selection remains a later provider-planning decision.

## Outputs

```text
output/jp_drama/ingestion/
├── normalized_script.txt
├── structured_script.json
├── episode_package.json
├── ingestion_report.json
└── unresolved_items.json
```

`episode_package.json` can be passed directly into the existing PR4 preparation
compiler.

## LLM providers

### Fixture

- deterministic
- zero network calls
- used by CI
- can return a sequence of payloads to test repair behavior

### DashScope

- reads only the configured API-key environment variable
- defaults to `qwen-plus`
- requests JSON output using the `StructuredScriptDraft` JSON Schema
- does not serialize or log the API key

## Safety and scope

- no video, image, or TTS generation occurs
- no paid provider credential is present in CI
- external LLM calls are counted in the report
- source rights status defaults to `unknown` and can be set explicitly
- unresolved optional facts are warnings rather than invented values
- the original normalized script is retained for traceability
