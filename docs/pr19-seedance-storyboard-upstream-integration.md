# PR19 — Seedance2 Storyboard Generator upstream integration

## Goal

Use the existing professional Seedance2 Storyboard Generator workflow as the
creative upstream instead of re-inventing its script, asset, storyboard, sound,
and continuation rules.

```text
Japanese story/script
  -> pinned upstream Skill (文・资・视・剪)
  -> original upstream Markdown artifacts
     - *_剧本.md
     - *_素材清单.md
     - *_E01_分镜.md
  -> strict compatibility importer
  -> canonical SeedanceStoryboardPackage
  -> operator manifest / later H3-Wan-Seedance adapters
```

## Pinned upstream

- repository: `liangdabiao/Seedance2-Storyboard-Generator`
- commit: `17b9ca6dfac3e4a086a2874791ef19ae5aae3932`
- core Skill plus five reference documents
- exact Git blob SHAs are stored in
  `third_party/seedance2_storyboard_generator/upstream.lock.json`

The upstream README describes the content as learning/reference material. The
sync command keeps the source in `.local/upstream` rather than copying and
silently editing it inside this repository.

## Preserved upstream semantics

The importer preserves, without translating them into a newly invented
creative format:

- C/S/P asset identifiers
- complete image-generation prompt text
- per-episode upload slot tables
- `@图片N` references
- timed storyboard beats
- style prompt
- sound prompt
- ending-frame description
- `将@视频1延长Xs` continuation instruction

## Strict failures

The importer stops instead of guessing when:

- an asset catalogue has no C/S/P entries
- a project directory contains multiple competing asset catalogues
- an upload row has no asset ID
- an episode references an undefined upload slot
- an episode references an asset absent from the catalogue
- a timeline starts after zero, overlaps, has gaps, or exceeds 15 seconds
- episode or asset IDs are duplicated

Unused catalogue assets remain warnings because a project may import only a
subset of its episodes.

## Commands

Synchronise the upstream Skill locally:

```bash
python -m src.apps.jp_drama.workflows.sync_seedance_storyboard_upstream
```

Import a generated upstream project:

```bash
python -m src.apps.jp_drama.workflows.import_seedance_storyboard \
  --project-dir path/to/generated-project \
  --output-dir output/jp_drama/seedance-storyboard
```

Outputs:

```text
seedance_storyboard_package.json
seedance_operator_manifest.md
```

## Fixture

The zero-call fixture models a Japanese reversal short drama and proves that a
critical appointment letter (`P01`), the bag (`P02`), C/S/P references, timed
shots, sound, ending frames, and second-episode video continuation survive the
import.

## Safety

- no LLM, image, video, TTS, or provider call in CI
- no provider credentials
- no media committed
- upstream sync is explicit, pinned, and hash verified
- generated upstream prose is retained for traceability
