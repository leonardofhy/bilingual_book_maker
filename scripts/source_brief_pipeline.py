#!/usr/bin/env python3
"""Build and validate source-only briefing cases from a frozen baseline."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

from build_translation_baseline import (
    generated_hashes,
    read_json,
    read_jsonl,
    sha256,
    term_present,
    write_json,
    write_jsonl,
)


BRIEF_SCHEMA_VERSION = 1
CONFIDENCES = {"high", "medium", "low"}
FINAL_STATUS = "final"
ALLOWED_CORRECTION_FIELDS = {
    "ambiguities",
    "confidence",
    "constraints",
    "cross_unit",
    "logic",
    "propositions",
    "rhetoric",
    "term_cards",
}
STRUCTURAL_TYPES = {
    "chapter_number",
    "footnotes_heading",
    "endnotes_section_heading",
    "url_marker",
}
FORBIDDEN_CASE_KEYS = {
    "alignment_status",
    "current_translation",
    "members",
    "origin",
    "review_payload",
    "translation",
    "translation_html",
    "translation_sha256",
}
NEGATIONS = ("not", "never", "no ", "nobody", "nothing", "without", "nor")
MODALS = ("must", "should", "could", "would", "might", "may", "can", "likely")
CAUSAL = ("because", "therefore", "so that", "if ", "unless", "even if", "then")
SCOPE_MARKERS = ("only", "all", "every", "any", "some", "at least", "even", "just")
QUANTITY_RE = re.compile(
    r"(?:\$?\d[\d,.]*(?:\s*(?:percent|%|years?|months?|days?|times?))?)",
    re.IGNORECASE,
)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z“‘\"])")


def safe_output_path(output: Path, workspace: Path) -> Path:
    if output.exists() and output.is_symlink():
        raise ValueError(f"refusing symlink output path: {output}")
    resolved = output.resolve()
    workspace_root = workspace.resolve()
    allowed_roots = {
        (workspace_root / "translation_preview").resolve(),
        Path("/tmp").resolve(),
    }
    if not output.name.startswith("source_brief_"):
        raise ValueError("source brief output name must start with 'source_brief_'")
    if resolved in {Path("/").resolve(), workspace_root, *allowed_roots}:
        raise ValueError(f"refusing destructive output path: {resolved}")
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise ValueError(
            "source brief output must be inside workspace/translation_preview or /tmp"
        )
    return resolved


def recursive_keys(value) -> set[str]:
    keys = set()
    if isinstance(value, dict):
        keys.update(value)
        for item in value.values():
            keys.update(recursive_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(recursive_keys(item))
    return keys


def split_term_names(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*(?:/|\|)\s*", value) if part.strip()]


def term_variants(term: str) -> list[str]:
    variants = [term]
    if re.fullmatch(r"[A-Za-z -]+", term) and not term.casefold().endswith("s"):
        variants.append(f"{term}s")
    return variants


def provenance_index(provenance_rows: list[dict]) -> dict[tuple[int, int], str]:
    result = {}
    for row in provenance_rows:
        index = row.get("legacy_translation_index")
        match = re.match(r"book_ch(\d+)\.", row["unit_id"])
        if index is not None and match:
            result[(int(match.group(1)), index)] = row["unit_id"]
    return result


def term_cards_by_unit(
    source_rows: list[dict], term_snapshot: dict, provenance_rows: list[dict]
) -> dict[str, list[dict]]:
    cards: dict[str, list[dict]] = defaultdict(list)
    for entry in term_snapshot["required_and_contextual"]:
        terms = [
            variant
            for term in split_term_names(entry["english"])
            for variant in term_variants(term)
        ]
        for row in source_rows:
            matched = [term for term in terms if term_present(row["source_text"], term)]
            if matched:
                cards[row["unit_id"]].append(
                    {
                        "matched_source_terms": matched,
                        "note": "house term from baseline_v2",
                        "source": entry["english"],
                        "status": entry["status"],
                        "target": entry["translation"],
                    }
                )
    for entry in term_snapshot["paratranz_candidates"]:
        for unit_id in entry["matched_unit_ids"]:
            cards[unit_id].append(
                {
                    "matched_source_terms": entry["matched_source_terms"],
                    "note": entry["note"],
                    "source": " / ".join(entry["matched_source_terms"]),
                    "status": "candidate_unverified",
                    "target": entry["translation"],
                }
            )
    index = provenance_index(provenance_rows)
    for entry in term_snapshot["legacy_candidates"]:
        paragraph = entry.get("para")
        if not isinstance(paragraph, int):
            continue
        unit_id = index.get((entry["book_chapter"], paragraph))
        if unit_id:
            cards[unit_id].append(
                {
                    "matched_source_terms": [entry["en"]],
                    "note": entry.get("note", ""),
                    "source": entry["en"],
                    "status": "candidate_legacy_zhHant",
                    "target": entry["zh"],
                }
            )
    for unit_id, unit_cards in cards.items():
        deduplicated = {}
        for card in unit_cards:
            key = (card["source"], card["target"], card["status"])
            deduplicated[key] = card
        cards[unit_id] = list(deduplicated.values())
    return cards


def endnote_relations(source_rows: list[dict]) -> dict[str, list[str]]:
    by_chapter: dict[int, list[dict]] = defaultdict(list)
    for row in source_rows:
        if row["content_type"] == "body":
            by_chapter[row["book_chapter"]].append(row)
    relations = {}
    for row in source_rows:
        if not row["content_type"].startswith("endnote_"):
            continue
        locator_segments = [
            item for item in row["source_segments"] if item["kind"] == "locator_phrase"
        ]
        if not locator_segments:
            continue
        locator = locator_segments[0]["text"].rstrip(":").strip()
        related = [
            body["unit_id"]
            for body in by_chapter[row["book_chapter"]]
            if locator and locator.casefold() in body["source_text"].casefold()
        ]
        relations[row["unit_id"]] = related
    return relations


def make_case(
    row: dict,
    policy: dict,
    term_cards: list[dict],
    related_units: list[str],
) -> dict:
    return {
        "book_chapter": row["book_chapter"],
        "content_policy": policy,
        "content_type": row["content_type"],
        "context_after": row["context_after"],
        "context_before": row["context_before"],
        "format_segments": row["source_segments"],
        "inline_references": row.get("inline_references", []),
        "related_source_units": related_units,
        "source_html": row["source_html"],
        "source_text": row["source_text"],
        "term_cards": term_cards,
        "unit_id": row["unit_id"],
    }


def marker_hits(source: str, markers: tuple[str, ...]) -> list[str]:
    lowered = source.casefold()
    return [marker.strip() for marker in markers if marker in lowered]


def scaffold(case: dict) -> dict:
    sentences = [
        part.strip() for part in SENTENCE_RE.split(case["source_text"]) if part.strip()
    ]
    propositions = [
        {
            "id": f"P{index}",
            "meaning": "",
            "source_span": sentence,
            "status": "requires_llm_analysis",
        }
        for index, sentence in enumerate(sentences or [case["source_text"]], start=1)
    ]
    preserve_segments = [
        item for item in case["format_segments"] if item["mode"] == "preserve_exact"
    ]
    return {
        "analysis_independence": "source_only_package",
        "confidence": "low",
        "constraints": [
            {
                "evidence": item["text"],
                "kind": "protected_segment",
                "requirement": f"Preserve {item['segment_id']} exactly.",
                "severity": "hard",
            }
            for item in preserve_segments
        ]
        + [
            {
                "evidence": reference["marker"],
                "kind": "inline_reference",
                "requirement": (
                    f"Preserve inline reference {reference['anchor_id']} linking "
                    f"to {reference['href']}."
                ),
                "severity": "hard",
            }
            for reference in case["inline_references"]
        ],
        "cross_unit": {
            "reason": "deterministic relation only; analyst must verify",
            "related_unit_ids": case["related_source_units"],
            "required": bool(case["related_source_units"]),
        },
        "logic": {
            "causality": marker_hits(case["source_text"], CAUSAL),
            "modality": marker_hits(case["source_text"], MODALS),
            "negation": marker_hits(case["source_text"], NEGATIONS),
            "quantities": QUANTITY_RE.findall(case["source_text"]),
            "references": [],
            "scope": marker_hits(case["source_text"], SCOPE_MARKERS),
        },
        "propositions": propositions,
        "rhetoric": {
            "devices": [],
            "purpose": "",
            "tone": "",
        },
        "status": "needs_llm_analysis",
        "term_cards": case["term_cards"],
        "unit_id": case["unit_id"],
    }


def deterministic_brief(case: dict) -> dict | None:
    content_type = case["content_type"]
    if content_type not in STRUCTURAL_TYPES:
        return None
    if content_type == "chapter_number":
        meaning = f"Identifies this section as book chapter {case['book_chapter']}."
        purpose = "structural chapter numbering"
    elif content_type in {"footnotes_heading", "endnotes_section_heading"}:
        meaning = "Identifies the beginning of the notes belonging to this chapter."
        purpose = "structural notes heading"
    else:
        meaning = "Provides the chapter-specific companion website address."
        purpose = "exact URL marker"
    preserve_segments = [
        item for item in case["format_segments"] if item["mode"] == "preserve_exact"
    ]
    constraints = [
        {
            "evidence": case["source_text"],
            "kind": "structure",
            "requirement": "Preserve the structural identity and chapter association.",
            "severity": "hard",
        }
    ]
    constraints.extend(
        {
            "evidence": item["text"],
            "kind": "protected_segment",
            "requirement": f"Preserve {item['segment_id']} exactly.",
            "severity": "hard",
        }
        for item in preserve_segments
    )
    constraints.extend(
        {
            "evidence": reference["marker"],
            "kind": "inline_reference",
            "requirement": (
                f"Preserve inline reference {reference['anchor_id']} linking "
                f"to {reference['href']}."
            ),
            "severity": "hard",
        }
        for reference in case["inline_references"]
    )
    return {
        "analysis_independence": "deterministic_source_only",
        "confidence": "high",
        "constraints": constraints,
        "cross_unit": {
            "reason": "structural relation",
            "related_unit_ids": case["related_source_units"],
            "required": bool(case["related_source_units"]),
        },
        "logic": {
            "causality": [],
            "modality": [],
            "negation": [],
            "quantities": QUANTITY_RE.findall(case["source_text"]),
            "references": [],
            "scope": [],
        },
        "propositions": [
            {
                "id": "P1",
                "meaning": meaning,
                "source_span": case["source_text"],
                "status": "analyzed",
            }
        ],
        "rhetoric": {
            "devices": [],
            "purpose": purpose,
            "tone": "neutral",
        },
        "status": FINAL_STATUS,
        "term_cards": case["term_cards"],
        "unit_id": case["unit_id"],
    }


def instructions_text() -> str:
    return """# Source brief protocol

