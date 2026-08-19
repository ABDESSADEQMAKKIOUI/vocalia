"""Regenerate `api/services/workflow/jeb_knowledge_data.py` from the site sources.

The JEB Training Center voice agent speaks from the center's own published
text. That text lives in two places that are NOT in this repository:

  * the site's FAQ source, `lib/faq-content.ts` in the site repository —
    35 questions in French, English and Arabic, plus the ten-step "LE PARCOURS
    JEB", the four-line "AVIS IMPORTANT" and the tagline;
  * the knowledge sections written from the rest of the site (programmes,
    fees, U.S. pathway, international candidates, contacts…), kept as one
    UTF-8 text file per section, verified line by line against the site.

This script turns both into Python literals so the production container needs
nothing but the repository. It is deterministic: same inputs, same output, and
the output carries the SHA-256 of every input so a diff in the generated file
can be traced to the input that changed.

Why literals and not files read at runtime
------------------------------------------
A knowledge base that silently renders an empty string because a file was not
copied into the image is worse than none: the agent would sound confident and
know nothing. Literals fail at import time, loudly, if anything is wrong.

Usage
-----
    python -m scripts.build_jeb_knowledge_data \
        --faq-json  <path to faq_site.json, exported from lib/faq-content.ts> \
        --sections  <directory holding <KEY>.verified.md files> \
        [--out api/services/workflow/jeb_knowledge_data.py]

`faq_site.json` is the evaluated content of `lib/faq-content.ts`
(`{"faqFr": {...}, "faqEn": {...}, "faqAr": {...}}`). The simplest export:

    node -e 'const fs=require("fs");let s=fs.readFileSync("lib/faq-content.ts","utf8");
      s=s.replace(/export type [\\s\\S]*?\\n}\\n/g,"").replace(/export const (\\w+): FaqContent =/g,"const $1 =");
      s+="\\nmodule.exports={faqFr,faqEn,faqAr};";const m={exports:{}};
      new Function("module","exports",s)(m,m.exports);
      fs.writeFileSync("faq_site.json",JSON.stringify(m.exports,null,1))'
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "api" / "services" / "workflow" / "jeb_knowledge_data.py"

# Section files, in the order they are assembled into the block. The key is the
# file stem (`<KEY>.verified.md`); the prefix is the numbering letter used on
# every line of that section, which the agent's rules refer to.
SECTIONS: List[Dict[str, str]] = [
    {"key": "A_identite_contacts", "prefix": "C"},
    {"key": "B_parcours_admission", "prefix": "A"},
    {"key": "C_programmes", "prefix": "P"},
    {"key": "D_parcours_usa_sponsorship", "prefix": "U"},
    {"key": "E_frais_maroc", "prefix": "F"},
    {"key": "F_candidats_internationaux", "prefix": "I"},
    {"key": "G_hors_socle", "prefix": "H"},
]

_LEADING_NUMBER = re.compile(r"^\s*\d+\s*[.)]\s*")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _faq_language(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Shape one language of the FAQ for the module.

    Questions keep their wording but lose the site's leading "12. " — the
    renderer numbers the three languages identically itself, so the number is
    data, not text. Answers are untouched: they are the center's words.
    """
    sections = []
    n = 0
    for section in raw["sections"]:
        items = []
        for item in section["items"]:
            n += 1
            items.append(
                {
                    "n": n,
                    "q": _LEADING_NUMBER.sub("", item["q"]).strip(),
                    "a": item["a"],
                }
            )
        sections.append({"title": section["title"], "items": items})
    return {
        "sections": sections,
        "pathway_title": raw["pathwayTitle"],
        "pathway": list(raw["pathway"]),
        "notice_title": raw["noticeTitle"],
        "notice": list(raw["notice"]),
        "tagline": raw["tagline"],
    }


def _check_alignment(faqs: Dict[str, Dict[str, Any]]) -> None:
    """The renderer relies on question N meaning the same thing in every
    language; the only cheap invariant we can check is the count per section."""
    shapes = {
        lang: [len(s["items"]) for s in faq["sections"]] for lang, faq in faqs.items()
    }
    first = next(iter(shapes.values()))
    for lang, shape in shapes.items():
        if shape != first:
            sys.exit(f"FAQ sections are not aligned across languages: {shapes}")


def _literal(value: Any) -> str:
    """A Python literal, readable in a diff: one JSON-ish line per string."""
    return json.dumps(value, ensure_ascii=False, indent=4)


def _read_section(sections_dir: Path, key: str) -> str:
    path = sections_dir / f"{key}.verified.md"
    if not path.exists():
        sys.exit(f"missing section file: {path}")
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    if not text:
        sys.exit(f"empty section file: {path}")
    return text


def build(faq_json: Path, sections_dir: Path, out: Path) -> None:
    raw = json.loads(faq_json.read_text(encoding="utf-8"))
    faqs = {
        "fr": _faq_language(raw["faqFr"]),
        "en": _faq_language(raw["faqEn"]),
        "ar": _faq_language(raw["faqAr"]),
    }
    _check_alignment(faqs)

    section_texts: Dict[str, str] = {}
    inputs = [(faq_json.name, _sha(faq_json))]
    for section in SECTIONS:
        section_texts[section["key"]] = _read_section(sections_dir, section["key"])
        path = sections_dir / f"{section['key']}.verified.md"
        inputs.append((path.name, _sha(path)))

    header = (
        '"""GENERATED FILE — do not edit by hand.\n\n'
        "Published content of JEB Training Center, shaped for the voice agent.\n"
        "Regenerate with `python -m scripts.build_jeb_knowledge_data` (see that\n"
        "script for where the inputs come from). Edits made here are lost on the\n"
        "next run; fix the inputs instead.\n\n"
        f"Generated on {date.today().isoformat()} from:\n"
        + "".join(f"  {name}  sha256:{sha}\n" for name, sha in inputs)
        + '"""\n\n'
        "from __future__ import annotations\n\n"
        "from typing import Any, Dict, List\n\n"
    )

    body = []
    body.append("# One entry per language; question numbers align across the three.\n")
    body.append(f"FAQ: Dict[str, Dict[str, Any]] = {_literal(faqs)}\n\n")
    body.append(
        "# Knowledge sections written from the site's pages, verified line by line.\n"
        "# Keyed by section id; the order of SECTION_ORDER is the order in the block.\n"
    )
    body.append(
        "SECTION_ORDER: List[Dict[str, str]] = "
        f"{_literal(SECTIONS)}\n\n"
    )
    body.append(f"SECTION_TEXTS: Dict[str, str] = {_literal(section_texts)}\n")

    out.write_text(header + "".join(body), encoding="utf-8", newline="\n")
    total = sum(len(t) for t in section_texts.values())
    print(f"wrote {out} — FAQ 3 × {len(faqs['fr']['sections'])} sections, "
          f"{sum(len(s['items']) for s in faqs['fr']['sections'])} questions; "
          f"{len(section_texts)} knowledge sections, {total:,} chars")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--faq-json", required=True, type=Path)
    parser.add_argument("--sections", required=True, type=Path)
    parser.add_argument("--out", default=DEFAULT_OUT, type=Path)
    args = parser.parse_args()
    build(args.faq_json, args.sections, args.out)


if __name__ == "__main__":
    main()
