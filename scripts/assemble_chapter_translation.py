#!/usr/bin/env python3
"""Assemble and validate chapter translation drafts into review manuscripts."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path


STRUCTURAL_TRANSLATIONS = {
    "chapter_number": lambda chapter, source: f"第{chapter_to_chinese(chapter)}章",
    "url_marker": lambda chapter, source: source,
    "footnotes_heading": lambda chapter, source: "脚注",
    "endnotes_section_heading": lambda chapter, source: (
        f"第{chapter_to_chinese(chapter)}章：这不是科学，而是炼金术"
        if chapter == 11
        else source
    ),
}


def chapter_to_chinese(chapter: int) -> str:
    numerals = {
        1: "一",
        2: "二",
        3: "三",
        4: "四",
        5: "五",
        6: "六",
        7: "七",
        8: "八",
        9: "九",
        10: "十",
        11: "十一",
        12: "十二",
        13: "十三",
    }
    return numerals.get(chapter, str(chapter))


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def collect_drafts(drafts_root: Path, chapter: int) -> dict[str, dict]:
    pattern = f"translation_pilot_ch{chapter}_batch*/stages/editorial_draft_v2.json"
    collected = {}
    for path in sorted(drafts_root.glob(pattern)):
        for row in read_json(path):
            unit_id = row.get("unit_id")
            if not unit_id:
                raise ValueError(f"missing unit_id in {path}")
            if unit_id in collected:
                raise ValueError(f"duplicate translation draft: {unit_id}")
            collected[unit_id] = {**row, "draft_path": str(path)}
    return collected


def collect_corrections(path: Path | None) -> dict[str, dict]:
    if path is None or not path.exists():
        return {}
    collected = {}
    for row in read_jsonl(path):
        unit_id = row.get("unit_id")
        if not unit_id:
            raise ValueError(f"missing unit_id in {path}")
        if unit_id in collected:
            raise ValueError(f"duplicate chapter correction: {unit_id}")
        if not row.get("after", "").strip():
            raise ValueError(f"empty corrected translation: {unit_id}")
        if not row.get("reason", "").strip():
            raise ValueError(f"missing correction reason: {unit_id}")
        collected[unit_id] = row
    return collected


def structural_translation(case: dict, chapter: int) -> str | None:
    handler = STRUCTURAL_TRANSLATIONS.get(case["content_type"])
    if handler:
        return handler(chapter, case["source_text"])
    return None


def validate_preserved_segments(
    segments: list[dict], unit_id: str, translation: str
) -> None:
    for segment in segments:
        if segment["mode"] != "preserve_exact":
            continue
        if segment["text"] not in translation:
            raise ValueError(
                f"preserved segment absent at {unit_id}: {segment['segment_id']}"
            )


def source_markup_requirements(source_html: str) -> dict:
    emphasis_texts = []
    for match in re.findall(r"<html:em>(.*?)</html:em>", source_html, flags=re.DOTALL):
        plain_text = re.sub(r"<[^>]+>", "", match)
        emphasis_texts.append(html.unescape(plain_text))
    container_class = re.search(r'<html:[^ >]+[^>]*\bclass="([^"]+)"', source_html)
    whole_unit_emphasis = bool(
        re.search(
            r"<html:p[^>]*>\s*<html:em>.*</html:em>\s*</html:p>",
            source_html,
            flags=re.DOTALL,
        )
    )
    return {
        "container_class": container_class.group(1) if container_class else None,
        "emphasis_source_texts": emphasis_texts,
        "has_inline_markup": bool(
            re.search(r"<html:(?:a|em|span|strong|sup)\b", source_html)
        ),
        "whole_unit_emphasis": whole_unit_emphasis,
    }


def footnote_reference_metadata(case: dict) -> dict | None:
    if case["content_type"] != "footnote":
        return None
    source_html = case["source_html"]
    target_id = re.search(r'<html:p[^>]*\bid="([^"]+)"', source_html)
    backlink = re.search(
        r'<html:a[^>]*\bhref="([^"]+)"[^>]*\brole="doc-backlink"', source_html
    )
    label = re.search(r"<html:a[^>]*>([^<]+)</html:a>", source_html)
    if not (target_id and backlink and label):
        raise ValueError(f"incomplete footnote reference metadata: {case['unit_id']}")
    return {
        "backlink_href": backlink.group(1),
        "label": label.group(1),
        "target_id": target_id.group(1),
    }


def superscript_marker(marker: str) -> str:
    return marker.translate(str.maketrans("iIvVxX", "ⁱᴵᵛⱽˣˣ"))


def render_bibliography_markup(row: dict, translation: str) -> str:
    if row["content_type"] != "endnote_bibliography":
        return translation
    segments = row["effective_format_segments"]
    if len(segments) < 3:
        raise ValueError(f"incomplete bibliography segments: {row['unit_id']}")
    prefix = segments[0]["text"]
    suffix = segments[-1]["text"]
    if not translation.startswith(prefix) or not translation.endswith(suffix):
        raise ValueError(f"bibliography boundary mismatch: {row['unit_id']}")
    locator_end = len(translation) - len(suffix) if suffix else len(translation)
    locator = translation[len(prefix) : locator_end]
    return f"{prefix}*{locator}*{suffix}"


def render_translation_for_review(row: dict) -> str:
    translation = render_bibliography_markup(row, row["translation"])
    if not row["markup_requirements"]["whole_unit_emphasis"]:
        translatable_source = "".join(
            segment["text"]
            for segment in row["effective_format_segments"]
            if segment["mode"] == "translate"
        )
        for emphasized_source in row["markup_requirements"][
            "emphasis_source_texts"
        ]:
            if (
                row["content_type"] == "endnote_bibliography"
                and emphasized_source.strip() in translatable_source
            ):
                continue
            if emphasized_source in translation:
                translation = translation.replace(
                    emphasized_source, f"*{emphasized_source}*"
                )
    for reference in row["inline_references"]:
        marker = superscript_marker(reference["marker"])
        target_id = reference["href"].rsplit("#", 1)[-1]
        rendered_marker = (
            f'<a id="{reference["anchor_id"]}"></a>[{marker}](#{target_id})'
        )
        if marker not in translation:
            raise ValueError(f"visible inline marker missing: {row['unit_id']}")
        translation = translation.replace(marker, rendered_marker, 1)

    footnote_reference = row.get("footnote_reference")
    if footnote_reference:
        label = footnote_reference["label"]
        backlink_id = footnote_reference["backlink_href"].rsplit("#", 1)[-1]
        rendered_label = (
            f'<a id="{footnote_reference["target_id"]}"></a>'
            f'[{label}](#{backlink_id})'
        )
        if not translation.startswith(f"{label} "):
            raise ValueError(f"footnote label missing: {row['unit_id']}")
        translation = translation.replace(label, rendered_label, 1)

    if row["markup_requirements"]["whole_unit_emphasis"]:
        translation = f"*{translation}*"
    return translation


def append_chinese_unit(lines: list[str], row: dict) -> None:
    heading_levels = {
        "chapter_number": "#",
        "chapter_title": "##",
        "footnotes_heading": "##",
        "endnotes_section_heading": "##",
    }
    prefix = heading_levels.get(row["content_type"])
    review_translation = render_translation_for_review(row)
    text = f"{prefix} {review_translation}" if prefix else review_translation
    lines.extend([text, ""])


def assemble(args: argparse.Namespace) -> int:
    corrections_path = args.corrections
    if corrections_path is None:
        default_corrections = (
            args.source_brief
            / "stages"
            / f"chapter_review_corrections_ch{args.chapter}.jsonl"
        )
        corrections_path = default_corrections if default_corrections.exists() else None
    corrections = collect_corrections(corrections_path)

    if args.output.exists():
        if not args.force:
            raise FileExistsError(f"output exists: {args.output}")
    args.output.mkdir(parents=True, exist_ok=True)

    all_cases = read_jsonl(args.source_brief / "input" / "cases.jsonl")
    cases = [row for row in all_cases if row["book_chapter"] == args.chapter]
    overrides_path = args.source_brief / "config" / "format_segment_overrides.json"
    overrides = read_json(overrides_path) if overrides_path.exists() else {}
    drafts = collect_drafts(args.drafts_root, args.chapter)
    case_ids = {row["unit_id"] for row in cases}
    unknown = set(drafts) - case_ids
    if unknown:
        raise ValueError(f"drafts contain units outside chapter: {sorted(unknown)}")
    unknown_corrections = set(corrections) - case_ids
    if unknown_corrections:
        raise ValueError(
            f"corrections contain units outside chapter: {sorted(unknown_corrections)}"
        )

    assembled = []
    missing = []
    for case in cases:
        unit_id = case["unit_id"]
        draft = drafts.get(unit_id)
        if draft:
            if draft.get("source") != case["source_text"]:
                raise ValueError(f"draft source mismatch: {unit_id}")
            translation = draft.get("translation", "").strip()
            origin = draft["draft_path"]
            status = draft.get("status", "draft")
            required_refs = {item["anchor_id"] for item in case["inline_references"]}
            supplied_refs = set(draft.get("inline_references", []))
            if not required_refs <= supplied_refs:
                raise ValueError(f"inline reference missing from draft: {unit_id}")
        else:
            translation = structural_translation(case, args.chapter)
            origin = "deterministic_structural_translation"
            status = "structural"
        if not translation:
            missing.append(unit_id)
            continue
        correction = corrections.get(unit_id)
        if correction:
            if correction.get("before") != translation:
                raise ValueError(f"correction baseline mismatch: {unit_id}")
            translation = correction["after"].strip()
            status = correction.get("status", "chapter_reviewed")
        segments = overrides.get(unit_id, case["format_segments"])
        if unit_id in overrides:
            reconstructed = "".join(segment["text"] for segment in segments)
            if reconstructed != case["source_text"]:
                raise ValueError(
                    f"format override does not reconstruct source: {unit_id}"
                )
        validate_preserved_segments(segments, unit_id, translation)
        assembled.append(
            {
                "content_type": case["content_type"],
                "draft_origin": origin,
                "effective_format_segments": segments,
                "format_override_applied": unit_id in overrides,
                "inline_references": case["inline_references"],
                "markup_requirements": source_markup_requirements(case["source_html"]),
                "source": case["source_text"],
                "source_html": case["source_html"],
                "status": status,
                "translation": translation,
                "unit_id": unit_id,
                **(
                    {"footnote_reference": footnote_reference_metadata(case)}
                    if case["content_type"] == "footnote"
                    else {}
                ),
                **({"correction": correction} if correction else {}),
            }
        )

    write_jsonl(args.output / "chapter_units.jsonl", assembled)
    report = {
        "assembled_units": len(assembled),
        "chapter": args.chapter,
        "complete": not missing and len(assembled) == len(cases),
        "corrections_applied": sorted(corrections),
        "corrections_path": str(corrections_path) if corrections_path else None,
        "expected_units": len(cases),
        "format_overrides_applied": sorted(set(overrides) & case_ids),
        "missing_unit_ids": missing,
        "review_only": True,
        "epub_writeback_source": False,
    }
    write_json(args.output / "report.json", report)

    review_notice = (
        "<!-- REVIEW ONLY: preserve chapter_units.jsonl metadata and source HTML "
        "for EPUB writeback. -->"
    )
    chinese_lines = [review_notice, ""]
    bilingual_lines = [
        review_notice,
        "",
        f"# Chapter {args.chapter} bilingual review",
        "",
    ]
    for row in assembled:
        append_chinese_unit(chinese_lines, row)
        bilingual_lines.extend(
            [
                f"<!-- {row['unit_id']} -->",
                "",
                "**Original**",
                "",
                row["source"],
                "",
                "**中文**",
                "",
                render_translation_for_review(row),
                "",
            ]
        )
    (args.output / "chapter_zhHans.md").write_text(
        "\n".join(chinese_lines), encoding="utf-8"
    )
    (args.output / "chapter_bilingual.md").write_text(
        "\n".join(bilingual_lines), encoding="utf-8"
    )
    revision_lines = [f"# Chapter {args.chapter} revision log", ""]
    if corrections:
        for unit_id, correction in corrections.items():
            reviewers = ", ".join(correction.get("reviewers", [])) or "unspecified"
            revision_lines.extend(
                [
                    f"## {unit_id}",
                    "",
                    f"- Reason: {correction['reason']}",
                    f"- Basis: {correction.get('basis', 'chapter review')}",
                    f"- Reviewers: {reviewers}",
                    "",
                    "**Before**",
                    "",
                    correction["before"],
                    "",
                    "**After**",
                    "",
                    correction["after"],
                    "",
                ]
            )
    else:
        revision_lines.append("- No chapter-level corrections applied.")
    (args.output / "revision_log.md").write_text(
        "\n".join(revision_lines) + "\n", encoding="utf-8"
    )
    print(
        f"Assembled chapter {args.chapter}: {len(assembled)}/{len(cases)} units; "
        f"missing={len(missing)}"
    )
    return 0 if not missing else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-brief", required=True, type=Path)
    parser.add_argument("--drafts-root", required=True, type=Path)
    parser.add_argument("--chapter", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--corrections", type=Path)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    try:
        return assemble(parse_args())
    except (FileExistsError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