Analyze the English source before viewing any candidate translation.

1. Split the source into atomic propositions. Every proposition needs an exact
   `source_span` and a concise Chinese `meaning`.
2. Record negation, modality, causal/conditional structure, scope, quantities,
   and references that a translation must preserve.
3. Describe rhetorical purpose, tone, and deliberate devices without proposing
   Chinese wording.
4. Convert risks into evidence-backed constraints. Separate semantic hard gates
   from editorial preferences.
5. Treat `required` terms as binding. `contextual`, `candidate_unverified`, and
   `candidate_legacy_zhHant` cards are evidence only and cannot decide meaning.
6. Every `preserve_exact` format segment is a hard constraint.
7. Use adjacent context only to resolve meaning and reference. Set cross-unit
   requirements explicitly when one unit is insufficient.
8. Do not read the baseline translation snapshot, old reviews, or future
   candidate files. Do not propose, rank, or identify translations.
9. Record genuine ambiguity explicitly. Never resolve an unsupported detail
   (for example, relative age in `sister`) merely to make Chinese wording easier.
"""


def report_status(output: Path, cases: list[dict], finals: list[dict]) -> None:
    final_ids = {row["unit_id"] for row in finals}
    batch = read_json(output / "batches" / "batch_001.json")
    completed_batch = [unit_id for unit_id in batch["unit_ids"] if unit_id in final_ids]
    by_type = Counter(
        case["content_type"] for case in cases if case["unit_id"] in final_ids
    )
    corrections_path = output / "stages" / "review_corrections_batch_001.jsonl"
    corrections = read_jsonl(corrections_path) if corrections_path.exists() else []
    lines = [
        "# Source brief status",
        "",
        f"- Total source-only cases: {len(cases)}",
        f"- Final briefs: {len(finals)}",
        f"- Remaining: {len(cases) - len(finals)}",
        f"- Batch 001 complete: {len(completed_batch)}/{len(batch['unit_ids'])}",
        f"- Review corrections applied: {len(corrections)}",
        "",
        "Final briefs by content type:",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(by_type.items()))
    (output / "reports" / "status.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def update_manifest(output: Path) -> None:
    manifest_path = output / "manifest.json"
    manifest = read_json(manifest_path)
    cases = read_jsonl(output / "input" / "cases.jsonl")
    finals = read_jsonl(output / "stages" / "final.jsonl")
    manifest["final_briefs"] = len(finals)
    manifest["remaining_briefs"] = len(cases) - len(finals)
    manifest["generated_files_sha256"] = generated_hashes(output)
    write_json(manifest_path, manifest)


def validate_brief(brief: dict, case: dict) -> None:
    if brief.get("status") != FINAL_STATUS:
        raise ValueError(f"brief is not final: {brief.get('unit_id')}")
    if brief.get("confidence") not in CONFIDENCES:
        raise ValueError(f"invalid confidence: {brief.get('unit_id')}")
    propositions = brief.get("propositions", [])
    proposition_ids = [item.get("id") for item in propositions]
    if (
        not propositions
        or None in proposition_ids
        or len(proposition_ids) != len(set(proposition_ids))
    ):
        raise ValueError(f"invalid propositions: {brief.get('unit_id')}")
    for proposition in propositions:
        if not proposition.get("meaning") or not proposition.get("source_span"):
            raise ValueError(f"incomplete proposition: {brief.get('unit_id')}")
        if proposition["source_span"] not in case["source_text"]:
            raise ValueError(f"proposition evidence absent: {brief.get('unit_id')}")
    logic = brief.get("logic", {})
    required_logic = {
        "causality",
        "modality",
        "negation",
        "quantities",
        "references",
        "scope",
    }
    if set(logic) != required_logic:
        raise ValueError(f"logic schema incomplete: {brief.get('unit_id')}")
    rhetoric = brief.get("rhetoric", {})
    if not rhetoric.get("purpose") or not rhetoric.get("tone"):
        raise ValueError(f"rhetoric missing: {brief.get('unit_id')}")
    if not brief.get("constraints"):
        raise ValueError(f"constraints missing: {brief.get('unit_id')}")
    protected_ids = {
        item["segment_id"]
        for item in case["format_segments"]
        if item["mode"] == "preserve_exact"
    }
    constrained_ids = {
        match.group(1)
        for constraint in brief["constraints"]
        for match in [re.search(r"Preserve (S\d+) exactly", constraint["requirement"])]
        if match
    }
    if not protected_ids <= constrained_ids:
        raise ValueError(
            f"protected segment constraint missing: {brief.get('unit_id')}"
        )
    required_anchors = {
        reference["anchor_id"] for reference in case["inline_references"]
    }
    constrained_anchors = {
        match.group(1)
        for constraint in brief["constraints"]
        for match in [
            re.search(
                r"Preserve inline reference ([A-Za-z0-9_-]+)",
                constraint["requirement"],
            )
        ]
        if match
    }
    if not required_anchors <= constrained_anchors:
        raise ValueError(f"inline reference constraint missing: {brief.get('unit_id')}")


def apply_review_corrections(manual: list[dict], corrections: list[dict]) -> list[dict]:
    corrected = {row["unit_id"]: dict(row) for row in manual}
    correction_ids = [row.get("unit_id") for row in corrections]
    if None in correction_ids or len(correction_ids) != len(set(correction_ids)):
        raise ValueError("duplicate or missing review correction unit IDs")
    for correction in corrections:
        unit_id = correction["unit_id"]
        if unit_id not in corrected:
            raise ValueError(f"review correction target is not manual: {unit_id}")
        changes = correction.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise ValueError(f"review correction has no changes: {unit_id}")
        unsupported = set(changes) - ALLOWED_CORRECTION_FIELDS
        if unsupported:
            raise ValueError(
                f"unsupported review correction fields at {unit_id}: "
                f"{sorted(unsupported)}"
            )
        corrected[unit_id].update(changes)
        corrected[unit_id].setdefault("review_history", []).append(
            {
                "findings": correction.get("findings", []),
                "review_id": correction["review_id"],
                "reviewed_at": correction["reviewed_at"],
            }
        )
    return list(corrected.values())


def validate(output: Path, check_hashes: bool = True) -> int:
    manifest = read_json(output / "manifest.json")
    cases = read_jsonl(output / "input" / "cases.jsonl")
    scaffolds = read_jsonl(output / "stages" / "scaffolds.jsonl")
    finals = read_jsonl(output / "stages" / "final.jsonl")
    corrections_path = output / "stages" / "review_corrections_batch_001.jsonl"
    corrections = read_jsonl(corrections_path) if corrections_path.exists() else []
    case_by_id = {row["unit_id"]: row for row in cases}
    if len(cases) != manifest["total_cases"] or len(case_by_id) != len(cases):
        raise ValueError("case count or uniqueness mismatch")
    if {row["unit_id"] for row in scaffolds} != set(case_by_id):
        raise ValueError("scaffolds do not cover every case")
    for case in cases:
        leaked = recursive_keys(case) & FORBIDDEN_CASE_KEYS
        if leaked:
            raise ValueError(
                f"candidate data leaked at {case['unit_id']}: {sorted(leaked)}"
            )
        if 'lang="zh' in case["source_html"].casefold():
            raise ValueError(
                f"Chinese translation leaked in source HTML: {case['unit_id']}"
            )
    final_ids = [row.get("unit_id") for row in finals]
    if None in final_ids or len(final_ids) != len(set(final_ids)):
        raise ValueError("duplicate or missing final brief unit IDs")
    if not set(final_ids) <= set(case_by_id):
        raise ValueError("unknown final brief unit ID")
    for brief in finals:
        validate_brief(brief, case_by_id[brief["unit_id"]])
    if corrections:
        final_by_id = {row["unit_id"]: row for row in finals}
        manual = read_jsonl(output / "stages" / "manual_batch_001.jsonl")
        expected = apply_review_corrections(manual, corrections)
        for brief in expected:
            actual = final_by_id.get(brief["unit_id"])
            if actual != brief:
                raise ValueError(
                    f"review correction not reflected in final: {brief['unit_id']}"
                )
    if check_hashes:
        for relative_path, expected_hash in manifest["generated_files_sha256"].items():
            path = output / relative_path
            if not path.exists() or sha256(path) != expected_hash:
                raise ValueError(f"generated file hash mismatch: {relative_path}")
    print(
        f"Validated source brief package: {len(cases)} cases, "
        f"{len(finals)} final briefs, no candidate translation leakage"
    )
    return 0


def merge(output: Path) -> int:
    deterministic = read_jsonl(output / "stages" / "deterministic.jsonl")
    manual = read_jsonl(output / "stages" / "manual_batch_001.jsonl")
    corrections_path = output / "stages" / "review_corrections_batch_001.jsonl"
    corrections = read_jsonl(corrections_path) if corrections_path.exists() else []
    manual = apply_review_corrections(manual, corrections)
    merged = {row["unit_id"]: row for row in deterministic}
    for row in manual:
        merged[row["unit_id"]] = row
    cases = read_jsonl(output / "input" / "cases.jsonl")
    order = {row["unit_id"]: index for index, row in enumerate(cases)}
    final_rows = sorted(merged.values(), key=lambda row: order[row["unit_id"]])
    write_jsonl(output / "stages" / "final.jsonl", final_rows)
    report_status(output, cases, final_rows)
    validate(output, check_hashes=False)
    update_manifest(output)
    validate(output, check_hashes=True)
    return 0


def build(args: argparse.Namespace) -> int:
    output = safe_output_path(args.output, args.workspace)
    if output.exists():
        if not args.force:
            raise FileExistsError(f"output exists: {output}")
        shutil.rmtree(output)
    for directory in ("config", "input", "stages", "batches", "reports"):
        (output / directory).mkdir(parents=True, exist_ok=True)

    baseline_manifest = read_json(args.baseline / "manifest.json")
    if baseline_manifest.get("baseline_version") != 2:
        raise ValueError("source briefing requires baseline_v2")
    source_rows = read_jsonl(args.baseline / "source" / "units.jsonl")
    provenance_rows = read_jsonl(args.baseline / "private" / "dom_provenance.jsonl")
    policies = read_json(args.baseline / "config" / "content_policies.json")
    term_snapshot = read_json(args.baseline / "config" / "term_snapshot.json")
    cards = term_cards_by_unit(source_rows, term_snapshot, provenance_rows)
    relations = endnote_relations(source_rows)
    cases = [
        make_case(
            row,
            policies[row["policy_key"]],
            cards.get(row["unit_id"], []),
            relations.get(row["unit_id"], []),
        )
        for row in source_rows
    ]
    scaffolds = [scaffold(case) for case in cases]
    deterministic = [
        brief
        for case in cases
        for brief in [deterministic_brief(case)]
        if brief is not None
    ]
    first_chapter_cases = [
        case
        for case in cases
        if case["book_chapter"] == 11
        and case["unit_id"].startswith("book_ch11.")
        and ".endnote." not in case["unit_id"]
        and case["unit_id"] != "book_ch11.endnotes.heading"
    ]
    batch_ids = [case["unit_id"] for case in first_chapter_cases[:10]]
    write_json(output / "config" / "brief_schema.json", brief_schema())
    (output / "config" / "BRIEF_INSTRUCTIONS.md").write_text(
        instructions_text(), encoding="utf-8"
    )
    write_jsonl(output / "input" / "cases.jsonl", cases)
    write_jsonl(output / "stages" / "scaffolds.jsonl", scaffolds)
    write_jsonl(output / "stages" / "deterministic.jsonl", deterministic)
    (output / "stages" / "manual_batch_001.jsonl").write_text("", encoding="utf-8")
    (output / "stages" / "review_corrections_batch_001.jsonl").write_text(
        "", encoding="utf-8"
    )
    write_jsonl(output / "stages" / "final.jsonl", deterministic)
    write_json(
        output / "batches" / "batch_001.json",
        {
            "purpose": "first ten Chapter 11 source units",
            "unit_ids": batch_ids,
        },
    )
    (output / "README.md").write_text(
        "# Chapter 11 and 13 source briefs\n\n"
        "This package contains English source, context, terms, and "
        "format constraints.\n"
        "It intentionally excludes current and team translation candidates.\n",
        encoding="utf-8",
    )
    report_status(output, cases, deterministic)
    manifest = {
        "baseline_manifest_sha256": sha256(args.baseline / "manifest.json"),
        "baseline_path": str(args.baseline.resolve()),
        "brief_schema_version": BRIEF_SCHEMA_VERSION,
        "final_briefs": len(deterministic),
        "generated_files_sha256": {},
        "remaining_briefs": len(cases) - len(deterministic),
        "source_only": True,
        "total_cases": len(cases),
    }
    write_json(output / "manifest.json", manifest)
    update_manifest(output)
    validate(output)
    print(
        f"Built {len(cases)} source-only cases with "
        f"{len(deterministic)} deterministic structural briefs"
    )
    return 0


def brief_schema() -> dict:
    return {
        "optional_fields": ["ambiguities", "review_history"],
        "required_fields": [
            "unit_id",
            "status",
            "analysis_independence",
            "propositions",
            "logic",
            "rhetoric",
            "constraints",
            "term_cards",
            "cross_unit",
            "confidence",
        ],
        "status": [FINAL_STATUS],
        "confidence": sorted(CONFIDENCES),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--baseline", required=True, type=Path)
    build_parser.add_argument("--workspace", required=True, type=Path)
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--force", action="store_true")
    for name in ("validate", "merge"):
        command = commands.add_parser(name)
        command.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "build":
            return build(args)
        if args.command == "merge":
            return merge(args.output)
        return validate(args.output)
    except (FileExistsError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
