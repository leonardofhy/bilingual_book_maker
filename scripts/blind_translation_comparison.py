#!/usr/bin/env python3
"""Build and validate content-anchored, multi-version blind translation reviews."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import secrets
import shutil
import sys
from collections import Counter
from pathlib import Path


LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ERROR_SEVERITIES = {"none", "minor", "major", "critical"}
COVERAGE_STATUSES = {"preserved", "partial", "omitted", "distorted"}
CONFIDENCES = {"high", "medium", "low"}
DIMENSION_RATINGS = {"strong", "acceptable", "weak"}
REQUIRED_DIMENSIONS = {"naturalness", "logic_references", "terminology", "voice_rhythm"}
VERDICTS = {"winner", "tie", "both_flawed", "needs_context"}


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_export_markup(text: str) -> str:
    """Remove transport-only Markdown while preserving authored punctuation."""
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("**", "").replace("__", "")
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def fragments(text: str) -> list[str]:
    cleaned = clean_export_markup(text)
    return [part.strip() for part in re.split(r"\n+", cleaned) if part.strip()]


def stable_version_id(unit_id: str, origin: str, text: str) -> str:
    raw = f"{unit_id}\0{origin}\0{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def build_content_units(source_rows: list[dict], chapters: set[str]) -> list[dict]:
    selected = [row for row in source_rows if row["chapter"] in chapters]
    chapter_rows: dict[str, list[dict]] = {}
    for row in selected:
        chapter_rows.setdefault(row["chapter"], []).append(row)

    output = []
    for chapter in sorted(chapter_rows):
        rows = chapter_rows[chapter]
        for index, row in enumerate(rows):
            versions = []
            ours_text = clean_export_markup(row["ours_translation"])
            versions.append(
                {
                    "version_id": stable_version_id(
                        row["comparison_id"], "ours", ours_text
                    ),
                    "origin": "ours",
                    "members": [],
                    "fragments": fragments(ours_text),
                    "is_missing": not bool(ours_text),
                }
            )
            for candidate_index, candidate in enumerate(row["team_candidates"]):
                text = clean_export_markup(candidate["translation"])
                origin = f"team_candidate_{candidate_index}"
                versions.append(
                    {
                        "version_id": stable_version_id(
                            row["comparison_id"], origin, text
                        ),
                        "origin": origin,
                        "members": candidate["members"],
                        "fragments": fragments(text),
                        "is_missing": not bool(text),
                    }
                )
            output.append(
                {
                    "unit_id": row["comparison_id"],
                    "chapter": chapter,
                    "source": row["original"],
                    "source_context_before": (
                        rows[index - 1]["original"] if index else ""
                    ),
                    "source_context_after": (
                        rows[index + 1]["original"] if index + 1 < len(rows) else ""
                    ),
                    "alignment": row["alignment"],
                    "versions": versions,
                }
            )
    return output


def make_blind_pass(
    content_units: list[dict], pass_name: str, seed: str
) -> tuple[list[dict], list[dict]]:
    rng = random.Random(seed)
    cases = []
    keys = []
    for unit in content_units:
        versions = list(unit["versions"])
        rng.shuffle(versions)
        if len(versions) > len(LABELS):
            raise ValueError(f"too many versions at {unit['unit_id']}")
        labels = LABELS[: len(versions)]
        blind_id = hashlib.sha256(
            f"{pass_name}\0{seed}\0{unit['unit_id']}".encode("utf-8")
        ).hexdigest()[:20]
        cases.append(
            {
                "blind_id": blind_id,
                "source": unit["source"],
                "source_context_before": unit["source_context_before"],
                "source_context_after": unit["source_context_after"],
                "versions": [
                    {
                        "label": label,
                        "paragraphs": version["fragments"],
                        "is_missing": version["is_missing"],
                    }
                    for label, version in zip(labels, versions)
                ],
            }
        )
        keys.append(
            {
                "blind_id": blind_id,
                "unit_id": unit["unit_id"],
                "chapter": unit["chapter"],
                "labels": {
                    label: {
                        "version_id": version["version_id"],
                        "origin": version["origin"],
                        "members": version["members"],
                    }
                    for label, version in zip(labels, versions)
                },
            }
        )
    order = list(range(len(cases)))
    rng.shuffle(order)
    return [cases[i] for i in order], [keys[i] for i in order]


def reviewer_instructions() -> str:
    return """# Blind content review protocol

