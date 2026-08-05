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

    approval_pattern = re.compile(
        r"(?m)^(?P<indent>\s*)max_cost_usd=(?P<value>[^\n]+),\n"
        r"(?P=indent)output_path="
    )
    text, approval_replacements = approval_pattern.subn(
        lambda match: (
            f"{match.group('indent')}max_cost_usd={match.group('value')},\n"
            f"{match.group('indent')}approval_verified=True,\n"
            f"{match.group('indent')}output_path="
        ),
        text,
    )
    if approval_replacements < 1:
        raise RuntimeError("legacy H3 executor approval tests were not migrated")

    ledger_needle = (
        '        estimated_cost_usd=Decimal("0.64"),\n'
        '        max_cost_usd=Decimal("1.00"),\n'
        '    )\n'
        '    executor.ledger_store.write(record)'
    )
    ledger_replacement = (
        '        estimated_cost_usd=Decimal("0.64"),\n'
        '        max_cost_usd=Decimal("1.00"),\n'
        '        price_snapshot_id="minimax-h3-2026-08-05",\n'
        '    )\n'
        '    executor.ledger_store.write(record)'
    )
    if ledger_needle not in text:
        raise RuntimeError("legacy submitting-ledger test shape changed")
    text = text.replace(ledger_needle, ledger_replacement, 1)

    budget_needle = (
        '            estimated_cost_usd=Decimal("1.01"),\n'
        '            max_cost_usd=Decimal("1.00"),'
    )
    budget_replacement = (
        '            estimated_cost_usd=Decimal("0.01"),\n'
        '            max_cost_usd=Decimal("0.63"),'
    )
    if budget_needle not in text:
        raise RuntimeError("legacy caller-supplied budget test shape changed")
    text = text.replace(budget_needle, budget_replacement, 1)

    legacy_test.write_text(text, encoding="utf-8")

    for name in PART_NAMES:
        (parts_dir / name).unlink()
    parts_dir.rmdir()
    (root / "tools/apply_h3_safety_fix.py").unlink()
    (root / ".github/workflows/apply-h3-safety-fix.yml").unlink()


if __name__ == "__main__":
    main()
