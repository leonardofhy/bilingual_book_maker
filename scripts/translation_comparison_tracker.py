#!/usr/bin/env python3
"""Build and validate a tracked team-vs-EPUB translation comparison corpus."""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import io
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


VERDICTS = {"team", "ours", "tie", "mixed", "needs_review", "no_comparison"}
CONFIDENCES = {"high", "medium", "low"}


def normalized_source(value: str) -> str:
    value = value.replace("**", "").replace("__", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", value).strip()


def chapter_for(filename: str) -> str:
    if filename == "ch1.csv":
        return "chapter001"
    if filename == "introduction.csv":
        return "introduction"
    match = re.search(r"(\d+)", filename)
    if not match:
        raise ValueError(f"cannot determine chapter from {filename}")
    return f"chapter{int(match.group(1)):03d}"


def key_order(value: str) -> tuple[int, str]:
    numbers = re.findall(r"\d+", value)
    return (int(numbers[-1]) if numbers else 0, value)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_team_zip(path: Path) -> tuple[dict, dict]:
    sources: dict[str, dict[str, str]] = defaultdict(dict)
    translations: dict[str, dict[str, dict[str, set[str]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(set))
    )
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            parts = info.filename.split("/", 2)
            if len(parts) != 3 or not parts[2].endswith(".csv"):
                continue
            _, member, filename = parts
            chapter = chapter_for(filename)
            text = archive.read(info).decode("utf-8-sig")
            for row in csv.reader(io.StringIO(text)):
                if len(row) < 2:
                    continue
                key = row[0].lstrip("\ufeff")
                original = row[1]
                previous = sources[chapter].setdefault(key, original)
                if normalized_source(previous) != normalized_source(original):
                    raise ValueError(f"source mismatch at {chapter}/{key}")
                if len(row) >= 3 and row[2].strip():
                    translations[chapter][key][row[2].strip()].add(member)
    return sources, translations


def read_ours(path: Path) -> dict[str, list[dict]]:
    chapters = {}
    for json_path in path.glob("*.json"):
        if json_path.name == "_all.json":
            continue
        chapters[json_path.stem] = json.loads(json_path.read_text(encoding="utf-8"))
    return chapters


def align_chapter(team_sources: dict[str, str], ours: list[dict]) -> dict[str, dict]:
    team_keys = sorted(team_sources, key=key_order)
    team_sequence = [normalized_source(team_sources[key]) for key in team_keys]
    ours_sequence = [normalized_source(entry["original"]) for entry in ours]
    matcher = difflib.SequenceMatcher(
        None, team_sequence, ours_sequence, autojunk=False
    )
    mapping = {}
    for team_start, ours_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            mapping[team_keys[team_start + offset]] = ours[ours_start + offset]
    return mapping


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def build(args: argparse.Namespace) -> int:
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    units_path = output / "source_units.jsonl"
    if units_path.exists() and not args.force:
        raise FileExistsError(f"comparison corpus already exists: {units_path}")

    sources, translations = read_team_zip(args.team_zip)
    ours = read_ours(args.ours_dir)
    units = []
    for chapter in sorted(translations):
        alignment = align_chapter(sources[chapter], ours.get(chapter, []))
        for team_key in sorted(translations[chapter], key=key_order):
            candidates = [
                {"translation": text, "members": sorted(members)}
                for text, members in translations[chapter][team_key].items()
            ]
            candidates.sort(
                key=lambda item: (-len(item["members"]), item["translation"])
            )
            ours_entry = alignment.get(team_key)
            units.append(
                {
                    "comparison_id": f"{chapter}:{team_key}",
                    "chapter": chapter,
                    "team_key": team_key,
                    "original": sources[chapter][team_key],
                    "ours_key": ours_entry["key"] if ours_entry else None,
                    "ours_translation": ours_entry["translation"] if ours_entry else "",
                    "team_candidates": candidates,
                    "alignment": "exact_sequence" if ours_entry else "ours_missing",
                }
            )

    ids = [unit["comparison_id"] for unit in units]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate comparison IDs")
    write_jsonl(units_path, units)

    evaluations_path = output / "evaluations.jsonl"
    if args.force or not evaluations_path.exists():
        evaluations_path.write_text("", encoding="utf-8")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "team_zip": str(args.team_zip.resolve()),
        "team_zip_sha256": sha256(args.team_zip),
        "ours_dir": str(args.ours_dir.resolve()),
        "terms_file": str(args.terms.resolve()),
        "terms_sha256": sha256(args.terms),
        "comparison_units": len(units),
        "aligned_units": sum(unit["alignment"] == "exact_sequence" for unit in units),
        "ours_missing_units": sum(
            unit["alignment"] == "ours_missing" for unit in units
        ),
        "rubric": {
            "priority": [
                "semantic_accuracy_and_completeness",
                "logic_and_reference_clarity",
                "team_term_consistency",
                "natural_simplified_chinese",
                "voice_rhythm_and_register",
            ],
            "verdicts": sorted(VERDICTS),
            "confidences": sorted(CONFIDENCES),
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Built {len(units)} comparison units in {output}")
    return write_progress(output)


def validate_evaluation(evaluation: dict, valid_ids: set[str]) -> None:
    comparison_id = evaluation.get("comparison_id")
    if comparison_id not in valid_ids:
        raise ValueError(f"unknown comparison_id: {comparison_id}")
    if evaluation.get("verdict") not in VERDICTS:
        raise ValueError(f"invalid verdict at {comparison_id}")
    if evaluation.get("confidence") not in CONFIDENCES:
        raise ValueError(f"invalid confidence at {comparison_id}")
    if not str(evaluation.get("reason", "")).strip():
        raise ValueError(f"missing reason at {comparison_id}")
    if (
        evaluation.get("verdict") == "team"
        and evaluation.get("best_team_candidate") is None
    ):
        raise ValueError(f"team verdict lacks best_team_candidate at {comparison_id}")


def markdown_text(value: str) -> str:
    return value.replace("\r\n", "\n").strip()


def write_report(output: Path, units: list[dict], evaluations: list[dict]) -> None:
    units_by_id = {unit["comparison_id"]: unit for unit in units}
    lines = [
        "# Team vs. EPUB translation comparison",
        "",
        "> Decision priority: semantic accuracy and completeness; logic and references; team terms; natural Simplified Chinese; voice and rhythm.",
        "",
    ]
    current_chapter = None
    for evaluation in evaluations:
        unit = units_by_id[evaluation["comparison_id"]]
        if unit["chapter"] != current_chapter:
            current_chapter = unit["chapter"]
            lines.extend([f"## {current_chapter}", ""])
        candidate_index = evaluation.get("best_team_candidate", 0)
        candidate = unit["team_candidates"][candidate_index]
        lines.extend(
            [
                f"### {unit['comparison_id']}",
                "",
                f"- Verdict: **{evaluation['verdict']}**",
                f"- Confidence: **{evaluation['confidence']}**",
                f"- Team candidate: {candidate_index} ({', '.join(candidate['members'])})",
                f"- Reason: {evaluation['reason']}",
                "",
                "**Original**",
                "",
                markdown_text(unit["original"]),
                "",
                "**Our EPUB**",
                "",
                markdown_text(unit["ours_translation"]) or "_[missing]_",
                "",
                "**Selected team translation**",
                "",
                markdown_text(candidate["translation"]),
                "",
            ]
        )
        if len(unit["team_candidates"]) > 1:
            lines.extend(
                [
                    f"Other team candidates retained in `source_units.jsonl`: {len(unit['team_candidates']) - 1}",
                    "",
                ]
            )
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_progress(output: Path) -> int:
    units = read_jsonl(output / "source_units.jsonl")
    evaluations = read_jsonl(output / "evaluations.jsonl")
    valid_ids = {unit["comparison_id"] for unit in units}
    by_id = {}
    for evaluation in evaluations:
        validate_evaluation(evaluation, valid_ids)
        comparison_id = evaluation["comparison_id"]
        if comparison_id in by_id:
            raise ValueError(f"duplicate evaluation: {comparison_id}")
        by_id[comparison_id] = evaluation

    chapter_totals = Counter(unit["chapter"] for unit in units)
    chapter_done = Counter(comparison_id.split(":", 1)[0] for comparison_id in by_id)
    verdicts = Counter(evaluation["verdict"] for evaluation in evaluations)
    lines = [
        "# Translation comparison progress",
        "",
        f"- Total comparison units: {len(units)}",
        f"- Completed: {len(evaluations)}",
        f"- Pending: {len(units) - len(evaluations)}",
        (
            f"- Completion: {len(evaluations) / len(units):.1%}"
            if units
            else "- Completion: 0.0%"
        ),
        "",
        "## Chapters",
        "",
        "| Chapter | Completed | Total |",
        "|---|---:|---:|",
    ]
    for chapter in sorted(chapter_totals):
        lines.append(
            f"| {chapter} | {chapter_done[chapter]} | {chapter_totals[chapter]} |"
        )
    lines.extend(["", "## Verdicts", ""])
    for verdict in sorted(VERDICTS):
        lines.append(f"- {verdict}: {verdicts[verdict]}")
    lines.extend(
        [
            "",
            "Each completed evaluation must include a verdict, confidence, and non-empty reason.",
            "For conflicting team versions, best_team_candidate identifies the compared candidate index.",
        ]
    )
    (output / "PROGRESS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_report(output, units, evaluations)
    print(f"Validated {len(evaluations)}/{len(units)} completed evaluations")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--team-zip", required=True, type=Path)
    build_parser.add_argument("--ours-dir", required=True, type=Path)
    build_parser.add_argument("--terms", required=True, type=Path)
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--force", action="store_true")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "build":
            return build(args)
        return write_progress(args.output)
    except (FileExistsError, OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
