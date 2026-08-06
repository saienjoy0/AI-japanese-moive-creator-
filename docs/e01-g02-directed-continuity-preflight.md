# E01-G02 Directed HappyHorse Continuity Preflight

## Purpose

Correct the reviewed `E01-G02` HappyHorse R2V request without changing the
storyboard, GenerationPlan, approved master images, G1 MP4, provider ledger, or
paid-call safety boundary.

The correction addresses two prompt conflicts:

1. `E01-G02` is tagged as `inner_monologue` with `lip_sync_required=false`, but
   the generic HappyHorse prompt requested visible mouth synchronization.
2. The G1 end frame is a continuity anchor for the beginning of G2, but the
   previous wording could be interpreted as freezing the same pose and framing
   for all ten seconds.

## External practice used

The implementation follows provider documentation rather than copying an
unverified community prompt:

- MiniMax image-to-video defines the supplied image as the **first frame** and
  says the text prompt should describe how the scene evolves from it:
  <https://platform.minimax.io/docs/guides/video-generation>
- MiniMax's image-to-video API likewise exposes `first_frame_image` as the
  starting frame:
  <https://platform.minimax.io/docs/api-reference/video-generation-i2v>
- Runway's official image-to-video guide treats the image as composition/style
  input and recommends describing camera motion and subject action while
  reducing ambiguity:
  <https://help.runwayml.com/hc/en-us/articles/48324313115155-Image-to-Video-Prompting-Guide>
- Alibaba's speech-to-video documentation treats lip synchronization as an
  audio-driven speaking/singing/performance operation. An internal monologue is
  therefore not sent as visible lip-sync direction:
  <https://www.alibabacloud.com/help/en/model-studio/wan-s2v-api>

## Corrected G2 contract

### References, in order

1. `C01` — protagonist identity
2. `C02` — Jim identity
3. `S01` — classroom
4. `P01` — imported wooden twelve-color paint box
5. `P02` — exactly two solid paint cakes: indigo and magenta
6. `E01-G01_end.png` — SHA-verified immediately previous final frame

### Dialogue

Exact inner voice-over:

```text
あの二色さえあれば……
```

The visible protagonist does not speak aloud. His mouth remains closed and is
not lip-synchronized.

### Ten-second progression

```text
0.0-1.5s  Start from G1's final frame; C01 is already looking toward the next desk.
1.5-4.0s  Reveal C02's hands and P01 from the established gaze direction.
4.0-7.0s  C02 opens P01; exactly two P02 colors remain inside the box.
7.0-10.0s Hold C01's restrained fixation with subtle rack focus/minimal reframing.
```

G2 must not show C01 touching, removing, or stealing the paints. That action
belongs to G3. The shot must not return to the Yokohama harbor memory.

## Implementation

- `render_happyhorse_segment_directed_continuity_canary.py`
  - rewrites tagged inner monologue to non-diegetic voice-over;
  - fails closed if the generic lip-sync template changes unexpectedly;
  - treats the previous frame as an opening-state anchor rather than a frozen
    whole-clip composition;
  - adds the reviewed G2 progression without altering other segment scripts.
- `test_jp_drama_directed_continuity_prompt.py`
  - locks mouth-closed inner voice-over behavior;
  - locks the two-paint count and no-theft-before-G3 rule;
  - locks motion-first continuity and the nine-image limit.
- `jp-drama-e01-g02-happyhorse-directed-preflight.yml`
  - downloads the sealed episode inputs and verified G1 continuity artifact;
  - creates a zero-call exact G2 preflight report;
  - verifies six references, ten seconds, 9:16, no visible lip-sync request, and
    no provider submission.

After this branch is merged, the owner-only Issue #37 command is:

```text
PREFLIGHT_ONE_BUNCH_E01_G02_HAPPYHORSE
```

This command performs preflight only. It does not receive provider credentials
and makes zero paid video-generation calls.