You are comparing anonymous Chinese translations of the same complete English source unit.

Rules:

1. Treat every `paragraphs` array as one complete translation. Paragraph count itself is neither a benefit nor a defect.
2. First split the English source into atomic propositions. Do not score Chinese style before checking content.
3. For every anonymous version and every proposition, record `preserved`, `partial`, `omitted`, or `distorted`, with Chinese evidence and a reason.
4. Record additions and other issues with severity: `minor`, `major`, or `critical`.
5. Only after fidelity, assess natural Simplified Chinese, logic/references, terminology, voice, and rhythm.
6. Rank every label. Tied labels share one ranking group. Use `both_flawed` in the overall assessment when no version is acceptable.
7. Do not guess or discuss which version came from which source. Do not access files outside the assigned blind directory.
8. A result without concrete evidence and reasons is invalid.

Result JSON schema (one JSON object per line):

```json
{
  "blind_id": "from case",
  "propositions": [{"id": "P1", "text": "source proposition"}],
  "versions": {
    "A": {
      "coverage": [{"proposition_id": "P1", "status": "preserved", "evidence": "quoted Chinese", "reason": "why"}],
      "additions": [{"evidence": "quoted Chinese", "severity": "minor", "reason": "why"}],
      "issues": [{"category": "naturalness", "evidence": "quoted Chinese", "severity": "minor", "reason": "why"}],
      "worst_severity": "minor",
      "dimensions": {
        "naturalness": {"rating": "strong", "evidence": "quoted Chinese", "reason": "why"},
        "logic_references": {"rating": "strong", "evidence": "quoted Chinese", "reason": "why"},
        "terminology": {"rating": "acceptable", "evidence": "quoted Chinese", "reason": "why"},
        "voice_rhythm": {"rating": "strong", "evidence": "quoted Chinese", "reason": "why"}
      },
      "summary": "version-specific assessment"
    }
  },
  "ranking": [["A"], ["B", "C"]],
  "verdict": "winner",
  "overall_assessment": "A is better because ...",
  "confidence": "high",
  "needs_cross_unit_context": false
}
```
"""


def readme_text(manifest: dict) -> str:
    return f"""# Chapter 1–2 blind comparison pilot

This pilot contains {manifest['english_content_units']} English content units and
{manifest['team_distinct_candidates']} distinct team translations. Every case presents
our complete translation and all team translations as anonymous, randomly ordered
versions. Target paragraph boundaries are preserved inside each version.

## Isolation rules

- Pass A and Pass B must be reviewed in two fresh, independent contexts.
- Reviewers may read only `blind/REVIEW_INSTRUCTIONS.md` and their assigned case file.
- Reviewers must not read `private/`, the old `comparison/` directory, or the other pass.
- Write results to `results/pass_a.jsonl` or `results/pass_b.jsonl` respectively.
- Do not unblind or aggregate until both result files are complete and validated.

## Validation

```sh
python3 scripts/blind_translation_comparison.py validate-cases \\
  --output translation_preview/comparison_v2_test_ch1_ch2

python3 scripts/blind_translation_comparison.py validate-results \\
  --output translation_preview/comparison_v2_test_ch1_ch2 --pass-name pass_a

python3 scripts/blind_translation_comparison.py validate-results \\
  --output translation_preview/comparison_v2_test_ch1_ch2 --pass-name pass_b
```

