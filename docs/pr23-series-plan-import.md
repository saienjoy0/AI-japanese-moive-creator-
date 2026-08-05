# PR23: Multi-Episode Series Plan Import

## Purpose

Import the reviewed three-episode production contracts from
`saienjoy0/Storyboard-Generator` without flattening them into three unrelated
single episodes.

The authoritative source is the pair:

```text
一房の葡萄_generation_plan.yaml
一房の葡萄_asset_catalog.yaml
```

The source repository is pinned at:

```text
3070100f6ff25a3994a749b240f423f703cd294f
```

No LLM, image, video, TTS, browser, or provider operation is performed by this
import.

## Why two input files are required

The generation plan owns:

- source title, author, and declared public-domain status;
- series ID and three-episode arc;
- 24 FPS and exact episode/frame totals;
- all 15 ten-second production segments;
- dialogue speaker, mode, and text;
- character, background-character, location, and prop references;
- paint and grape state transitions;
- provider policy, acceptance criteria, and manual-review requirements.

The asset catalogue owns:

- the complete C/S/P namespace;
- reviewed names and descriptions;
- image prompts and negative prompts;
- episode usage declarations;
- character voice-identity requirements;
- distinct E02 and E03 grape-instance rules.

The importer refuses to create production artifacts when either side is missing
or their project, title, asset references, episode usage, speaker kind, timing,
or continuity declarations conflict.

## Output

```text
output/one-bunch-series/
├── series_manifest.json
├── import_report.json
├── E01/
│   ├── prepared_episode.json
│   ├── h3/
│   │   ├── generation_plan_episode.json
│   │   └── asset_bundle_pending.json
│   ├── wan/
│   │   ├── generation_plan_episode.json
│   │   └── asset_bundle_pending.json
│   └── seedance/
│       ├── generation_plan_episode.json
│       └── asset_bundle_pending.json
├── E02/
└── E03/
```

## Series manifest

The immutable manifest retains:

- `project_id` and `series_id`;
- source title and author;
- `rights_status=public_domain`;
- source repository and full commit SHA;
- SHA-256 for both source YAML files;
- canonical digest of their validated content;
- 3 episodes, 15 segments, 24 FPS;
- 1200 frames per episode and 3600 total frames;
- paths and digests for every PreparedEpisode, GenerationPlan, and pending
  AssetBundle;
- acceptance criteria and mandatory manual review items;
- `external_api_calls=0`.

## PreparedEpisode conversion

Each logical episode becomes one PreparedEpisode while preserving the shared
series ID and numeric episode number.

Every source segment becomes exactly one ten-second `StoryboardFrameDraft`.
The importer does not run the semantic splitter again and therefore does not
turn the approved 15 production units into a different segment count.

Dialogue remains structured:

- `spoken` and visible off-screen/on-screen dialogue may request lip sync;
- `inner_monologue`, `voice_over`, and `memory_voice` explicitly do not;
- speakers are included as character seeds even when their voice is heard from
  memory and they are not visually present;
- timing is deterministically allocated inside the ten-second segment;
- dense dialogue is retained but marked for manual review rather than dropped.

The source asset catalogue creates complete CharacterSeed, LocationSeed, and
PropSeed objects. P02 retains the two-paint constraint. P05 receives a different
continuity suffix in E02 and E03 so the first-day and next-day grapes cannot be
mistaken for one persistent object.

## Provider GenerationPlans

### MiniMax H3

- route: `minimax/h3-reference-av`
- five ten-second segments per episode
- character, every referenced location, and prop master images
- native Japanese audio for segments containing dialogue
- exact authoritative USD estimate from the pinned H3 price snapshot
- at most nine reference images, validated by the existing adapter

### Wan 2.7

- route: `wan/i2v`
- five ten-second segments per episode
- one pending approved first frame per segment
- character/location/prop masters retained in the AssetBundle for first-frame
  creation and review
- external Qwen TTS for structured dialogue
- CNY image and video estimates plus explicit unknown TTS components

### Seedance official platform

- route: `seedance/platform`
- five independent manual ten-second operations per episode
- native dialogue prompt retained
- no browser automation
- unknown provider cost remains explicit
- returned MP4 must later be imported as an approved `SegmentArtifact`

All routes preserve the same familiar segment IDs:

```text
E01-G01 ... E01-G05
E02-G01 ... E02-G05
E03-G01 ... E03-G05
```

No source segment is silently regrouped or renumbered.

## Continuity

Continuity groups use the series ID and primary location. Asset requirements are
hash-stable and route-specific. Each contract contains:

- character appearance locks from the reviewed catalogue;
- location prompt lock;
- prop prompt plus episode-specific state rules;
- reference slots and segment coverage;
- global visual style and period lighting.

A segment may hold a secondary location reference, such as the brief S05 port
memory inside E01-G01, while keeping S01 as the primary continuity location.
This is reported rather than silently collapsing the locations.

## CLI

```bash
python -m src.apps.jp_drama.workflows.import_series_production_plan \
  --series-plan /path/to/一房の葡萄_generation_plan.yaml \
  --asset-catalog /path/to/一房の葡萄_asset_catalog.yaml \
  --output-dir output/one-bunch-series \
  --live-provider-config examples/jp_drama/dashscope_live_providers.json \
  --minimax-h3-config examples/jp_drama/minimax_h3_live_provider.json \
  --source-commit 3070100f6ff25a3994a749b240f423f703cd294f \
  --require-route-ready
```

`--overwrite` is required to replace existing artifacts. This prevents a later
source edit from silently overwriting a previously approved production plan.

## CI evidence

The workflow checks out the exact pinned Storyboard commit and proves:

- real source YAML parses under strict Pydantic contracts;
- 3 episodes, 15 segments, 24 FPS, 1200 frames per episode;
- every C/S/P and dialogue speaker resolves;
- PreparedEpisode keeps series ID and episode number;
- H3, Wan, and Seedance each retain five source segments per episode;
- all three routes are planning and route ready;
- every route emits a pending AssetBundle;
- P02 remains exactly two paints;
- E02 and E03 P05 prompts are distinct;
- C03 memory voice in E03-G01 remains non-lip-sync and non-visual;
- no credentials, images, videos, TTS, or provider calls are used.

## Next

The next production PR should make approved assets operational rather than only
pending:

1. publish or sign approved image bytes for H3 and produce its HTTPS manifest;
2. generate Wan first frames using the approved character/location/prop masters,
   or fail closed when the selected image model cannot accept them;
3. add hash-bound MP4 import for manual Seedance output and continuation video;
4. normalize every live or manual result to the PR22 `SegmentArtifact` contract.
