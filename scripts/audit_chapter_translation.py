#!/usr/bin/env python3
"""Audit an assembled Simplified Chinese chapter for hard consistency gates."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path


FULL_TRANSLATION_TYPES = {
    "body",
    "chapter_title",
    "endnote_commentary",
    "endnote_quotation",
    "footnote",
}
TERM_RULES = (
    (re.compile(r"\bAqua Regia\b", re.I), ("王水",), "Aqua Regia"),
    (re.compile(r"\b(?:alchemist|alchemists|alchemy)\b", re.I), ("炼金",), "alchemy"),
    (
        re.compile(r"\bartificial superintelligence\b", re.I),
        ("超级人工智能",),
        "artificial superintelligence",
    ),
    (
        re.compile(r"\bmachine superintelligence\b", re.I),
        ("机器超级智能", "超级智能机器"),
        "machine superintelligence",
    ),
    (re.compile(r"\bASI\b"), ("ASI",), "ASI"),
    (re.compile(r"\b(?:Yann )?LeCun\b", re.I), ("杨立昆",), "Yann LeCun"),
    (re.compile(r"\bMusk\b", re.I), ("马斯克",), "Musk"),
    (re.compile(r"\bChernobyl\b", re.I), ("切尔诺贝利",), "Chernobyl"),
    (re.compile(r"\bTruthGPT\b"), ("TruthGPT",), "TruthGPT"),
)
TRADITIONAL_WARNING_CHARS = set(
    "鍊術鐳車諾楊圖靈學習風險關閉體現與為這會還個們"
)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def audit(args: argparse.Namespace) -> int:
    report = read_json(args.assembly / "report.json")
    rows = read_jsonl(args.assembly / "chapter_units.jsonl")
    hard_issues = []
    warnings = []
    if not report.get("complete"):
        hard_issues.append(
            {
                "kind": "incomplete_chapter",
                "missing_unit_ids": report.get("missing_unit_ids", []),
            }
        )
    if len(rows) != report["expected_units"]:
        hard_issues.append(
            {
                "kind": "unit_count_mismatch",
                "actual": len(rows),
                "expected": report["expected_units"],
            }
        )

    footnotes = {
        row["footnote_reference"]["target_id"]: row
        for row in rows
        if row.get("footnote_reference")
    }
    markdown = (args.assembly / "chapter_zhHans.md").read_text(encoding="utf-8")
    forward_links = 0
    backlinks = 0
    unresolved_links = 0
    reference_ids = []
    markup_metadata_units = 0
    quotation_markup_failures = 0

    for row in rows:
        translation = row["translation"]
        if not row.get("source_html"):
            hard_issues.append(
                {"kind": "source_html_metadata_missing", "unit_id": row["unit_id"]}
            )
        markup = row.get("markup_requirements")
        if markup is None:
            hard_issues.append(
                {"kind": "markup_metadata_missing", "unit_id": row["unit_id"]}
            )
        elif markup["has_inline_markup"]:
            markup_metadata_units += 1

        for reference in row.get("inline_references", []):
            forward_links += 1
            reference_ids.append(reference["anchor_id"])
            target_id = reference["href"].rsplit("#", 1)[-1]
            footnote = footnotes.get(target_id)
            if footnote is None:
                unresolved_links += 1
                hard_issues.append(
                    {
                        "kind": "footnote_target_missing",
                        "target_id": target_id,
                        "unit_id": row["unit_id"],
                    }
                )
                continue
            reference_ids.append(target_id)
            backlink_id = footnote["footnote_reference"]["backlink_href"].rsplit(
                "#", 1
            )[-1]
            if backlink_id != reference["anchor_id"]:
                unresolved_links += 1
                hard_issues.append(
                    {
                        "actual_backlink": backlink_id,
                        "expected_backlink": reference["anchor_id"],
                        "kind": "footnote_backlink_mismatch",
                        "unit_id": footnote["unit_id"],
                    }
                )
            else:
                backlinks += 1

        if row["content_type"] == "endnote_quotation":
            if f"*{translation}*" not in markdown:
                quotation_markup_failures += 1
                hard_issues.append(
                    {
                        "kind": "quotation_review_markup_missing",
                        "unit_id": row["unit_id"],
                    }
                )

        if row["content_type"] in FULL_TRANSLATION_TYPES:
            for pattern, required, label in TERM_RULES:
                if pattern.search(row["source"]) and not any(
                    target in translation for target in required
                ):
                    hard_issues.append(
                        {
                            "kind": "required_term_missing",
                            "term": label,
                            "unit_id": row["unit_id"],
                        }
                    )
        traditional = sorted(set(translation) & TRADITIONAL_WARNING_CHARS)
        if traditional:
            warnings.append(
                {
                    "characters": traditional,
                    "kind": "possible_traditional_script",
                    "unit_id": row["unit_id"],
                }
            )

    duplicate_ids = sorted(
        value for value, count in Counter(reference_ids).items() if count > 1
    )
    if duplicate_ids:
        hard_issues.append(
            {"ids": duplicate_ids, "kind": "duplicate_reference_ids"}
        )
    if (
        any("H. pylori" in row["source"] for row in rows)
        and "*H. pylori*" not in markdown
    ):
        hard_issues.append(
            {
                "kind": "scientific_name_review_markup_missing",
                "unit_id": "book_ch11.footnote.ii",
            }
        )

    duplicate_texts = {
        text: unit_ids
        for text, unit_ids in _translation_index(rows).items()
        if len(unit_ids) > 1 and len(text) > 20
    }
    if duplicate_texts:
        warnings.append(
            {
                "kind": "duplicate_translation_text",
                "units": list(duplicate_texts.values()),
            }
        )

    result = {
        "chapter": report["chapter"],
        "complete": not hard_issues,
        "content_type_counts": dict(
            sorted(Counter(row["content_type"] for row in rows).items())
        ),
        "hard_issue_count": len(hard_issues),
        "hard_issues": hard_issues,
        "backlinks": backlinks,
        "duplicate_ids": len(duplicate_ids),
        "forward_links": forward_links,
        "markup_metadata_units": markup_metadata_units,
        "quotation_markup_failures": quotation_markup_failures,
        "unresolved_links": unresolved_links,
        "unit_count": len(rows),
        "warning_count": len(warnings),
        "warnings": warnings,
    }
    (args.assembly / "audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        f"# Chapter {report['chapter']} translation audit",
        "",
        f"- Units: {len(rows)}/{report['expected_units']}",
        f"- Hard issues: {len(hard_issues)}",
        f"- Warnings: {len(warnings)}",
        f"- Forward footnote links: {forward_links}",
        f"- Footnote backlinks: {backlinks}",
        f"- Unresolved links: {unresolved_links}",
        f"- Duplicate reference IDs: {len(duplicate_ids)}",
        f"- Quotation markup failures: {quotation_markup_failures}",
        f"- Units carrying inline-markup metadata: {markup_metadata_units}",
        "",
        "## Hard issues",
        "",
    ]
    lines.extend(
        ["- None"] if not hard_issues else [f"- `{item}`" for item in hard_issues]
    )
    lines.extend(["", "## Warnings", ""])
    lines.extend(["- None"] if not warnings else [f"- `{item}`" for item in warnings])
    (args.assembly / "audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"Audited chapter {report['chapter']}: units={len(rows)}, "
        f"hard={len(hard_issues)}, warnings={len(warnings)}"
    )
    return 0 if not hard_issues else 1


def _translation_index(rows: list[dict]) -> dict[str, list[str]]:
    result = {}
    for row in rows:
        result.setdefault(row["translation"], []).append(row["unit_id"])
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assembly", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    try:
        return audit(parse_args())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
