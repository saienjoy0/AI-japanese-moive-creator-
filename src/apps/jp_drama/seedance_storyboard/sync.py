"""Fetch the pinned upstream Skill snapshot with git-blob verification.

The upstream repository does not publish a conventional OSS licence. The
files are therefore fetched into a local, ignored working directory rather
than copied into this repository. Provenance and the exact commit remain
machine-readable.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.request
from pathlib import Path


UPSTREAM_REPOSITORY = "liangdabiao/Seedance2-Storyboard-Generator"
UPSTREAM_COMMIT = "17b9ca6dfac3e4a086a2874791ef19ae5aae3932"
UPSTREAM_FILES = {
    ".claude/skills/seedance-storyboard-generator/SKILL.md": (
        "8cc6be2f9bf4b9e11979209317c5c5bb4cbdf3f8"
    ),
    ".claude/skills/seedance-storyboard-generator/references/seedance-manual.md": (
        "870ce477480129aba308f395f7e5a52affa49c4f"
    ),
    ".claude/skills/seedance-storyboard-generator/references/优化分镜.md": (
        "9373bea652c173757287cb7c6ea0a5397dd939aa"
    ),
    ".claude/skills/seedance-storyboard-generator/references/分镜优化与声音设计.md": (
        "8dc6fe4cf5cedb2732b640fee9022ca2c91a1803"
    ),
    ".claude/skills/seedance-storyboard-generator/references/好剧本.md": (
        "b896a032e70128a7ecb096c04e0f135648403a79"
    ),
    ".claude/skills/seedance-storyboard-generator/references/故事转视频脚本-转换工具.md": (
        "9e2fa9b3d858415106a3030afcacb62aa84bac13"
    ),
}


class UpstreamSyncError(RuntimeError):
    """The pinned upstream snapshot cannot be fetched or verified."""


def git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def sync_upstream(
    destination: str | Path,
    *,
    timeout_seconds: int = 30,
) -> Path:
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    completed: dict[str, dict[str, object]] = {}

    for relative_path, expected_blob_sha in UPSTREAM_FILES.items():
        url = (
            f"https://raw.githubusercontent.com/{UPSTREAM_REPOSITORY}/"
            f"{UPSTREAM_COMMIT}/{relative_path}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "ai-japanese-movie-creator-upstream-sync/1.0"
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_seconds,
            ) as response:
                content = response.read()
        except Exception as exc:  # pragma: no cover - network boundary
            raise UpstreamSyncError(
                f"failed to fetch {relative_path}: {exc}"
            ) from exc

        actual_blob_sha = git_blob_sha(content)
        if actual_blob_sha != expected_blob_sha:
            raise UpstreamSyncError(
                f"git blob SHA mismatch for {relative_path}: "
                f"expected {expected_blob_sha}, got {actual_blob_sha}"
            )

        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=target.name + ".",
            suffix=".part",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(target)
        completed[relative_path] = {
            "git_blob_sha": actual_blob_sha,
            "size_bytes": len(content),
        }

    manifest = root / "UPSTREAM_MANIFEST.json"
    manifest.write_text(
        json.dumps(
            {
                "repository": UPSTREAM_REPOSITORY,
                "commit": UPSTREAM_COMMIT,
                "usage_note": (
                    "Upstream README says content is for learning and reference; "
                    "preserve attribution and verify redistribution rights."
                ),
                "files": completed,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest
