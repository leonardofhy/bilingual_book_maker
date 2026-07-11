#!/usr/bin/env python3
"""Build an immutable translation baseline directly from bilingual EPUB XHTML."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET


EPUB_NS = "http://www.idpf.org/2007/ops"
XML_NS = "http://www.w3.org/XML/1998/namespace"
BASELINE_VERSION = 2
SCHEMA_VERSION = 2
SPACE_RE = re.compile(r"\s+")
NUMBERED_NOTE_RE = re.compile(r"^(\d+)\.\s*")
REVIEW_HEADING_RE = re.compile(
    r"^###\s+\(?(\d+)\)?\s+\[(嚴重|建议|建議)\]", re.MULTILINE
)

SCOPES = (
    {
        "book_chapter": 11,
        "title": "AN ALCHEMY, NOT A SCIENCE",
        "document": "OEBPS/chapter012.xhtml",
        "section_id": "chapter012",
        "endnotes_document": "OEBPS/appendix003.xhtml",
        "endnotes_section_id": "a010",
        "expected_legacy_translations": 86,
        "expected_footnotes": 2,
        "expected_endnote_paragraphs": 23,
        "review2": "translation_preview/review2/chapter012.md",
        "review3": "translation_preview/review3_blind/chapter012.json",
        "legacy_terms": "translation_preview/review2/chapter012.terms.json",
    },
    {
        "book_chapter": 13,
        "title": "SHUT IT DOWN",
        "document": "OEBPS/chapter014.xhtml",
        "section_id": "chapter014",
        "endnotes_document": "OEBPS/appendix003.xhtml",
        "endnotes_section_id": "a012",
        "expected_legacy_translations": 61,
        "expected_footnotes": 6,
        "expected_endnote_paragraphs": 7,
        "review2": "translation_preview/review2/chapter014.md",
        "review3": "translation_preview/review3_blind/chapter014.json",
        "legacy_terms": "translation_preview/review2/chapter014.terms.json",
    },
)

NUMBERED_COMMENTARY_UNITS = {(13, "06")}


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
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalized_text(element: ET.Element) -> str:
    return SPACE_RE.sub(" ", "".join(element.itertext())).strip()


def normalized_match_text(text: str) -> str:
    return SPACE_RE.sub(" ", text).strip().casefold()


def language(element: ET.Element) -> str:
    return (element.get("lang") or element.get(f"{{{XML_NS}}}lang") or "").casefold()


def is_translation(element: ET.Element) -> bool:
    return language(element).startswith("zh")


def classes(element: ET.Element) -> set[str]:
    return set((element.get("class") or "").split())


def element_by_id(root: ET.Element, element_id: str) -> ET.Element:
    for element in root.iter():
        if element.get("id") == element_id:
            return element
    raise ValueError(f"element id not found: {element_id}")


def parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}


def next_sibling(
    element: ET.Element, parents: dict[ET.Element, ET.Element]
) -> ET.Element | None:
    parent = parents.get(element)
    if parent is None:
        return None
    siblings = list(parent)
    index = siblings.index(element)
    return siblings[index + 1] if index + 1 < len(siblings) else None


def element_path(element: ET.Element, parents: dict[ET.Element, ET.Element]) -> str:
    parts = []
    current = element
    while current in parents:
        parent = parents[current]
        same_tag = [
            child
            for child in parent
            if local_name(child.tag) == local_name(current.tag)
        ]
        parts.append(f"{local_name(current.tag)}[{same_tag.index(current) + 1}]")
        current = parent
    parts.append(local_name(current.tag))
    return "/" + "/".join(reversed(parts))


def serialized(element: ET.Element) -> str:
    return ET.tostring(element, encoding="unicode", method="xml")


def segment(segment_id: str, text: str, mode: str, kind: str) -> dict:
    return {
        "kind": kind,
        "mode": mode,
        "segment_id": segment_id,
        "source_sha256": text_sha256(text),
        "text": text,
    }


def source_segments(element: ET.Element, content_type: str, unit_id: str) -> list[dict]:
    source_text = normalized_text(element)
    if content_type == "url_marker":
        return [segment("S1", source_text, "preserve_exact", "url")]
    if content_type == "footnote":
        label_element = next(
            (
                child
                for child in element.iter()
                if local_name(child.tag) == "a" and "backlink" in classes(child)
            ),
            None,
        )
        label = normalized_text(label_element) if label_element is not None else ""
        prefix = f"{label} " if label and source_text.startswith(f"{label} ") else ""
        if prefix:
            return [
                segment("S1", prefix, "preserve_exact", "footnote_label"),
                segment("S2", source_text[len(prefix) :], "translate", "prose"),
            ]
    if content_type in {"endnote_bibliography", "endnote_numbered_commentary"}:
        number_match = NUMBERED_NOTE_RE.match(source_text)
        locator_element = next(
            (child for child in element.iter() if local_name(child.tag) == "em"),
            None,
        )
        if number_match is None or locator_element is None:
            raise ValueError(f"cannot segment numbered endnote: {unit_id}")
        number = number_match.group(0)
        locator = normalized_text(locator_element)
        remainder_start = len(number)
        if not source_text[remainder_start:].startswith(locator):
            raise ValueError(f"endnote locator is not leading text: {unit_id}")
        remainder_start += len(locator)
        remainder = source_text[remainder_start:]
        remainder_mode = (
            "preserve_exact" if content_type == "endnote_bibliography" else "translate"
        )
        remainder_kind = (
            "citation" if content_type == "endnote_bibliography" else "commentary"
        )
        return [
            segment("S1", number, "preserve_exact", "note_number"),
            segment("S2", locator, "translate", "locator_phrase"),
            segment("S3", remainder, remainder_mode, remainder_kind),
        ]
    return [segment("S1", source_text, "translate", "prose")]


def inline_references(element: ET.Element) -> list[dict]:
    references = []
    for child in element.iter():
        if local_name(child.tag) != "a" or "backlink" in classes(child):
            continue
        marker = normalized_text(child)
        href = child.get("href", "")
        anchor_id = child.get("id", "")
        has_superscript = any(
            local_name(descendant.tag) == "sup" for descendant in child.iter()
        )
        if marker and href and anchor_id and has_superscript:
            references.append(
                {
                    "anchor_id": anchor_id,
                    "href": href,
                    "marker": marker,
                }
            )
    return references


def parse_document(epub: zipfile.ZipFile, document: str) -> ET.Element:
    try:
        return ET.fromstring(epub.read(document))
    except KeyError as error:
        raise ValueError(f"EPUB document missing: {document}") from error


def term_present(source: str, term: str) -> bool:
    source_norm = normalized_match_text(source)
    term_norm = normalized_match_text(term)
    if not term_norm:
        return False
    if re.fullmatch(r"[a-z0-9 -]+", term_norm):
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(term_norm)}(?![a-z0-9])",
                source_norm,
            )
        )
    return term_norm in source_norm


def classify_chapter_node(element: ET.Element) -> tuple[str, str]:
    tag = local_name(element.tag)
    node_classes = classes(element)
    if tag.startswith("h"):
        if "chapter-number" in node_classes:
            return "chapter_number", "heading.number"
        if "chapter-titlefn" in node_classes:
            return "footnotes_heading", "heading.footnotes"
        if "chapter-title" in node_classes:
            return "chapter_title", "heading.title"
        return "heading", "heading.other"
    if "footnote" in node_classes:
        label = normalized_text(element).split(" ", 1)[0].casefold()
        return "footnote", f"footnote.{label}"
    if "captionb" in node_classes:
        return "url_marker", "url"
    return "body", "body"


def make_source_row(
    *,
    unit_id: str,
    book_chapter: int,
    document: str,
    context_group: str,
    content_type: str,
    source_order: int,
    element: ET.Element,
    requires_translation: bool,
) -> dict:
    source_text = normalized_text(element)
    source_html = serialized(element)
    return {
        "book_chapter": book_chapter,
        "content_type": content_type,
        "context_after": "",
        "context_before": "",
        "context_group": context_group,
        "document": document,
        "inline_references": inline_references(element),
        "policy_key": content_type,
        "requires_translation": requires_translation,
        "source_html": source_html,
        "source_html_sha256": text_sha256(source_html),
        "source_order": source_order,
        "source_sha256": text_sha256(source_text),
        "source_segments": source_segments(element, content_type, unit_id),
        "source_text": source_text,
        "unit_id": unit_id,
    }


def make_translation_row(
    unit_id: str,
    translation: ET.Element | None,
    origin: str,
    alignment_status: str = "adjacent_sibling",
) -> dict:
    if translation is None:
        return {
            "alignment_status": "missing",
            "origin": origin,
            "status": "missing",
            "translation": "",
            "translation_html": "",
            "translation_sha256": "",
            "unit_id": unit_id,
        }
    text = normalized_text(translation)
    return {
        "alignment_status": alignment_status,
        "origin": origin,
        "status": "present",
        "translation": text,
        "translation_html": serialized(translation),
        "translation_sha256": text_sha256(text),
        "unit_id": unit_id,
    }


def translation_structure_compatible(
    source: ET.Element, translation: ET.Element, content_type: str
) -> tuple[bool, str]:
    source_tag = local_name(source.tag)
    translation_tag = local_name(translation.tag)
    if source_tag.startswith("h"):
        if translation_tag not in {source_tag, "p"}:
            return False, f"heading translated as incompatible {translation_tag}"
    elif source_tag != translation_tag:
        return False, f"{source_tag} translated as {translation_tag}"
    required_classes = {
        "footnote": {"footnote"},
        "url_marker": {"captionb"},
    }.get(content_type, set())
    if not required_classes <= classes(translation):
        return False, f"missing structural classes: {sorted(required_classes)}"
    return True, ""


def alignment_graph(
    *,
    source_count: int,
    translation_nodes: list[ET.Element],
    consumed: set[ET.Element],
    ambiguous: list[dict],
    parents: dict[ET.Element, ET.Element],
) -> dict:
    orphan_nodes = [node for node in translation_nodes if node not in consumed]
    return {
        "ambiguous": ambiguous,
        "ambiguous_count": len(ambiguous),
        "consumed_translation_count": len(consumed),
        "orphan_translation_count": len(orphan_nodes),
        "orphan_translation_paths": [
            element_path(node, parents) for node in orphan_nodes
        ],
        "source_count": source_count,
        "translation_node_count": len(translation_nodes),
    }


def extract_chapter(
    epub: zipfile.ZipFile, scope: dict, origin: str
) -> tuple[list[dict], list[dict], list[dict], dict]:
    root = parse_document(epub, scope["document"])
    section = element_by_id(root, scope["section_id"])
    parents = parent_map(root)
    source_rows = []
    translation_rows = []
    provenance_rows = []
    counters = Counter()
    legacy_translation_index = 0
    consumed_translations: set[ET.Element] = set()
    ambiguous_alignments = []
    translation_nodes = [
        element
        for element in section.iter()
        if is_translation(element) and normalized_text(element)
    ]
    selected = [
        element
        for element in section.iter()
        if local_name(element.tag) in {"h1", "h2", "h3", "p"}
        and normalized_text(element)
        and not is_translation(element)
    ]
    for source_order, element in enumerate(selected, start=1):
        content_type, suffix_base = classify_chapter_node(element)
        counters[suffix_base] += 1
        if suffix_base in {"heading.number", "heading.title", "heading.footnotes"}:
            suffix = suffix_base
        elif suffix_base.startswith("footnote."):
            suffix = suffix_base
        else:
            suffix = f"{suffix_base}.{counters[suffix_base]:04d}"
        unit_id = f"book_ch{scope['book_chapter']:02d}.{suffix}"
        sibling = next_sibling(element, parents)
        translation = (
            sibling if sibling is not None and is_translation(sibling) else None
        )
        alignment_status = "adjacent_sibling"
        if translation is not None:
            compatible, reason = translation_structure_compatible(
                element, translation, content_type
            )
            if translation in consumed_translations:
                compatible = False
                reason = "translation node already consumed"
            consumed_translations.add(translation)
            if not compatible:
                alignment_status = "ambiguous_structure"
                ambiguous_alignments.append(
                    {
                        "reason": reason,
                        "translation_path": element_path(translation, parents),
                        "unit_id": unit_id,
                    }
                )
            legacy_translation_index += 1
        source_rows.append(
            make_source_row(
                unit_id=unit_id,
                book_chapter=scope["book_chapter"],
                document=scope["document"],
                context_group=f"book_ch{scope['book_chapter']:02d}.chapter",
                content_type=content_type,
                source_order=source_order,
                element=element,
                requires_translation=content_type != "url_marker",
            )
        )
        translation_rows.append(
            make_translation_row(
                unit_id,
                translation,
                origin,
                alignment_status=alignment_status,
            )
        )
        provenance_rows.append(
            {
                "document": scope["document"],
                "dom_path": element_path(element, parents),
                "element_id": element.get("id", ""),
                "legacy_translation_index": (
                    legacy_translation_index if translation is not None else None
                ),
                "unit_id": unit_id,
            }
        )

    footnote_nodes = [
        element
        for element in selected
        if classify_chapter_node(element)[0] == "footnote"
    ]
    footnote_ids = {
        element.get("id") for element in footnote_nodes if element.get("id")
    }
    reference_targets = set()
    broken_backlinks = []
    all_ids = {element.get("id") for element in root.iter() if element.get("id")}
    for element in section.iter():
        if local_name(element.tag) != "a" or is_translation(element):
            continue
        href = element.get("href", "")
        if href.startswith(f"{Path(scope['document']).name}#fn") and href.endswith(
            "fn"
        ):
            reference_targets.add(href.split("#", 1)[1])
    for footnote in footnote_nodes:
        for link in footnote.iter():
            if local_name(link.tag) != "a" or "backlink" not in classes(link):
                continue
            target = link.get("href", "").split("#", 1)[-1]
            if target not in all_ids:
                broken_backlinks.append(target)
    link_validation = {
        "backlinks_valid": not broken_backlinks,
        "broken_backlinks": sorted(broken_backlinks),
        "footnote_ids": sorted(footnote_ids),
        "reference_targets": sorted(reference_targets),
        "references_match_footnotes": reference_targets == footnote_ids,
    }
    validation = {
        "alignment": alignment_graph(
            source_count=len(selected),
            translation_nodes=translation_nodes,
            consumed=consumed_translations,
            ambiguous=ambiguous_alignments,
            parents=parents,
        ),
        "footnote_links": link_validation,
    }
    return source_rows, translation_rows, provenance_rows, validation


def extract_endnotes(
    epub: zipfile.ZipFile, scope: dict, origin: str
) -> tuple[list[dict], list[dict], list[dict], dict]:
    root = parse_document(epub, scope["endnotes_document"])
    heading = element_by_id(root, scope["endnotes_section_id"])
    parents = parent_map(root)
    parent = parents[heading]
    siblings = list(parent)
    start = siblings.index(heading)
    range_nodes = [heading]
    for element in siblings[start + 1 :]:
        if local_name(element.tag) == "h2":
            break
        range_nodes.append(element)
    selected = [
        element
        for element in range_nodes
        if local_name(element.tag) in {"h2", "p"}
        and normalized_text(element)
        and not is_translation(element)
    ]
    translation_nodes = [
        element
        for element in range_nodes
        if is_translation(element) and normalized_text(element)
    ]

    source_rows = []
    translation_rows = []
    provenance_rows = []
    consumed_translations: set[ET.Element] = set()
    ambiguous_alignments = []
    current_note = ""
    commentary_counts = Counter()
    for source_order, element in enumerate(selected, start=1):
        text = normalized_text(element)
        if element is heading:
            content_type = "endnotes_section_heading"
            suffix = "endnotes.heading"
        else:
            match = NUMBERED_NOTE_RE.match(text)
            if match:
                current_note = f"{int(match.group(1)):02d}"
                content_type = (
                    "endnote_numbered_commentary"
                    if (scope["book_chapter"], current_note)
                    in NUMBERED_COMMENTARY_UNITS
                    else "endnote_bibliography"
                )
                suffix = f"endnote.{current_note}"
            else:
                if not current_note:
                    raise ValueError(
                        f"endnote commentary precedes numbered note in chapter "
                        f"{scope['book_chapter']}"
                    )
                is_quote = "indentia1b" in classes(element)
                kind = "quotation" if is_quote else "commentary"
                commentary_counts[(current_note, kind)] += 1
                content_type = f"endnote_{kind}"
                suffix = (
                    f"endnote.{current_note}.{kind}."
                    f"{commentary_counts[(current_note, kind)]:02d}"
                )
        unit_id = f"book_ch{scope['book_chapter']:02d}.{suffix}"
        sibling = next_sibling(element, parents)
        translation = (
            sibling if sibling is not None and is_translation(sibling) else None
        )
        alignment_status = "adjacent_sibling"
        if translation is not None:
            compatible, reason = translation_structure_compatible(
                element, translation, content_type
            )
            if translation in consumed_translations:
                compatible = False
                reason = "translation node already consumed"
            consumed_translations.add(translation)
            if not compatible:
                alignment_status = "ambiguous_structure"
                ambiguous_alignments.append(
                    {
                        "reason": reason,
                        "translation_path": element_path(translation, parents),
                        "unit_id": unit_id,
                    }
                )
        source_rows.append(
            make_source_row(
                unit_id=unit_id,
                book_chapter=scope["book_chapter"],
                document=scope["endnotes_document"],
                context_group=f"book_ch{scope['book_chapter']:02d}.endnotes",
                content_type=content_type,
                source_order=source_order,
                element=element,
                requires_translation=True,
            )
        )
        translation_rows.append(
            make_translation_row(
                unit_id,
                translation,
                origin,
                alignment_status=alignment_status,
            )
        )
        provenance_rows.append(
            {
                "document": scope["endnotes_document"],
                "dom_path": element_path(element, parents),
                "element_id": element.get("id", ""),
                "legacy_translation_index": None,
                "unit_id": unit_id,
            }
        )
    validation = alignment_graph(
        source_count=len(selected),
        translation_nodes=translation_nodes,
        consumed=consumed_translations,
        ambiguous=ambiguous_alignments,
        parents=parents,
    )
    return source_rows, translation_rows, provenance_rows, validation


def add_context(rows: list[dict]) -> None:
    by_group: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_group[row["context_group"]].append(row)
    for group_rows in by_group.values():
        group_rows.sort(key=lambda row: row["source_order"])
        for index, row in enumerate(group_rows):
            row["context_before"] = (
                group_rows[index - 1]["source_text"] if index else ""
            )
            row["context_after"] = (
                group_rows[index + 1]["source_text"]
                if index + 1 < len(group_rows)
                else ""
            )


def extract_all(
    epub_path: Path, origin: str
) -> tuple[list[dict], list[dict], list[dict], dict]:
    source_rows = []
    translation_rows = []
    provenance_rows = []
    links = {}
    alignments = {}
    with zipfile.ZipFile(epub_path) as epub:
        for scope in SCOPES:
            (
                chapter_source,
                chapter_translation,
                chapter_provenance,
                chapter_validation,
            ) = extract_chapter(epub, scope, origin)
            (
                end_source,
                end_translation,
                end_provenance,
                endnote_alignment,
            ) = extract_endnotes(epub, scope, origin)
            source_rows.extend(chapter_source)
            source_rows.extend(end_source)
            translation_rows.extend(chapter_translation)
            translation_rows.extend(end_translation)
            provenance_rows.extend(chapter_provenance)
            provenance_rows.extend(end_provenance)
            chapter_key = str(scope["book_chapter"])
            links[chapter_key] = chapter_validation["footnote_links"]
            alignments[chapter_key] = {
                "chapter": chapter_validation["alignment"],
                "endnotes": endnote_alignment,
            }
    add_context(source_rows)
    validation = {
        "alignment_graph": alignments,
        "footnote_links": links,
    }
    return source_rows, translation_rows, provenance_rows, validation


def compare_sources(primary: list[dict], reference: list[dict]) -> dict:
    primary_map = {row["unit_id"]: row for row in primary}
    reference_map = {row["unit_id"]: row for row in reference}
    text_mismatches = []
    html_mismatches = []
    for unit_id in sorted(set(primary_map) | set(reference_map)):
        primary_row = primary_map.get(unit_id, {})
        reference_row = reference_map.get(unit_id, {})
        if primary_row.get("source_text") != reference_row.get("source_text"):
            text_mismatches.append(unit_id)
        if primary_row.get("source_html") != reference_row.get("source_html"):
            html_mismatches.append(unit_id)
    mismatches = sorted(set(text_mismatches) | set(html_mismatches))
    return {
        "identical": not mismatches,
        "html_mismatched_unit_ids": html_mismatches,
        "mismatched_unit_ids": mismatches,
        "primary_unit_count": len(primary_map),
        "reference_unit_count": len(reference_map),
        "text_mismatched_unit_ids": text_mismatches,
    }


def legacy_index_map(provenance_rows: list[dict], chapter: int) -> dict[int, str]:
    prefix = f"book_ch{chapter:02d}."
    return {
        row["legacy_translation_index"]: row["unit_id"]
        for row in provenance_rows
        if row["unit_id"].startswith(prefix)
        and row["legacy_translation_index"] is not None
    }


def parse_review2(path: Path, chapter: int, provenance_rows: list[dict]) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    matches = list(REVIEW_HEADING_RE.finditer(text))
    index_map = legacy_index_map(provenance_rows, chapter)
    rows = []
    for position, match in enumerate(matches):
        end = (
            matches[position + 1].start() if position + 1 < len(matches) else len(text)
        )
        paragraph_index = int(match.group(1))
        unit_id = index_map.get(paragraph_index)
        rows.append(
            {
                "book_chapter": chapter,
                "mapping_assumption": None,
                "mapping_confidence": "high" if unit_id else "unresolved",
                "mapping_method": (
                    "legacy_translation_index" if unit_id else "unresolved"
                ),
                "mapping_score": 1.0 if unit_id else None,
                "reported_severity": (
                    "major" if match.group(2) == "嚴重" else "suggestion"
                ),
                "review_payload": text[match.start() : end].strip(),
                "runner_up_score": 0.0 if unit_id else None,
                "score_margin": 1.0 if unit_id else None,
                "source_review": str(path),
                "source_review_index": paragraph_index,
                "status": "unconfirmed",
                "unit_id": unit_id,
            }
        )
    return rows


def best_translation_match(
    needle: str, translation_rows: list[dict], chapter: int
) -> tuple[str | None, str, float | None, float | None, float | None]:
    needle_norm = normalized_match_text(needle)
    candidates = [
        row
        for row in translation_rows
        if row["unit_id"].startswith(f"book_ch{chapter:02d}.")
        and row["status"] == "present"
    ]
    exact = [
        row
        for row in candidates
        if needle_norm in normalized_match_text(row["translation"])
    ]
    if len(exact) == 1:
        return exact[0]["unit_id"], "translation_text_substring", 1.0, 0.0, 1.0
    scored = []
    for row in candidates:
        candidate = normalized_match_text(row["translation"])
        window = candidate[: max(len(needle_norm) * 2, len(needle_norm))]
        ratio = difflib.SequenceMatcher(None, needle_norm, window).ratio()
        scored.append((ratio, row["unit_id"]))
    scored.sort(reverse=True)
    top_score = scored[0][0] if scored else None
    runner_up_score = scored[1][0] if len(scored) > 1 else 0.0 if scored else None
    margin = (
        top_score - runner_up_score
        if top_score is not None and runner_up_score is not None
        else None
    )
    if scored and scored[0][0] >= 0.65:
        if margin is not None and margin >= 0.05:
            return (
                scored[0][1],
                "translation_text_similarity",
                top_score,
                runner_up_score,
                margin,
            )
    return None, "unresolved", top_score, runner_up_score, margin


def parse_review3(
    path: Path,
    chapter: int,
    translation_rows: list[dict],
    provenance_rows: list[dict],
) -> list[dict]:
    document = read_json(path)
    index_map = legacy_index_map(provenance_rows, chapter)
    rows = []
    for flag in document.get("flags", []):
        unit_id, method, score, runner_up_score, score_margin = best_translation_match(
            flag.get("text", ""), translation_rows, chapter
        )
        assumption = None
        if unit_id is None and isinstance(flag.get("idx"), int):
            unit_id = index_map.get(flag["idx"] + 1)
            if unit_id:
                method = "review3_zero_based_legacy_index_fallback"
                score = None
                runner_up_score = None
                score_margin = None
                assumption = (
                    "review3 idx is zero-based; legacy translation index is idx + 1"
                )
        rows.append(
            {
                "book_chapter": chapter,
                "mapping_assumption": assumption,
                "mapping_confidence": (
                    "high" if score == 1.0 else "medium" if unit_id else "unresolved"
                ),
                "mapping_method": method,
                "mapping_score": score,
                "reported_severity": flag.get("rating", ""),
                "review_payload": flag.get("note", ""),
                "runner_up_score": runner_up_score,
                "score_margin": score_margin,
                "source_review": str(path),
                "source_review_index": flag.get("idx"),
                "status": "unconfirmed",
                "unit_id": unit_id,
            }
        )
    return rows


def reviewer_profile() -> dict:
    return {
        "audience": "中国大陆普通读者，同时不牺牲技术含义",
        "hard_gates": [
            "核心命题、否定、范围、因果和指代完整",
            "required 术语一致",
            "简体字与大陆图书标点合规",
            "脚注与书末注释的链接、引文和文献信息完整",
        ],
        "language": "简体中文",
        "profile_version": 1,
        "role": "资深简体中文科普图书编辑",
        "source_of_truth": "translation_preview/STYLE_GUIDE.md",
    }


def content_policies() -> dict:
    heading_policy = {
        "evaluate": ["accuracy", "book_style_consistency"],
        "translation_mode": "full_translation",
    }
    return {
        "body": {
            "evaluate": ["fidelity", "naturalness", "logic", "voice", "rhythm"],
            "translation_mode": "full_translation",
        },
        "chapter_number": dict(heading_policy),
        "chapter_title": dict(heading_policy),
        "endnote_bibliography": {
            "evaluate": ["bibliographic_integrity", "locator_phrase", "markup"],
            "preserve": ["author", "work_title", "date", "page", "doi", "url"],
            "translation_mode": "translate_locator_preserve_citation",
        },
        "endnote_commentary": {
            "evaluate": ["fidelity", "clarity", "citation_context"],
            "translation_mode": "full_translation",
        },
        "endnote_quotation": {
            "evaluate": ["quotation_fidelity", "voice", "markup"],
            "translation_mode": "translate_quote_preserve_source_citation",
        },
        "endnote_numbered_commentary": {
            "evaluate": ["fidelity", "clarity", "citation_context"],
            "preserve": ["note_number"],
            "translation_mode": "translate_locator_and_commentary",
        },
        "endnotes_section_heading": dict(heading_policy),
        "footnote": {
            "evaluate": ["fidelity", "clarity", "reference_integrity"],
            "translation_mode": "full_translation",
        },
        "footnotes_heading": dict(heading_policy),
        "url_marker": {
            "evaluate": ["exact_copy"],
            "translation_mode": "do_not_translate",
        },
    }


def build_term_snapshot(
    source_rows: list[dict], workspace: Path, paratranz_terms: Path, style_guide: Path
) -> dict:
    legacy = []
    for scope in SCOPES:
        path = workspace / scope["legacy_terms"]
        for entry in read_json(path):
            legacy.append(
                {
                    **entry,
                    "book_chapter": scope["book_chapter"],
                    "source_file": str(path),
                    "status": "candidate_legacy_zhHant",
                }
            )
    paratranz_document = read_json(paratranz_terms)
    relevant_paratranz = []
    for index, entry in enumerate(paratranz_document.get("entries", [])):
        matches = []
        for term in entry.get("source_terms", []):
            unit_ids = [
                row["unit_id"]
                for row in source_rows
                if term_present(row["source_text"], term)
            ]
            if unit_ids:
                matches.append(
                    {
                        "source_term": term,
                        "unit_ids": unit_ids,
                    }
                )
        if matches:
            relevant_paratranz.append(
                {
                    "entry_index": index,
                    "matched_source_terms": [match["source_term"] for match in matches],
                    "matched_unit_ids": sorted(
                        {unit_id for match in matches for unit_id in match["unit_ids"]}
                    ),
                    "matches": matches,
                    "note": entry.get("note", ""),
                    "status": "candidate_unverified",
                    "translation": entry.get("translation", ""),
                }
            )
    required = [
        {
            "english": "alchemist / alchemy",
            "status": "required",
            "translation": "炼金术士／炼金术",
        },
        {
            "english": "AI alignment",
            "status": "required",
            "translation": "AI 对齐",
        },
        {
            "english": "artificial superintelligence / ASI",
            "status": "required",
            "translation": "超级人工智能（ASI）",
        },
        {
            "english": "superintelligence",
            "status": "required",
            "translation": "超级智能",
        },
        {
            "english": "Aqua Regia",
            "status": "required",
            "translation": "王水",
        },
        {
            "english": "shut down",
            "status": "contextual",
            "translation": "关停／关闭／叫停／停机（按对象与语境）",
        },
    ]
    conflicts = [
        {
            "english": "alchemy",
            "issue": "legacy glossary uses traditional 鍊／煉 variants; target is zh-Hans",
            "resolution": "炼金术",
            "status": "resolved_by_style_guide",
        },
        {
            "english": "superintelligence / ASI",
            "issue": "legacy glossary uses 台湾繁中 智慧 forms",
            "resolution": "超级智能；超级人工智能（ASI）",
            "status": "resolved_by_style_guide",
        },
        {
            "english": "shut down",
            "issue": (
                "single fixed translation would be wrong across "
                "company/device/development contexts"
            ),
            "resolution": "contextual term card",
            "status": "contextual",
        },
        {
            "english": "attorney general",
            "issue": (
                "legacy term 檢察總長 conflicts with mainland-target editorial "
                "context"
            ),
            "resolution": "needs contextual editorial decision before finalization",
            "status": "provisional",
        },
    ]
    return {
        "conflicts": conflicts,
        "legacy_candidates": legacy,
        "paratranz_candidates": relevant_paratranz,
        "priority_order": [
            "STYLE_GUIDE",
            "required house terms",
            "ParaTranz terms after verification",
            "legacy glossary candidates",
        ],
        "required_and_contextual": required,
        "schema_version": 1,
        "source_hashes": {
            "paratranz_terms": sha256(paratranz_terms),
            "style_guide": sha256(style_guide),
        },
    }


def scope_document() -> dict:
    return {
        "book_scopes": [
            {
                key: value
                for key, value in scope.items()
                if key not in {"review2", "review3", "legacy_terms"}
            }
            for scope in SCOPES
        ],
        "shared_structure": [
            {
                "document": "OEBPS/appendix003.xhtml",
                "element": "NOTES",
                "status": "recorded_outside_chapter_ownership",
            }
        ],
    }


def coverage_report(
    source_rows: list[dict], translation_rows: list[dict], validation: dict
) -> str:
    translations = {row["unit_id"]: row for row in translation_rows}
    lines = [
        "# Baseline coverage",
        "",
        "This report counts English DOM units, not only units that already have "
        "translations.",
        "",
        "| Book chapter | Content type | Units | Present zh-Hans | Missing zh-Hans |",
        "|---:|---|---:|---:|---:|",
    ]
    grouped: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for row in source_rows:
        grouped[(row["book_chapter"], row["content_type"])].append(row)
    for (chapter, content_type), rows in sorted(grouped.items()):
        present = sum(
            translations[row["unit_id"]]["status"] == "present" for row in rows
        )
        lines.append(
            f"| {chapter} | {content_type} | {len(rows)} | {present} | "
            f"{len(rows) - present} |"
        )
    present_count = sum(row["status"] == "present" for row in translation_rows)
    missing_count = sum(row["status"] == "missing" for row in translation_rows)
    sources_identical = validation["source_comparison"]["identical"]
    lines.extend(
        [
            "",
            f"- Total source units: {len(source_rows)}",
            f"- Present translations: {present_count}",
            f"- Missing translations: {missing_count}",
            f"- Primary/reference English identical: {sources_identical}",
            "",
            "Footnote link validation:",
            "",
        ]
    )
    for chapter, result in sorted(validation["footnote_links"].items()):
        lines.append(
            f"- Chapter {chapter}: {len(result['footnote_ids'])} footnotes; "
            f"references_match={result['references_match_footnotes']}; "
            f"backlinks_valid={result['backlinks_valid']}"
        )
    return "\n".join(lines) + "\n"


def missing_report(source_rows: list[dict], translation_rows: list[dict]) -> str:
    source_by_id = {row["unit_id"]: row for row in source_rows}
    missing = [row for row in translation_rows if row["status"] == "missing"]
    lines = [
        "# Missing translations",
        "",
        "Missing means no adjacent zh-Hans translation exists in the frozen input "
        "EPUB.",
        "",
        "| Unit | Type | English source |",
        "|---|---|---|",
    ]
    for item in missing:
        source = source_by_id[item["unit_id"]]
        text = source["source_text"].replace("|", "\\|")
        lines.append(f"| {item['unit_id']} | {source['content_type']} | {text} |")
    return "\n".join(lines) + "\n"


def alignment_report(
    evidence_rows: list[dict],
    translation_rows: list[dict],
    graphs: dict,
) -> str:
    accepted_alignments = {"missing", "adjacent_sibling"}
    ambiguous = [
        row
        for row in translation_rows
        if row["alignment_status"] not in accepted_alignments
    ]
    unresolved_evidence = [row for row in evidence_rows if not row["unit_id"]]
    lines = [
        "# Alignment issues",
        "",
        f"- Ambiguous source/translation alignments: {len(ambiguous)}",
        f"- Unresolved prior-review mappings: {len(unresolved_evidence)}",
        "",
        "Complete translation graph:",
        "",
    ]
    for chapter, sections in sorted(graphs.items()):
        for section_name, graph in sorted(sections.items()):
            lines.append(
                f"- Chapter {chapter} {section_name}: "
                f"sources={graph['source_count']}, "
                f"translations={graph['translation_node_count']}, "
                f"consumed={graph['consumed_translation_count']}, "
                f"orphan={graph['orphan_translation_count']}, "
                f"ambiguous={graph['ambiguous_count']}"
            )
    lines.append("")
    for row in unresolved_evidence:
        lines.append(
            f"- {row['source_review']} index {row['source_review_index']}: "
            f"{row['review_payload']}"
        )
    return "\n".join(lines) + "\n"


def term_conflict_report(snapshot: dict) -> str:
    lines = ["# Term conflicts", ""]
    for conflict in snapshot["conflicts"]:
        lines.extend(
            [
                f"## {conflict['english']}",
                "",
                f"- Status: {conflict['status']}",
                f"- Issue: {conflict['issue']}",
                f"- Resolution: {conflict['resolution']}",
                "",
            ]
        )
    return "\n".join(lines)


def validate_expected(
    source_rows: list[dict],
    translation_rows: list[dict],
    provenance_rows: list[dict],
    validation: dict,
) -> None:
    unit_ids = [row["unit_id"] for row in source_rows]
    source_by_id = {row["unit_id"]: row for row in source_rows}
    policies = content_policies()
    if len(unit_ids) != len(set(unit_ids)):
        raise ValueError("duplicate source unit IDs")
    if set(unit_ids) != {row["unit_id"] for row in translation_rows}:
        raise ValueError("source and translation unit IDs differ")
    if set(unit_ids) != {row["unit_id"] for row in provenance_rows}:
        raise ValueError("source and provenance unit IDs differ")
    if not validation["source_comparison"]["identical"]:
        raise ValueError("primary and reference EPUB English sources differ")
    for row in source_rows:
        if text_sha256(row["source_html"]) != row["source_html_sha256"]:
            raise ValueError(f"source HTML hash mismatch: {row['unit_id']}")
        if row.get("policy_key") not in policies:
            raise ValueError(
                f"missing content policy for {row['unit_id']}: {row.get('policy_key')}"
            )
        segments = row.get("source_segments", [])
        if not segments:
            raise ValueError(f"source segments missing: {row['unit_id']}")
        if "".join(item["text"] for item in segments) != row["source_text"]:
            raise ValueError(f"source segment round trip failed: {row['unit_id']}")
        for item in segments:
            if text_sha256(item["text"]) != item["source_sha256"]:
                raise ValueError(f"source segment hash mismatch: {row['unit_id']}")
        if row["content_type"] == "endnote_bibliography":
            protected_kinds = {
                item["kind"] for item in segments if item["mode"] == "preserve_exact"
            }
            if not {"note_number", "citation"} <= protected_kinds:
                raise ValueError(
                    f"bibliographic protected spans incomplete: {row['unit_id']}"
                )
        for reference in row.get("inline_references", []):
            if not reference.get("anchor_id") or not reference.get("href"):
                raise ValueError(f"inline reference incomplete: {row['unit_id']}")
            if reference.get("marker") not in row["source_text"]:
                raise ValueError(f"inline reference marker absent: {row['unit_id']}")
    for scope in SCOPES:
        chapter = scope["book_chapter"]
        prefix = f"book_ch{chapter:02d}"
        expected_headings = {
            f"{prefix}.heading.number": f"CHAPTER {chapter}",
            f"{prefix}.heading.title": scope["title"],
            f"{prefix}.endnotes.heading": f"CHAPTER {chapter}: {scope['title']}",
        }
        for unit_id, expected_text in expected_headings.items():
            if source_by_id.get(unit_id, {}).get("source_text") != expected_text:
                raise ValueError(
                    f"scope heading mismatch at {unit_id}: expected {expected_text!r}"
                )
        legacy_count = len(legacy_index_map(provenance_rows, chapter))
        if legacy_count != scope["expected_legacy_translations"]:
            raise ValueError(
                f"chapter {chapter}: expected {scope['expected_legacy_translations']} "
                f"legacy translations, got {legacy_count}"
            )
        link_result = validation["footnote_links"][str(chapter)]
        if len(link_result["footnote_ids"]) != scope["expected_footnotes"]:
            raise ValueError(f"chapter {chapter}: unexpected footnote count")
        if (
            not link_result["references_match_footnotes"]
            or not link_result["backlinks_valid"]
        ):
            raise ValueError(f"chapter {chapter}: broken footnote link graph")
        endnote_paragraphs = sum(
            row["book_chapter"] == chapter
            and row["content_type"].startswith("endnote_")
            and row["content_type"] != "endnotes_section_heading"
            for row in source_rows
        )
        if endnote_paragraphs != scope["expected_endnote_paragraphs"]:
            raise ValueError(
                f"chapter {chapter}: expected {scope['expected_endnote_paragraphs']} "
                f"endnote paragraphs, got {endnote_paragraphs}"
            )
        for section_name, graph in validation["alignment_graph"][str(chapter)].items():
            if graph["orphan_translation_count"]:
                raise ValueError(
                    f"chapter {chapter} {section_name}: orphan translations present"
                )
            if graph["ambiguous_count"]:
                raise ValueError(
                    f"chapter {chapter} {section_name}: ambiguous alignments present"
                )
    accepted_alignments = {"missing", "adjacent_sibling"}
    ambiguous = [
        row
        for row in translation_rows
        if row["alignment_status"] not in accepted_alignments
    ]
    if ambiguous:
        raise ValueError(f"ambiguous alignments present: {len(ambiguous)}")


def generated_hashes(output: Path) -> dict[str, str]:
    return {
        str(path.relative_to(output)): sha256(path)
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }


def safe_output_path(output: Path, workspace: Path) -> Path:
    if output.exists() and output.is_symlink():
        raise ValueError(f"refusing symlink output path: {output}")
    resolved = output.resolve()
    workspace_root = workspace.resolve()
    allowed_roots = {
        (workspace_root / "translation_preview").resolve(),
        Path("/tmp").resolve(),
    }
    if not output.name.startswith("baseline_"):
        raise ValueError("baseline output directory name must start with 'baseline_'")
    if resolved in {Path("/").resolve(), workspace_root, *allowed_roots}:
        raise ValueError(f"refusing destructive output path: {resolved}")
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise ValueError(
            "baseline output must be inside workspace/translation_preview or /tmp"
        )
    return resolved


def build(args: argparse.Namespace) -> int:
    output = safe_output_path(args.output, args.workspace)
    if output.exists():
        if not args.force:
            raise FileExistsError(f"output exists: {output}")
        shutil.rmtree(output)
    for directory in ("config", "source", "baseline", "evidence", "private", "reports"):
        (output / directory).mkdir(parents=True, exist_ok=True)

    source_rows, translation_rows, provenance_rows, primary_validation = extract_all(
        args.epub, "local_zhHans_epub"
    )
    reference_source, _, _, _ = extract_all(
        args.reference_epub, "reference_zhHant_epub"
    )
    source_comparison = compare_sources(source_rows, reference_source)
    validation = {
        "alignment_graph": primary_validation["alignment_graph"],
        "footnote_links": primary_validation["footnote_links"],
        "source_comparison": source_comparison,
    }
    validate_expected(source_rows, translation_rows, provenance_rows, validation)

    evidence_rows = []
    for scope in SCOPES:
        review2_path = args.workspace / scope["review2"]
        review3_path = args.workspace / scope["review3"]
        evidence_rows.extend(
            parse_review2(review2_path, scope["book_chapter"], provenance_rows)
        )
        evidence_rows.extend(
            parse_review3(
                review3_path,
                scope["book_chapter"],
                translation_rows,
                provenance_rows,
            )
        )

    term_snapshot = build_term_snapshot(
        source_rows,
        args.workspace,
        args.paratranz_terms,
        args.style_guide,
    )
    write_json(output / "config" / "reviewer_profile.json", reviewer_profile())
    write_json(output / "config" / "content_policies.json", content_policies())
    write_json(output / "config" / "term_snapshot.json", term_snapshot)
    write_json(output / "source" / "scope.json", scope_document())
    write_jsonl(output / "source" / "units.jsonl", source_rows)
    write_jsonl(output / "baseline" / "current_translations.jsonl", translation_rows)
    write_jsonl(output / "evidence" / "prior_review_flags.jsonl", evidence_rows)
    write_jsonl(output / "private" / "dom_provenance.jsonl", provenance_rows)
    (output / "reports" / "coverage.md").write_text(
        coverage_report(source_rows, translation_rows, validation), encoding="utf-8"
    )
    (output / "reports" / "missing_translations.md").write_text(
        missing_report(source_rows, translation_rows), encoding="utf-8"
    )
    (output / "reports" / "alignment_issues.md").write_text(
        alignment_report(
            evidence_rows,
            translation_rows,
            validation["alignment_graph"],
        ),
        encoding="utf-8",
    )
    (output / "reports" / "term_conflicts.md").write_text(
        term_conflict_report(term_snapshot), encoding="utf-8"
    )
    (output / "README.md").write_text(
        "# Chapter 11 and 13 translation baseline\n\n"
        "This immutable baseline records English source units, current zh-Hans "
        "translations, missing material, term inputs, and prior review evidence.\n\n"
        "It makes no winner decisions and does not edit either EPUB.\n",
        encoding="utf-8",
    )

    counts_by_chapter = {}
    for scope in SCOPES:
        chapter = scope["book_chapter"]
        chapter_sources = [row for row in source_rows if row["book_chapter"] == chapter]
        chapter_ids = {row["unit_id"] for row in chapter_sources}
        chapter_translations = [
            row for row in translation_rows if row["unit_id"] in chapter_ids
        ]
        counts_by_chapter[str(chapter)] = {
            "missing_translations": sum(
                row["status"] == "missing" for row in chapter_translations
            ),
            "present_translations": sum(
                row["status"] == "present" for row in chapter_translations
            ),
            "source_units": len(chapter_sources),
        }
    manifest = {
        "baseline_version": BASELINE_VERSION,
        "counts_by_chapter": counts_by_chapter,
        "epub_sha256": sha256(args.epub),
        "generated_files_sha256": generated_hashes(output),
        "primary_epub": str(args.epub.resolve()),
        "reference_epub": str(args.reference_epub.resolve()),
        "reference_epub_sha256": sha256(args.reference_epub),
        "schema_version": SCHEMA_VERSION,
        "source_comparison": source_comparison,
        "total_missing_translations": sum(
            row["status"] == "missing" for row in translation_rows
        ),
        "total_present_translations": sum(
            row["status"] == "present" for row in translation_rows
        ),
        "total_source_units": len(source_rows),
        "validation": {
            "alignment_graph": validation["alignment_graph"],
            "ambiguous_alignments": sum(
                graph["ambiguous_count"]
                for sections in validation["alignment_graph"].values()
                for graph in sections.values()
            ),
            "footnote_links": validation["footnote_links"],
            "prior_review_flags": len(evidence_rows),
            "unresolved_prior_review_mappings": sum(
                not row["unit_id"] for row in evidence_rows
            ),
        },
    }
    write_json(output / "manifest.json", manifest)
    validate(output)
    print(
        f"Built baseline_v{BASELINE_VERSION} with {len(source_rows)} source units, "
        f"{manifest['total_present_translations']} present translations, and "
        f"{manifest['total_missing_translations']} missing translations"
    )
    return 0


def validate(output: Path) -> int:
    manifest = read_json(output / "manifest.json")
    source_rows = read_jsonl(output / "source" / "units.jsonl")
    translation_rows = read_jsonl(output / "baseline" / "current_translations.jsonl")
    provenance_rows = read_jsonl(output / "private" / "dom_provenance.jsonl")
    validation = {
        "alignment_graph": manifest["validation"]["alignment_graph"],
        "footnote_links": manifest["validation"]["footnote_links"],
        "source_comparison": manifest["source_comparison"],
    }
    validate_expected(source_rows, translation_rows, provenance_rows, validation)
    if len(source_rows) != manifest["total_source_units"]:
        raise ValueError("manifest source unit count mismatch")
    present_count = sum(row["status"] == "present" for row in translation_rows)
    missing_count = sum(row["status"] == "missing" for row in translation_rows)
    if present_count != manifest["total_present_translations"]:
        raise ValueError("manifest present translation count mismatch")
    if missing_count != manifest["total_missing_translations"]:
        raise ValueError("manifest missing translation count mismatch")
    for path_key, hash_key in (
        ("primary_epub", "epub_sha256"),
        ("reference_epub", "reference_epub_sha256"),
    ):
        epub_path = Path(manifest[path_key])
        if not epub_path.exists() or sha256(epub_path) != manifest[hash_key]:
            raise ValueError(f"input EPUB hash mismatch: {epub_path}")
    for relative_path, expected_hash in manifest["generated_files_sha256"].items():
        path = output / relative_path
        if not path.exists() or sha256(path) != expected_hash:
            raise ValueError(f"generated file hash mismatch: {relative_path}")
    print(
        f"Validated baseline_v{manifest['baseline_version']}: "
        f"{len(source_rows)} source units, no ambiguous alignments"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--epub", required=True, type=Path)
    build_parser.add_argument("--reference-epub", required=True, type=Path)
    build_parser.add_argument("--workspace", required=True, type=Path)
    build_parser.add_argument("--paratranz-terms", required=True, type=Path)
    build_parser.add_argument("--style-guide", required=True, type=Path)
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--force", action="store_true")
    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "build":
            return build(args)
        return validate(args.output)
    except (
        FileExistsError,
        OSError,
        ValueError,
        ET.ParseError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
