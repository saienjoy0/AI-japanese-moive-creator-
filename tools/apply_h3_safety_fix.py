"""One-shot repository migration for PR #15 H3 safety files."""
from __future__ import annotations

import base64
import io
import re
import tarfile
from pathlib import Path

PART_NAMES = [f"part{index:02d}.txt" for index in range(7)]
ALLOWED_PREFIXES = (
    ".github/workflows/",
    "docs/",
    "examples/",
    "src/",
    "tests/",
)


def main() -> None:
    root = Path.cwd().resolve()
    parts_dir = root / "tools/h3_fix_parts"
    encoded = "".join((parts_dir / name).read_text(encoding="ascii") for name in PART_NAMES)
    payload = base64.b64decode(encoded, validate=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            name = member.name.removeprefix("./")
            if not name or member.isdir():
                continue
            if not member.isfile() or not name.startswith(ALLOWED_PREFIXES):
                raise RuntimeError(f"unsafe archive member: {member.name}")
            target = (root / name).resolve()
            if root not in target.parents:
                raise RuntimeError(f"path traversal rejected: {member.name}")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"cannot read archive member: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())

    legacy_test = root / "tests/test_jp_drama_minimax_h3_core.py"
    text = legacy_test.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(?m)^(?P<indent>\s*)max_cost_usd=(?P<value>[^\n]+),\n"
        r"(?P=indent)output_path="
    )
    text, replacements = pattern.subn(
        lambda match: (
            f"{match.group('indent')}max_cost_usd={match.group('value')},\n"
            f"{match.group('indent')}approval_verified=True,\n"
            f"{match.group('indent')}output_path="
        ),
        text,
    )
    if replacements < 1:
        raise RuntimeError("legacy H3 executor tests were not migrated")
    legacy_test.write_text(text, encoding="utf-8")

    for name in PART_NAMES:
        (parts_dir / name).unlink()
    parts_dir.rmdir()
    (root / "tools/apply_h3_safety_fix.py").unlink()
    (root / ".github/workflows/apply-h3-safety-fix.yml").unlink()


if __name__ == "__main__":
    main()
