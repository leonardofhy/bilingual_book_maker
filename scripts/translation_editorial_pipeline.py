#!/usr/bin/env python3
"""Build, validate, and report an auditable translation editorial pilot.

The pipeline turns blind candidate comparisons into a final-translation workflow:
source brief -> anonymous candidate audit -> synthesis -> fidelity/native checks.
It never edits the source EPUB or ParaTranz export.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path


UNIT_TYPES = {"substantive", "heading", "speaker_label"}
OUTCOMES = {"selected", "edited", "synthesized", "retranslated"}
CONFIDENCES = {"high", "medium", "low"}
CHECK_STATUSES = {"pass", "revise"}
NATIVE_READ_CRITERIA = {
    "chinese_information_flow",
    "content_examples_preserved",
    "idiomatic_expression",
    "sentence_restructuring",
    "voice_rhythm",
}


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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def term_is_present(source: str, term: str) -> bool:
    source_norm = normalize(source)
    term_norm = normalize(term)
    if not term_norm:
        return False
    if re.fullmatch(r"[a-z0-9 -]+", term_norm):
        return bool(
            re.search(rf"(?<![a-z0-9]){re.escape(term_norm)}(?![a-z0-9])", source_norm)
        )
    return term_norm in source_norm


def classify_unit(source: str) -> str:
    stripped = source.strip()
    if re.fullmatch(r"[A-Z][A-Z ]*:", stripped):
        return "speaker_label"
    if stripped.casefold() in {"footnotes", "notes", "endnotes"}:
        return "heading"
    if len(stripped.split()) <= 5 and stripped.upper() == stripped:
        return "heading"
    return "substantive"


def matched_terms(source: str, term_entries: list[dict]) -> list[dict]:
    matches = []
    for entry_index, entry in enumerate(term_entries):
        found = [
            term
            for term in entry.get("source_terms", [])
            if term_is_present(source, term)
        ]
        if not found:
            continue
        matches.append(
            {
                "entry_index": entry_index,
                "matched_source_terms": found,
                "translation": entry.get("translation", ""),
                "note": entry.get("note", ""),
                "status": "candidate_unverified",
            }
        )
    return matches


def reviewer_profile() -> dict:
    return {
        "profile_version": 2,
        "role": "资深简体中文科普图书编辑",
        "audience": "中国大陆普通读者，同时不牺牲技术含义",
        "language": "简体中文",
        "publishing_norm": "中国大陆图书标点与技术术语规范",
        "priorities": [
            "命题、否定、范围、因果、指代和语气完整",
            "中文成稿必须像原生中文写作；忠实但保留英语句法仍须返工",
            "允许拆句、合句、重排信息、改变语法主语和改写惯用语",
            "保留克制、反讽、节奏和角色口吻",
            "只本地化表达，不替换作者的人物、地点、制度、类比或例子",
            "遵守已定核心术语；未定团队术语不得用于处罚候选",
            "不因候选来源或段落数量产生偏见",
        ],
        "hard_gates": [
            "无 critical 或 major 忠实度错误",
            "核心命题无遗漏或歪曲",
            "required 术语一致",
            "简体字及大陆标点合规",
            "通过中文信息流、句式重组、惯用表达和朗读节奏检查",
            "作者的例子和类比未被本地化替换",
        ],
        "allowed_restructuring": [
            "拆分或合并句子",
            "重排分句和信息出现顺序",
            "改变语法主语或改用话题结构",
            "以中性中文惯用表达替换英语惯用语",
        ],
        "content_localization_policy": {
            "default": "保留作者的例子、人物、地点、制度和类比",
            "opaque_reference": "优先在句内简短说明；仍无法消化时标记人工复核",
            "example_substitution": "禁止，除非作为独立改编并获得明确批准",
        },
        "house_terms": {
            "AI": "AI／人工智能（依语境）",
            "intelligence": "智能",
            "prediction / predicting": "预测",
            "steering": "操控",
            "hominid-god": "人猿之神",
            "grown, not crafted": "是长出来的，不是打造出来的",
            "LLM": "LLM",
        },
        "source_of_truth": "translation_preview/STYLE_GUIDE.md",
    }


def build(args: argparse.Namespace) -> int:
    comparison = args.comparison
    output = args.output
    if output.exists():
        if not args.force:
            raise FileExistsError(f"output exists: {output}")
        shutil.rmtree(output)
    for directory in ("config", "input", "private", "evidence", "stages", "reports"):
        (output / directory).mkdir(parents=True, exist_ok=True)

    agreement = read_json(comparison / "reports" / "pilot_agreement.json")
    selected_ids = [row["unit_id"] for row in agreement["units"]]
    units = {
        row["unit_id"]: row
        for row in read_jsonl(comparison / "private" / "content_units.jsonl")
    }
    missing = set(selected_ids) - set(units)
    if missing:
        raise ValueError(f"units absent from content corpus: {sorted(missing)}")

    terms_doc = read_json(args.terms)
    term_entries = terms_doc.get("entries", [])
    cases = []
    key_rows = []
    for unit_id in selected_ids:
        unit = units[unit_id]
        ordered = sorted(unit["versions"], key=lambda item: item["version_id"])
        labels = [f"V{index:02d}" for index in range(1, len(ordered) + 1)]
        cases.append(
            {
                "unit_id": unit_id,
                "unit_type": classify_unit(unit["source"]),
                "source": unit["source"],
                "source_context_before": unit["source_context_before"],
                "source_context_after": unit["source_context_after"],
                "term_candidates": matched_terms(unit["source"], term_entries),
                "versions": [
                    {
                        "label": label,
                        "paragraphs": version["fragments"],
                        "is_missing": version["is_missing"],
                    }
                    for label, version in zip(labels, ordered)
                ],
            }
        )
        key_rows.append(
            {
                "unit_id": unit_id,
                "labels": {
                    label: {
                        "version_id": version["version_id"],
                        "origin": version["origin"],
                        "members": version["members"],
                    }
                    for label, version in zip(labels, ordered)
                },
            }
        )

    write_json(output / "config" / "reviewer_profile.json", reviewer_profile())
    write_jsonl(output / "input" / "cases.jsonl", cases)
    write_json(output / "private" / "candidate_key.json", key_rows)
    for name in ("source_briefs", "candidate_audits", "finals", "verification"):
        (output / "stages" / f"{name}.jsonl").write_text("", encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "purpose": "final-translation editorial pilot, not winner counting",
        "unit_count": len(cases),
        "unit_ids": selected_ids,
        "unit_type_counts": {
            unit_type: sum(case["unit_type"] == unit_type for case in cases)
            for unit_type in sorted(UNIT_TYPES)
        },
        "comparison_manifest_sha256": sha256(comparison / "manifest.json"),
        "agreement_sha256": sha256(comparison / "reports" / "pilot_agreement.json"),
        "terms_sha256": sha256(args.terms),
        "style_guide_sha256": sha256(args.style_guide),
        "candidate_identity_hidden_in_input": True,
        "legacy_v1_used_for_final_decisions": False,
    }
    write_json(output / "manifest.json", manifest)
    (output / "README.md").write_text(
        "# Ten-unit editorial pilot\n\n"
        "This package converts anonymous comparisons into final translations. "
        "The stages are:\n\n"
        "1. `source_briefs.jsonl`: freeze source meaning before editorial selection.\n"
        "2. `candidate_audits.jsonl`: review anonymous candidates and shortlist "
        "evidence.\n"
        "3. `finals.jsonl`: select, edit, synthesize, or retranslate.\n"
        "4. `verification.jsonl`: fidelity check plus Chinese-only native-reading "
        "check.\n\n"
        "The private key is used only after decisions for provenance reporting.\n",
        encoding="utf-8",
    )
    print(f"Built editorial pilot with {len(cases)} units at {output}")
    return 0


def require_ids(rows: list[dict], expected: set[str], stage: str) -> None:
    ids = [row.get("unit_id") for row in rows]
    if None in ids or len(ids) != len(set(ids)):
        raise ValueError(f"{stage}: missing or duplicate unit_id")
    if set(ids) != expected:
        raise ValueError(f"{stage}: expected {len(expected)} units, got {len(ids)}")


def validate(args: argparse.Namespace) -> int:
    output = args.output
    manifest = read_json(output / "manifest.json")
    profile = read_json(output / "config" / "reviewer_profile.json")
    expected = set(manifest["unit_ids"])
    cases = read_jsonl(output / "input" / "cases.jsonl")
    require_ids(cases, expected, "cases")

    briefs = read_jsonl(output / "stages" / "source_briefs.jsonl")
    audits = read_jsonl(output / "stages" / "candidate_audits.jsonl")
    finals = read_jsonl(output / "stages" / "finals.jsonl")
    checks = read_jsonl(output / "stages" / "verification.jsonl")
    for rows, name in (
        (briefs, "source_briefs"),
        (audits, "candidate_audits"),
        (finals, "finals"),
        (checks, "verification"),
    ):
        require_ids(rows, expected, name)

    case_by_id = {case["unit_id"]: case for case in cases}
    for brief in briefs:
        if brief.get("unit_type") not in UNIT_TYPES:
            raise ValueError(f"invalid unit type at {brief['unit_id']}")
        if not brief.get("propositions") or not brief.get("constraints"):
            raise ValueError(f"incomplete source brief at {brief['unit_id']}")
    for audit in audits:
        labels = {
            version["label"] for version in case_by_id[audit["unit_id"]]["versions"]
        }
        if set(audit.get("candidates", {})) != labels:
            raise ValueError(f"candidate audit incomplete at {audit['unit_id']}")
        shortlist = audit.get("shortlist", [])
        if not shortlist or not set(shortlist) <= labels:
            raise ValueError(f"invalid shortlist at {audit['unit_id']}")
    for final in finals:
        if final.get("outcome") not in OUTCOMES or not final.get("final_translation"):
            raise ValueError(f"invalid final at {final['unit_id']}")
        if final.get("confidence") not in CONFIDENCES:
            raise ValueError(f"invalid final confidence at {final['unit_id']}")
    for check in checks:
        for name in ("fidelity", "native_read"):
            item = check.get(name, {})
            if item.get("status") not in CHECK_STATUSES or "issues" not in item:
                raise ValueError(f"invalid {name} check at {check['unit_id']}")
        if profile.get("profile_version", 1) >= 2:
            native_read = check["native_read"]
            criteria = native_read.get("criteria", {})
            if set(criteria) != NATIVE_READ_CRITERIA:
                raise ValueError(
                    f"native-read criteria incomplete at {check['unit_id']}"
                )
            for criterion, assessment in criteria.items():
                if assessment.get("status") not in CHECK_STATUSES:
                    raise ValueError(
                        f"invalid {criterion} status at {check['unit_id']}"
                    )
                if not assessment.get("evidence") or not assessment.get("reason"):
                    raise ValueError(
                        f"unsupported {criterion} assessment at {check['unit_id']}"
                    )
            criteria_status = (
                "pass"
                if all(item["status"] == "pass" for item in criteria.values())
                else "revise"
            )
            if native_read["status"] != criteria_status:
                raise ValueError(f"native-read status mismatch at {check['unit_id']}")
        expected_status = (
            "verified"
            if check["fidelity"]["status"] == check["native_read"]["status"] == "pass"
            else "revise"
        )
        if check.get("final_status") != expected_status:
            raise ValueError(f"final status mismatch at {check['unit_id']}")
    print(f"Validated all four stages for {len(expected)} editorial units")
    return 0


def report(args: argparse.Namespace) -> int:
    validate(args)
    output = args.output
    cases = {
        row["unit_id"]: row for row in read_jsonl(output / "input" / "cases.jsonl")
    }
    finals = {
        row["unit_id"]: row for row in read_jsonl(output / "stages" / "finals.jsonl")
    }
    checks = {
        row["unit_id"]: row
        for row in read_jsonl(output / "stages" / "verification.jsonl")
    }
    briefs = {
        row["unit_id"]: row
        for row in read_jsonl(output / "stages" / "source_briefs.jsonl")
    }
    audits = {
        row["unit_id"]: row
        for row in read_jsonl(output / "stages" / "candidate_audits.jsonl")
    }
    key = {
        row["unit_id"]: row
        for row in read_json(output / "private" / "candidate_key.json")
    }
    outcome_counts = {
        outcome: sum(row["outcome"] == outcome for row in finals.values())
        for outcome in sorted(OUTCOMES)
    }
    lines = [
        "# 10 笔最终译文报告",
        "",
        "本报告记录编辑决策，不是胜率表。" "候选审计沿用两次独立盲评的证据；",
        "本轮最终译文的忠实度与中文顺读检查则由同一工作上下文"
        "分阶段完成，尚不等于独立终审。",
        "",
        "- 直接采用：{selected}",
        "- 局部编辑：{edited}",
        "- 多版综合：{synthesized}",
        "- 全新重译：{retranslated}",
        "",
    ]
    lines = [line.format(**outcome_counts) if "{" in line else line for line in lines]
    for unit_id in read_json(output / "manifest.json")["unit_ids"]:
        final = finals[unit_id]
        label_origins = {
            label: meta["origin"] for label, meta in key[unit_id]["labels"].items()
        }
        provenance = [
            f"{label} ({label_origins.get(label, 'unknown')})"
            for label in final.get("base_labels", [])
        ]
        independence = checks[unit_id]["independence_status"]
        provenance_text = ", ".join(provenance) if provenance else "new translation"
        lines.extend(
            [
                f"## {unit_id}",
                "",
                f"- Type: {cases[unit_id]['unit_type']}",
                f"- Outcome: {final['outcome']}",
                f"- Confidence: {final['confidence']}",
                f"- Verification: {checks[unit_id]['final_status']}",
                f"- Verification independence: {independence}",
                f"- Provenance: {provenance_text}",
                "",
                f"> {final['final_translation']}",
                "",
                f"Rationale: {final['rationale']}",
                "",
            ]
        )
    report_path = output / "reports" / "final_translations.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    audit_lines = [
        "# 10 笔候选译文完整审计",
        "",
        "候选标签在审计阶段匿名；下列来源信息由定稿后私钥反查，",
        "仅用于追踪，不参与评分。",
        "",
    ]
    for unit_id in read_json(output / "manifest.json")["unit_ids"]:
        case = cases[unit_id]
        brief = briefs[unit_id]
        audit = audits[unit_id]
        version_by_label = {version["label"]: version for version in case["versions"]}
        audit_lines.extend(
            [
                f"## {unit_id}",
                "",
                f"Source: {case['source']}",
                "",
                "Source propositions:",
                "",
            ]
        )
        for proposition in brief["propositions"]:
            audit_lines.append(f"- {proposition['id']}: {proposition['text']}")
        audit_lines.extend(["", "Constraints:", ""])
        audit_lines.extend(f"- {constraint}" for constraint in brief["constraints"])
        audit_lines.extend(["", "Candidate audits:", ""])
        for label, candidate_audit in audit["candidates"].items():
            origin = key[unit_id]["labels"][label]["origin"]
            translation = "\n> ".join(
                version_by_label[label]["paragraphs"] or ["[MISSING]"]
            )
            audit_lines.extend(
                [
                    f"### {label} ({origin}) — {candidate_audit['decision']}",
                    "",
                    f"> {translation}",
                    "",
                    "Strengths:",
                    "",
                ]
            )
            strengths = candidate_audit["strengths"] or ["无"]
            audit_lines.extend(f"- {strength}" for strength in strengths)
            audit_lines.extend(["", "Errors:", ""])
            errors = candidate_audit["errors"]
            if errors:
                audit_lines.extend(
                    f"- [{error['severity']}/{error['category']}] "
                    f"{error['evidence']}：{error['reason']}"
                    for error in errors
                )
            else:
                audit_lines.append("- 无")
            audit_lines.append("")
        audit_lines.extend(
            [
                f"Shortlist: {', '.join(audit['shortlist'])}",
                "",
                f"Synthesis guidance: {audit['synthesis_guidance']}",
                "",
            ]
        )
    audit_path = output / "reports" / "candidate_audits.md"
    audit_path.write_text("\n".join(audit_lines), encoding="utf-8")
    print(f"Wrote {report_path}")
    print(f"Wrote {audit_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--comparison", required=True, type=Path)
    build_parser.add_argument("--terms", required=True, type=Path)
    build_parser.add_argument("--style-guide", required=True, type=Path)
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--force", action="store_true")
    for name in ("validate", "report"):
        command = commands.add_parser(name)
        command.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "build":
            return build(args)
        if args.command == "validate":
            return validate(args)
        return report(args)
    except (FileExistsError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
