"""Parser for the upstream Seedance2 Storyboard Generator Markdown outputs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .models import (
    SeedanceStoryboardEpisode,
    SeedanceStoryboardPackage,
    StoryboardAsset,
    StoryboardImportIssue,
    TimelineBeat,
    UploadSlot,
    UpstreamProvenance,
)


UPSTREAM_COMMIT = "17b9ca6dfac3e4a086a2874791ef19ae5aae3932"


class SeedanceStoryboardParseError(ValueError):
    """An upstream Markdown artifact cannot be parsed without guessing."""


_ASSET_HEADING = re.compile(
    r"^###\s+(?P<id>[CSP][0-9]{2,3}(?:[._-][A-Za-z0-9]+)?)\s*[—–-]\s*(?P<name>.+?)\s*$",
    re.MULTILINE,
)
_TIMELINE_START = re.compile(
    r"^\s*(?:\*\*)?(?P<start>[0-9]+(?:\.[0-9]+)?)\s*[-—~至]\s*"
    r"(?P<end>[0-9]+(?:\.[0-9]+)?)\s*秒(?:画面)?[：:]?(?:\*\*)?\s*(?P<rest>.*)$"
)
_CONTINUATION = re.compile(
    r"将\s*@(?P<source>视频[0-9]+)\s*延长\s*(?P<seconds>[0-9]+(?:\.[0-9]+)?)\s*(?:s|秒)",
    re.IGNORECASE,
)
_REFERENCE_TOKEN = re.compile(r"@(?P<slot>图片[0-9]+)")
_EPISODE_HEADING = re.compile(
    r"^#\s*(?P<episode>E[0-9]{1,3})\s*[-—:]\s*(?P<title>.+?)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ProjectMarkdown:
    project_title: str
    asset_markdown: str
    storyboards: tuple[str, ...]


def _normalise(markdown: str) -> str:
    return markdown.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")


def _section(markdown: str, heading_pattern: str, *, required: bool = True) -> str:
    match = re.search(heading_pattern, markdown, flags=re.IGNORECASE | re.MULTILINE)
    if match is None:
        if required:
            raise SeedanceStoryboardParseError(
                f"required Markdown section not found: {heading_pattern}"
            )
        return ""
    start = match.end()
    next_heading = re.search(r"^##\s+", markdown[start:], flags=re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(markdown)
    return markdown[start:end].strip("\n -")


def _strip_code_fence(text: str) -> str:
    code = re.search(r"```(?:[^\n]*)\n(?P<body>.*?)```", text, flags=re.DOTALL)
    if code:
        return code.group("body").strip()
    return text.strip()


def parse_asset_catalog(markdown: str) -> list[StoryboardAsset]:
    """Parse C/S/P assets from an upstream ``*_素材清单.md`` document."""
    text = _normalise(markdown)
    matches = list(_ASSET_HEADING.finditer(text))
    if not matches:
        raise SeedanceStoryboardParseError("asset catalog contains no C/S/P headings")

    assets: list[StoryboardAsset] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        body = re.split(
            r"^##\s+(?:素材编号总览|使用说明|编号总览)",
            body,
            maxsplit=1,
            flags=re.MULTILINE,
        )[0].strip()
        prompt = _strip_code_fence(body)
        prompt = re.sub(
            r"^\*\*(?:用途|故事功能|场景用途|道具用途)[:：]\*\*\s*",
            "",
            prompt,
        )
        if not prompt:
            raise SeedanceStoryboardParseError(
                f"asset {match.group('id')} has no generation prompt"
            )
        prefix = match.group("id")[0]
        kind = {"C": "character", "S": "scene", "P": "prop"}[prefix]
        assets.append(
            StoryboardAsset(
                asset_id=match.group("id"),
                kind=kind,
                name=match.group("name").strip(),
                prompt=prompt,
            )
        )
    return assets


def _parse_upload_slots(section: str) -> list[UploadSlot]:
    slots: list[UploadSlot] = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        if any(marker in cells[0] for marker in ("素材槽", "上传位置", "---")):
            continue
        if set(cells[0]) <= {"-", ":"}:
            continue
        asset_match = re.search(
            r"\b([CSP][0-9]{2,3}(?:[._-][A-Za-z0-9]+)?)\b",
            cells[1],
        )
        if asset_match is None:
            raise SeedanceStoryboardParseError(
                f"upload slot {cells[0]!r} has no C/S/P asset ID"
            )
        slots.append(
            UploadSlot(
                slot_name=cells[0],
                asset_id=asset_match.group(1),
                description=cells[2],
            )
        )
    if not slots:
        raise SeedanceStoryboardParseError("storyboard has no upload slots")
    return slots


def _parse_prompt(
    prompt_section: str,
) -> tuple[
    str,
    list[TimelineBeat],
    str | None,
    str | None,
    str | None,
    Decimal | None,
]:
    lines = prompt_section.splitlines()
    continuation_source: str | None = None
    continuation_seconds: Decimal | None = None
    continuation_match = _CONTINUATION.search(prompt_section)
    if continuation_match:
        continuation_source = continuation_match.group("source")
        continuation_seconds = Decimal(continuation_match.group("seconds"))

    timeline_starts: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        match = _TIMELINE_START.match(line)
        if match:
            timeline_starts.append((index, match))
    if not timeline_starts:
        raise SeedanceStoryboardParseError(
            "Seedance Prompt contains no timed storyboard beats"
        )

    style_lines = []
    first_timeline_line = timeline_starts[0][0]
    for line in lines[:first_timeline_line]:
        stripped = line.strip()
        if not stripped or _CONTINUATION.search(stripped):
            continue
        if stripped.startswith(("【", "〖")):
            continue
        style_lines.append(stripped)
    style_prompt = "\n".join(style_lines).strip()
    if not style_prompt:
        raise SeedanceStoryboardParseError(
            "Seedance Prompt has no style description"
        )

    timeline: list[TimelineBeat] = []
    for order, (line_index, match) in enumerate(timeline_starts, start=1):
        next_index = (
            timeline_starts[order][0]
            if order < len(timeline_starts)
            else len(lines)
        )
        body_lines: list[str] = []
        rest = match.group("rest").strip()
        if rest:
            body_lines.append(rest)
        for candidate in lines[line_index + 1:next_index]:
            stripped = candidate.strip()
            if re.match(r"^[【〖](?:声音|参考)[】〗]", stripped):
                break
            if stripped and stripped != "---":
                body_lines.append(stripped)
        text = "\n".join(body_lines).strip()
        if not text:
            raise SeedanceStoryboardParseError(
                f"timeline {match.group('start')}-{match.group('end')} has no description"
            )
        timeline.append(
            TimelineBeat(
                order=order,
                start_seconds=Decimal(match.group("start")),
                end_seconds=Decimal(match.group("end")),
                text=text,
            )
        )

    sound_match = re.search(
        r"^[【〖]声音[】〗]\s*(?P<body>.+?)\s*$",
        prompt_section,
        flags=re.MULTILINE,
    )
    reference_match = re.search(
        r"^[【〖]参考[】〗]\s*(?P<body>.+?)\s*$",
        prompt_section,
        flags=re.MULTILINE,
    )
    return (
        style_prompt,
        timeline,
        sound_match.group("body").strip() if sound_match else None,
        reference_match.group("body").strip() if reference_match else None,
        continuation_source,
        continuation_seconds,
    )


def parse_storyboard(markdown: str) -> SeedanceStoryboardEpisode:
    """Parse one upstream ``*_E01_分镜.md`` document."""
    text = _normalise(markdown)
    heading = _EPISODE_HEADING.search(text)
    if heading is None:
        raise SeedanceStoryboardParseError(
            "storyboard heading must be '# E01 - title'"
        )

    upload_section = _section(
        text,
        r"^##\s+(?:素材上传清单|素材上传列表)\s*$",
    )
    prompt_section = _section(text, r"^##\s+Seedance\s+Prompt\s*$")
    ending_frame = _section(
        text,
        r"^##\s+(?:尾帧描述|Ending\s+Frame\s+Description)\s*$",
    )
    slots = _parse_upload_slots(upload_section)
    (
        style_prompt,
        timeline,
        sound_prompt,
        reference_prompt,
        continuation_source,
        continuation_seconds,
    ) = _parse_prompt(prompt_section)

    known_slot_names = {item.slot_name for item in slots}
    referenced_slot_names = set(_REFERENCE_TOKEN.findall(prompt_section))
    unknown_slots = sorted(referenced_slot_names - known_slot_names)
    if unknown_slots:
        raise SeedanceStoryboardParseError(
            f"reference prompt uses undefined upload slots: {unknown_slots}"
        )

    return SeedanceStoryboardEpisode(
        episode_id=heading.group("episode"),
        title=heading.group("title").strip(),
        style_prompt=style_prompt,
        upload_slots=slots,
        timeline=timeline,
        sound_prompt=sound_prompt,
        reference_prompt=reference_prompt,
        ending_frame=ending_frame,
        continuation_source=continuation_source,
        continuation_seconds=continuation_seconds,
        raw_prompt=prompt_section,
    )


def parse_project(project: ProjectMarkdown) -> SeedanceStoryboardPackage:
    assets = parse_asset_catalog(project.asset_markdown)
    episodes = sorted(
        (parse_storyboard(item) for item in project.storyboards),
        key=lambda episode: int(episode.episode_id[1:]),
    )
    used_by_asset: dict[str, list[str]] = {
        item.asset_id: [] for item in assets
    }
    for episode in episodes:
        for slot in episode.upload_slots:
            used_by_asset.setdefault(slot.asset_id, []).append(episode.episode_id)
    assets = [
        asset.model_copy(
            update={
                "used_in_episode_ids": sorted(
                    set(used_by_asset.get(asset.asset_id, [])),
                    key=lambda value: int(value[1:]),
                )
            }
        )
        for asset in assets
    ]

    warnings: list[StoryboardImportIssue] = []
    unused = [item.asset_id for item in assets if not item.used_in_episode_ids]
    for asset_id in unused:
        warnings.append(
            StoryboardImportIssue(
                code="asset_not_used_by_imported_storyboards",
                severity="warning",
                message=(
                    "asset exists in the catalogue but is not used by imported episodes"
                ),
                asset_id=asset_id,
            )
        )

    return SeedanceStoryboardPackage.build_with_digest(
        project_title=project.project_title,
        provenance=UpstreamProvenance(commit=UPSTREAM_COMMIT),
        assets=assets,
        episodes=episodes,
        warnings=warnings,
    )


def load_project_directory(path: str | Path) -> ProjectMarkdown:
    """Load one generated upstream project directory without choosing duplicates."""
    root = Path(path).resolve()
    if not root.is_dir():
        raise SeedanceStoryboardParseError(
            f"project directory does not exist: {root}"
        )

    asset_candidates = sorted(root.glob("*素材清单*.md"))
    if len(asset_candidates) != 1:
        raise SeedanceStoryboardParseError(
            "project directory must contain exactly one '*素材清单*.md'; "
            f"found {[item.name for item in asset_candidates]}"
        )
    storyboard_candidates = sorted(root.glob("*_E[0-9][0-9]*_分镜.md"))
    if not storyboard_candidates:
        raise SeedanceStoryboardParseError(
            "project directory contains no per-episode '*_E01_分镜.md' files"
        )

    stem = asset_candidates[0].stem
    project_title = re.sub(r"_素材清单.*$", "", stem)
    return ProjectMarkdown(
        project_title=project_title,
        asset_markdown=asset_candidates[0].read_text(encoding="utf-8"),
        storyboards=tuple(
            item.read_text(encoding="utf-8")
            for item in storyboard_candidates
        ),
    )


def write_import_artifacts(
    package: SeedanceStoryboardPackage,
    output_dir: str | Path,
) -> list[Path]:
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    output = root / "seedance_storyboard_package.json"
    output.write_text(package.to_canonical_json() + "\n", encoding="utf-8")

    operator_manifest = root / "seedance_operator_manifest.md"
    parts = [f"# {package.project_title} — Seedance Operator Manifest", ""]
    for episode in package.episodes:
        parts.extend(
            [
                f"## {episode.episode_id} — {episode.title}",
                "",
                "### Upload slots",
                "",
                "| Slot | Asset | Purpose |",
                "|---|---|---|",
                *[
                    f"| {slot.slot_name} | {slot.asset_id} | {slot.description} |"
                    for slot in episode.upload_slots
                ],
                "",
                "### Prompt",
                "",
                episode.raw_prompt,
                "",
                "### Ending frame",
                "",
                episode.ending_frame,
                "",
            ]
        )
    operator_manifest.write_text(
        "\n".join(parts).rstrip() + "\n",
        encoding="utf-8",
    )
    return [output, operator_manifest]