The existing v1 judgments are retained separately as a provisional pilot and are not
included in this corpus or its final statistics.
"""


def build(args: argparse.Namespace) -> int:
    if args.output.exists():
        if not args.force:
            raise FileExistsError(f"output exists: {args.output}")
        shutil.rmtree(args.output)
    (args.output / "blind").mkdir(parents=True)
    (args.output / "private").mkdir()
    (args.output / "results").mkdir()

    source_rows = read_jsonl(args.source_units)
    chapters = set(args.chapters)
    content_units = build_content_units(source_rows, chapters)
    if not content_units:
        raise ValueError("no content units selected")
    write_jsonl(args.output / "private" / "content_units.jsonl", content_units)

    secret = secrets.token_hex(32)
    answer_keys = {}
    for pass_index, pass_name in enumerate(("pass_a", "pass_b"), start=1):
        seed = hashlib.sha256(f"{secret}:{pass_index}".encode("utf-8")).hexdigest()
        cases, keys = make_blind_pass(content_units, pass_name, seed)
        write_jsonl(args.output / "blind" / f"{pass_name}_cases.jsonl", cases)
        answer_keys[pass_name] = keys
        (args.output / "results" / f"{pass_name}.jsonl").write_text(
            "", encoding="utf-8"
        )

    (args.output / "private" / "answer_key.json").write_text(
        json.dumps(answer_keys, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    instructions_path = args.output / "blind" / "REVIEW_INSTRUCTIONS.md"
    instructions_path.write_text(reviewer_instructions(), encoding="utf-8")

    counts = Counter(len(unit["versions"]) - 1 for unit in content_units)
    manifest = {
        "schema_version": 2,
        "source_units_path": str(args.source_units.resolve()),
        "source_units_sha256": sha256(args.source_units),
        "chapters": sorted(chapters),
        "english_content_units": len(content_units),
        "team_distinct_candidates": sum(
            len(unit["versions"]) - 1 for unit in content_units
        ),
        "units_with_multiple_team_candidates": sum(
            len(unit["versions"]) > 2 for unit in content_units
        ),
        "team_candidate_distribution": dict(sorted(counts.items())),
        "blind_passes": 2,
        "secret_sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        "legacy_v1_results_included": False,
        "review_instructions_sha256": sha256(instructions_path),
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output / "README.md").write_text(readme_text(manifest), encoding="utf-8")
    print(
        f"Built {len(content_units)} blind content units with "
        f"{manifest['team_distinct_candidates']} team candidates"
    )
    return validate_cases(args.output)


def validate_cases(output: Path) -> int:
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    answer_keys = json.loads(
        (output / "private" / "answer_key.json").read_text(encoding="utf-8")
    )
    pass_maps = {}
    for pass_name in ("pass_a", "pass_b"):
        cases = read_jsonl(output / "blind" / f"{pass_name}_cases.jsonl")
        keys = answer_keys[pass_name]
        if len(cases) != manifest["english_content_units"] or len(keys) != len(cases):
            raise ValueError(f"case count mismatch in {pass_name}")
        case_ids = [case["blind_id"] for case in cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError(f"duplicate blind IDs in {pass_name}")
        keys_by_id = {key["blind_id"]: key for key in keys}
        if set(case_ids) != set(keys_by_id):
            raise ValueError(f"answer key mismatch in {pass_name}")
        unit_map = {}
        for case in cases:
            labels = [version["label"] for version in case["versions"]]
            key = keys_by_id[case["blind_id"]]
            if set(labels) != set(key["labels"]):
                raise ValueError(f"label mismatch at {case['blind_id']}")
            origins = [entry["origin"] for entry in key["labels"].values()]
            if origins.count("ours") != 1:
                raise ValueError(f"expected one ours version at {case['blind_id']}")
            if any(
                "origin" in version or "member" in version
                for version in case["versions"]
            ):
                raise ValueError(f"identity leaked at {case['blind_id']}")
            unit_map[key["unit_id"]] = {
                entry["version_id"] for entry in key["labels"].values()
            }
        pass_maps[pass_name] = unit_map
    if pass_maps["pass_a"] != pass_maps["pass_b"]:
        raise ValueError("blind passes do not contain identical underlying versions")
    print(
        f"Validated two blind passes × {manifest['english_content_units']} units; "
        "no explicit origin/member fields in blind cases"
    )
    return 0


def validate_results(output: Path, pass_name: str) -> int:
    cases = {
        case["blind_id"]: case
        for case in read_jsonl(output / "blind" / f"{pass_name}_cases.jsonl")
    }
    results = read_jsonl(output / "results" / f"{pass_name}.jsonl")
    seen = set()
    for result in results:
        blind_id = result.get("blind_id")
        if blind_id not in cases or blind_id in seen:
            raise ValueError(f"unknown or duplicate blind_id: {blind_id}")
        seen.add(blind_id)
        case = cases[blind_id]
        labels = {version["label"] for version in case["versions"]}
        propositions = result.get("propositions", [])
        proposition_ids = {item.get("id") for item in propositions}
        if (
            not propositions
            or None in proposition_ids
            or len(proposition_ids) != len(propositions)
        ):
            raise ValueError(f"invalid propositions at {blind_id}")
        if set(result.get("versions", {})) != labels:
            raise ValueError(f"version labels incomplete at {blind_id}")
        for label, assessment in result["versions"].items():
            coverage = assessment.get("coverage", [])
            if {item.get("proposition_id") for item in coverage} != proposition_ids:
                raise ValueError(f"coverage incomplete for {label} at {blind_id}")
            for item in coverage:
                if item.get("status") not in COVERAGE_STATUSES or not item.get(
                    "reason"
                ):
                    raise ValueError(f"invalid coverage for {label} at {blind_id}")
            for collection in ("additions", "issues"):
                for item in assessment.get(collection, []):
                    if (
                        item.get("severity") not in ERROR_SEVERITIES - {"none"}
                        or not item.get("evidence")
                        or not item.get("reason")
                    ):
                        raise ValueError(
                            f"invalid {collection} for {label} at {blind_id}"
                        )
            if assessment.get("worst_severity") not in ERROR_SEVERITIES:
                raise ValueError(f"invalid severity for {label} at {blind_id}")
            dimensions = assessment.get("dimensions", {})
            if set(dimensions) != REQUIRED_DIMENSIONS:
                raise ValueError(f"dimensions incomplete for {label} at {blind_id}")
            for dimension, item in dimensions.items():
                if item.get("rating") not in DIMENSION_RATINGS or not item.get(
                    "reason"
                ):
                    raise ValueError(f"invalid {dimension} for {label} at {blind_id}")
            if not assessment.get("summary"):
                raise ValueError(f"missing summary for {label} at {blind_id}")
        ranking = result.get("ranking", [])
        if not ranking or any(not group for group in ranking):
            raise ValueError(f"empty ranking group at {blind_id}")
        ranked = [label for group in ranking for label in group]
        if set(ranked) != labels or len(ranked) != len(labels):
            raise ValueError(f"ranking incomplete at {blind_id}")
        verdict = result.get("verdict")
        if verdict not in VERDICTS:
            raise ValueError(f"invalid verdict at {blind_id}")
        if verdict == "winner" and len(ranking[0]) != 1:
            raise ValueError(f"winner verdict requires one top label at {blind_id}")
        if verdict == "tie" and len(ranking[0]) < 2:
            raise ValueError(f"tie verdict requires multiple top labels at {blind_id}")
        if result.get("confidence") not in CONFIDENCES or not result.get(
            "overall_assessment"
        ):
            raise ValueError(f"missing conclusion at {blind_id}")
        if not isinstance(result.get("needs_cross_unit_context"), bool):
            raise ValueError(f"context flag missing at {blind_id}")
    print(f"Validated {len(results)}/{len(cases)} results for {pass_name}")
    return 0


def status(output: Path) -> int:
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    total = manifest["english_content_units"]
    print(f"Blind comparison pilot: {total} English content units per pass")
    for pass_name in ("pass_a", "pass_b"):
        results = read_jsonl(output / "results" / f"{pass_name}.jsonl")
        print(f"- {pass_name}: {len(results)}/{total} ({len(results) / total:.1%})")
    return 0


def result_by_unit(output: Path, pass_name: str) -> dict[str, dict]:
    keys = json.loads(
        (output / "private" / "answer_key.json").read_text(encoding="utf-8")
    )
    key_by_blind = {item["blind_id"]: item for item in keys[pass_name]}
    output_rows = {}
    for result in read_jsonl(output / "results" / f"{pass_name}.jsonl"):
        key = key_by_blind[result["blind_id"]]
        rank_by_version = {}
        for rank, group in enumerate(result["ranking"]):
            for label in group:
                rank_by_version[key["labels"][label]["version_id"]] = rank
        version_meta = {item["version_id"]: item for item in key["labels"].values()}
        top_versions = {
            version_id for version_id, rank in rank_by_version.items() if rank == 0
        }
        ours_id = next(
            version_id
            for version_id, item in version_meta.items()
            if item["origin"] == "ours"
        )
        team_ids = {
            version_id
            for version_id, item in version_meta.items()
            if item["origin"] != "ours"
        }
        best_team_rank = min(rank_by_version[version_id] for version_id in team_ids)
        ours_rank = rank_by_version[ours_id]
        unit_outcome = (
            "ours"
            if ours_rank < best_team_rank
            else "team" if ours_rank > best_team_rank else "tie"
        )
        output_rows[key["unit_id"]] = {
            "top_versions": top_versions,
            "rank_by_version": rank_by_version,
            "version_meta": version_meta,
            "ours_vs_best_team": unit_outcome,
            "verdict": result["verdict"],
            "confidence": result["confidence"],
        }
    return output_rows


def agreement(output: Path) -> int:
    validate_results(output, "pass_a")
    validate_results(output, "pass_b")
    pass_a = result_by_unit(output, "pass_a")
    pass_b = result_by_unit(output, "pass_b")
    overlapping = sorted(set(pass_a) & set(pass_b))
    rows = []
    pairwise_total = 0
    pairwise_agree = 0
    for unit_id in overlapping:
        a = pass_a[unit_id]
        b = pass_b[unit_id]
        version_ids = set(a["rank_by_version"])
        if version_ids != set(b["rank_by_version"]):
            raise ValueError(f"underlying versions differ at {unit_id}")
        ours_id = next(
            version_id
            for version_id, item in a["version_meta"].items()
            if item["origin"] == "ours"
        )
        team_ids = sorted(version_ids - {ours_id})
        pair_outcomes = []
        for team_id in team_ids:

            def compare(item: dict) -> str:
                ours_rank = item["rank_by_version"][ours_id]
                team_rank = item["rank_by_version"][team_id]
                return (
                    "ours"
                    if ours_rank < team_rank
                    else "team" if ours_rank > team_rank else "tie"
                )

            outcome_a = compare(a)
            outcome_b = compare(b)
            pairwise_total += 1
            pairwise_agree += outcome_a == outcome_b
            pair_outcomes.append(
                {
                    "team_version_id": team_id,
                    "pass_a": outcome_a,
                    "pass_b": outcome_b,
                    "agree": outcome_a == outcome_b,
                }
            )
        rows.append(
            {
                "unit_id": unit_id,
                "exact_top_agreement": a["top_versions"] == b["top_versions"],
                "ours_vs_best_team_pass_a": a["ours_vs_best_team"],
                "ours_vs_best_team_pass_b": b["ours_vs_best_team"],
                "ours_vs_best_team_agreement": a["ours_vs_best_team"]
                == b["ours_vs_best_team"],
                "pass_a_verdict": a["verdict"],
                "pass_b_verdict": b["verdict"],
                "candidate_pairwise": pair_outcomes,
            }
        )
    summary = {
        "overlapping_units": len(overlapping),
        "exact_top_agreement": sum(row["exact_top_agreement"] for row in rows),
        "ours_vs_best_team_agreement": sum(
            row["ours_vs_best_team_agreement"] for row in rows
        ),
        "candidate_pairwise_comparisons": pairwise_total,
        "candidate_pairwise_agreement": pairwise_agree,
        "units": rows,
    }
    reports = output / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "pilot_agreement.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Blind pilot agreement",
        "",
        f"- Overlapping English units: {len(overlapping)}",
        f"- Exact top-version agreement: {summary['exact_top_agreement']}/{len(overlapping)}",
        f"- Ours-vs-best-team outcome agreement: {summary['ours_vs_best_team_agreement']}/{len(overlapping)}",
        f"- Candidate pairwise agreement: {pairwise_agree}/{pairwise_total}",
        "",
        "This is a pipeline smoke test, not a reliable quality estimate; the overlap sample is intentionally small.",
        "",
        "| Unit | Top agreement | Ours vs best team A | Ours vs best team B |",
        "|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['unit_id']} | {'yes' if row['exact_top_agreement'] else 'no'} | "
            f"{row['ours_vs_best_team_pass_a']} | {row['ours_vs_best_team_pass_b']} |"
        )
    (reports / "pilot_agreement.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(
        f"Agreement on {len(overlapping)} overlapping units: "
        f"top={summary['exact_top_agreement']}, ours-vs-team={summary['ours_vs_best_team_agreement']}, "
        f"pairwise={pairwise_agree}/{pairwise_total}"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--source-units", required=True, type=Path)
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--chapters", nargs="+", required=True)
    build_parser.add_argument("--force", action="store_true")
    case_parser = subparsers.add_parser("validate-cases")
    case_parser.add_argument("--output", required=True, type=Path)
    result_parser = subparsers.add_parser("validate-results")
    result_parser.add_argument("--output", required=True, type=Path)
    result_parser.add_argument(
        "--pass-name", choices=("pass_a", "pass_b"), required=True
    )
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--output", required=True, type=Path)
    agreement_parser = subparsers.add_parser("agreement")
    agreement_parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "build":
            return build(args)
        if args.command == "validate-cases":
            return validate_cases(args.output)
        if args.command == "validate-results":
            return validate_results(args.output, args.pass_name)
        if args.command == "status":
            return status(args.output)
        return agreement(args.output)
    except (FileExistsError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
