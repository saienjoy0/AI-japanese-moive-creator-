# PR5 Design: Persist PreparedEpisode as a LumenX Project

## Purpose

PR5 persists the deterministic `PreparedEpisode` created in PR4 into the
existing LumenX project store without calling providers or creating generated
media.

```text
PreparedEpisode
  -> LumenX model adapter
  -> Script / Character / Scene / Prop / StoryboardFrame
  -> read-back verification
  -> atomic projects.json + persistence index commit
```

PR5 does not create `VideoTask`, image URLs, video URLs, audio URLs, provider
task IDs, or API requests.

## Existing LumenX storage boundary

LumenX stores projects as a JSON object in `output/projects.json`.

```json
{
  "<project_id>": {
    "...": "Script fields"
  }
}
```

`ComicGenPipeline._load_data()` reads every value as a `Script`. PR5 therefore
writes exactly that format and does not add metadata keys to `projects.json`.

Japanese-drama ownership and provenance are stored separately in:

```text
output/jp_drama/persistence_index.json
```

The index records:

- deterministic LumenX project ID
- `source_digest`
- package and episode IDs
- PreparedEpisode schema and compiler versions
- canonical LumenX project hash
- source series ID
- episode number

## Model mapping

| PreparedEpisode | LumenX |
|---|---|
| `ProjectDraft` | `Script` |
| `CharacterSeed` | `Character` |
| `LocationSeed` | `Scene` |
| `PropSeed` | `Prop` |
| `StoryboardFrameDraft` | `StoryboardFrame` |
| `DialogueDraft` | legacy dialogue text + first `DialogueStructured` |
| `CameraDraft` | camera fields + `CameraMovementData` |
| `AudioDraft` | `AudioNote` |
| render intent and exact trace fields | `composition_data.jp_drama` |

The LumenX project remains self-contained. PR5 does not create or mutate a
LumenX `Series`; the original series ID is retained in the persistence index.

## Determinism

The imported project uses IDs already created by PR4.

```text
project:   jpdrama_<package_id>
character: <episode_id>_character_<source_character_id>
scene:     <episode_id>_location_<source_location_id>
prop:      <episode_id>_prop_<source_prop_id>
frame:     <episode_id>_<shot_id>
```

Generated-media timestamps are not needed, so imported project timestamps are
set to deterministic zero values. Canonical project hashes use sorted compact
JSON and SHA-256.

## Idempotency and conflicts

Saving the same `PreparedEpisode` twice produces:

```text
first save  -> created
second save -> unchanged
```

No files are rewritten for `unchanged`.

PR5 refuses to overwrite when:

- the same project ID belongs to another source digest
- only one of the project or index entries exists
- a managed project was manually modified
- the adapter output changed for the same source

`--overwrite` is required to replace or repair these states.

## Transaction and rollback

Both output files are staged in their destination directories and fsynced
before replacement.

```text
stage projects.json
stage persistence_index.json
replace projects.json
replace persistence_index.json
read back and verify
```

If replacement or read-back verification fails, both files are restored to
their previous bytes. A project is never intentionally left half-registered.

## Verification

Before and after persistence, PR5 verifies:

- project ID, title, and episode number
- exact character, scene, prop, and frame IDs
- all frame references resolve
- per-frame Japanese-drama trace data matches PR4
- no `VideoTask` exists
- no generated media URL exists
- the project round-trips through the LumenX `Script` model
- external API call count is zero

`generation_ready=false` or a readiness error blocks persistence.

## CLI

Dry-run:

```bash
python -m src.apps.jp_drama.workflows.save_prepared_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --projects-file output/projects.json \
  --index-file output/jp_drama/persistence_index.json \
  --dry-run
```

Persist:

```bash
python -m src.apps.jp_drama.workflows.save_prepared_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --projects-file output/projects.json \
  --index-file output/jp_drama/persistence_index.json \
  --report output/jp_drama/persistence_result.json
```

Explicit replacement:

```bash
python -m src.apps.jp_drama.workflows.save_prepared_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --overwrite
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success, unchanged, or successful dry-run |
| 1 | invalid input |
| 2 | PreparedEpisode not ready |
| 3 | persistence conflict |
| 4 | conversion/read-back verification failure |
| 5 | storage or transaction failure |

## Completion criteria

- a strict PR4 sample becomes a valid LumenX `Script`
- `projects.json` remains readable by existing LumenX models
- the second save is idempotent and byte-stable
- partial commit failure restores both files
- generated tasks and media URLs remain absent
- no provider module or network call is used
- existing `comic_gen` models and pipeline remain unchanged
- PR3, PR4, PR5, and Foundation CI pass
