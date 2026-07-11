#!/usr/bin/env python3
"""Export aligned source/translation pairs from a bilingual EPUB for ParaTranz.

The bilingual EPUB produced by bbook-maker places each translated paragraph
after its source paragraph and marks it with a ``lang`` attribute. Some list
and table translations are nested inside the same parent element as the source;
those are handled as well.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET


CONTAINER_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
OPF_NS = {"opf": "http://www.idpf.org/2007/opf"}
SPACE_RE = re.compile(r"\s+")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalized_text(element: ET.Element) -> str:
    return SPACE_RE.sub(" ", "".join(element.itertext())).strip()


def language_matches(element: ET.Element, target_lang: str) -> bool:
    lang = element.get("lang") or element.get(
        "{http://www.w3.org/XML/1998/namespace}lang"
    )
    return bool(lang and (lang == target_lang or lang.startswith(f"{target_lang}-")))


def remove_translations(element: ET.Element, target_lang: str) -> None:
    for parent in list(element.iter()):
        for child in list(parent):
            if language_matches(child, target_lang):
                parent.remove(child)


def source_for_translation(
    translation: ET.Element,
    parent: ET.Element,
    target_lang: str,
) -> str:
    siblings = list(parent)
    position = siblings.index(translation)
    previous = siblings[position - 1] if position else None

    # Normal bbook-maker output: <p>source</p><p lang="zh-Hans">translation</p>
    if (
        previous is not None
        and local_name(previous.tag) == "p"
        and not language_matches(previous, target_lang)
    ):
        return normalized_text(previous)

    # Lists and tables often keep source text directly in <li>/<td>, followed
    # by a nested translated <p>. Clone the parent and strip translations.
    source_container = copy.deepcopy(parent)
    remove_translations(source_container, target_lang)
    return normalized_text(source_container)


def package_path(epub: zipfile.ZipFile) -> str:
    root = ET.fromstring(epub.read("META-INF/container.xml"))
    node = root.find("c:rootfiles/c:rootfile", CONTAINER_NS)
    if node is None or not node.get("full-path"):
        raise ValueError("EPUB container does not declare an OPF package")
    return node.get("full-path", "")


def spine_documents(epub: zipfile.ZipFile, opf_path: str) -> list[str]:
    root = ET.fromstring(epub.read(opf_path))
    manifest = {
        item.get("id"): item.get("href")
        for item in root.findall("opf:manifest/opf:item", OPF_NS)
        if item.get("id") and item.get("href")
    }
    base = PurePosixPath(opf_path).parent
    documents = []
    for itemref in root.findall("opf:spine/opf:itemref", OPF_NS):
        href = manifest.get(itemref.get("idref"))
        if href:
            documents.append(str(base / href))
    return documents


def extract_file(
    epub: zipfile.ZipFile,
    document: str,
    target_lang: str,
) -> list[dict[str, str]]:
    root = ET.fromstring(epub.read(document))
    parents = {child: parent for parent in root.iter() for child in parent}
    translations = [node for node in root.iter() if language_matches(node, target_lang)]
    chapter = Path(document).stem
    entries = []

    for index, translation_node in enumerate(translations, start=1):
        parent = parents.get(translation_node)
        if parent is None:
            continue
        original = source_for_translation(translation_node, parent, target_lang)
        translation = normalized_text(translation_node)
        if not original or not translation:
            continue
        entries.append(
            {
                "key": f"{chapter}.{index:04d}",
                "original": original,
                "translation": translation,
                "context": f"{document} · unit {index}",
            }
        )
    return entries


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def export(epub_path: Path, output_dir: Path, target_lang: str, force: bool) -> int:
    if output_dir.exists():
        if not force:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    all_entries: list[dict[str, str]] = []
    chapter_counts: list[tuple[str, int]] = []
    with zipfile.ZipFile(epub_path) as epub:
        opf_path = package_path(epub)
        for document in spine_documents(epub, opf_path):
            entries = extract_file(epub, document, target_lang)
            if not entries:
                continue
            chapter = Path(document).stem
            write_json(output_dir / f"{chapter}.json", entries)
            all_entries.extend(entries)
            chapter_counts.append((chapter, len(entries)))

    keys = [entry["key"] for entry in all_entries]
    if len(keys) != len(set(keys)):
        raise ValueError("generated duplicate ParaTranz keys")

    write_json(output_dir / "_all.json", all_entries)
    summary = [
        "ParaTranz import package",
        "========================",
        f"Source EPUB: {epub_path.name}",
        f"Target language: {target_lang}",
        f"Total entries: {len(all_entries)}",
        "",
        "Recommended import:",
        "1. Create a private ParaTranz project with English as the source language.",
        "2. Upload the chapter JSON files individually under File Management.",
        "3. If the files already exist in ParaTranz, use Import Translation to merge them.",
        "4. Do not upload _all.json together with chapter files; it duplicates every entry.",
        "5. Download Original Data from ParaTranz when the team is ready to merge edits back.",
        "",
        "Files:",
        *(f"- {chapter}.json: {count}" for chapter, count in chapter_counts),
    ]
    (output_dir / "README.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return len(all_entries)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", type=Path, help="bilingual EPUB to extract")
    parser.add_argument(
        "output_dir", type=Path, help="directory for ParaTranz JSON files"
    )
    parser.add_argument(
        "--target-lang",
        default="zh-Hans",
        help="translation lang attribute to extract (default: zh-Hans)",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace an existing output directory"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        count = export(args.epub, args.output_dir, args.target_lang, args.force)
    except (
        FileExistsError,
        OSError,
        ValueError,
        ET.ParseError,
        zipfile.BadZipFile,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Exported {count} aligned entries to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
