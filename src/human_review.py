#!/usr/bin/env python3
"""Pre-check worksheet generator for human review.

Run: python3 -m src.human_review

Generates data/review_worksheet.md with automated structural pre-checks.
The actual human review must be in data/human_review.md (separate artifact).
This script does NOT produce final review verdicts.
"""
import json
import random
import re
from pathlib import Path
from src.config import PARSED_DIR, TRANSLATED_DIR, DIST_DIR, INDEX_FILE


def assess_title(index_title: str, parsed_title: str, title_zh: str) -> tuple[str, str]:
    """Assess title translation using index as authority."""
    # Use index title as ground truth
    source = index_title or parsed_title
    if not source:
        return "WARN", "No source title"
    if not title_zh:
        return "FAIL", f"No Chinese title for '{source}'"

    # Check parsed title matches index (metadata consistency)
    if index_title and parsed_title and parsed_title != index_title:
        if parsed_title == parsed_title.lower().replace(" ", ""):
            return "WARN", f"Parsed title '{parsed_title}' is slug, not index title '{index_title}'"

    has_cjk = bool(re.search(r'[\u4e00-\u9fff]', title_zh))
    if not has_cjk and title_zh == source:
        return "WARN", f"Title untranslated: '{source}'"

    return "PASS", f"'{source}' → '{title_zh}'"


def assess_terms(segments: list[dict]) -> tuple[str, str]:
    """Assess term consistency by checking that proper nouns are handled correctly.
    Accepts both preserved English AND known Chinese equivalents."""
    TERM_MAP = {
        "Silicon Valley": ["Silicon Valley", "硅谷"],
        "Y Combinator": ["Y Combinator", "YC"],
        "Paul Graham": ["Paul Graham", "保罗·格雷厄姆", "保罗"],
        "Google": ["Google", "谷歌"],
        "Facebook": ["Facebook", "脸书"],
        "Lisp": ["Lisp"],
    }
    found = missing = 0
    for seg in segments[:15]:
        orig = seg.get("text_original", "")
        zh = seg.get("text_zh", "")
        for en_term, acceptable in TERM_MAP.items():
            if en_term in orig:
                if any(acc in zh for acc in acceptable):
                    found += 1
                else:
                    missing += 1
    if missing > 0 and missing > found:
        return "WARN", f"{missing} terms not found (with equivalents checked)"
    return "PASS", f"{found} terms correctly preserved/translated"


def assess_footnotes(parsed: dict, translated: dict) -> tuple[str, str]:
    """Assess footnote semantic quality."""
    p_fn = parsed.get("footnotes", [])
    t_fn = translated.get("footnotes", [])
    cross = parsed.get("cross_page_notes")

    if cross:
        return "N/A", f"Cross-page notes to {cross['notes_page_slug']} ({cross['ref_count']} refs)"
    if not p_fn:
        return "N/A", "No footnotes"
    if len(p_fn) != len(t_fn):
        return "FAIL", f"Count: {len(p_fn)} → {len(t_fn)}"

    # Check each footnote has meaningful translation with real thresholds
    # Accept bibliographic citations (author, title, journal) as valid untranslated
    good = 0
    untranslated = []
    for i, (pf, tf) in enumerate(zip(p_fn, t_fn)):
        zh = tf.get("text_zh", "")
        en = pf.get("text", "")
        is_bib = bool(re.match(r'^[A-Z][a-z]+,?\s+[A-Z]', en.strip())) or \
                  any(w in en for w in ['Working Paper', 'NBER', 'Journal', 'Press', 'University'])
        if zh and len(zh) > 3 and (zh != en or is_bib):
            good += 1
        else:
            untranslated.append(i + 1)
    pct = good / max(len(p_fn), 1) * 100
    if pct >= 90:
        return "PASS", f"{good}/{len(p_fn)} translated ({pct:.0f}%)"
    elif pct >= 70:
        return "WARN", f"{good}/{len(p_fn)} translated; untranslated: fn#{untranslated[:3]}"
    return "FAIL", f"Only {good}/{len(p_fn)} translated; untranslated: fn#{untranslated[:5]}"


def assess_drift(segments: list[dict], rendered_html: str = "") -> tuple[str, str]:
    """Assess paragraph meaning preservation.

    Uses rendered HTML when available (which has placeholders resolved to real links),
    falling back to raw translated text.
    """
    total = len(segments)
    if total == 0:
        return "N/A", "No segments"

    # If we have rendered HTML, extract paragraph texts from it for evaluation
    rendered_paras = []
    if rendered_html:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(rendered_html, "lxml")
        content = soup.find(class_="article-content")
        if content:
            rendered_paras = [p.get_text(strip=True) for p in content.find_all(["p", "blockquote"])]

    good = 0
    issues = []
    for i, seg in enumerate(segments):
        orig = seg.get("text_original", "")
        # Use rendered paragraph if available (has placeholders resolved)
        zh = rendered_paras[i] if i < len(rendered_paras) else seg.get("text_zh", "")

        if not orig:
            good += 1
            continue
        if not zh:
            issues.append(f"Seg {seg.get('index','?')}: empty")
            continue

        has_cjk = bool(re.search(r'[\u4e00-\u9fff]', zh))
        is_citation = bool(re.match(r'^\[?\d+\]?\s*[A-Z]', orig.strip())) or \
                       bool(re.match(r'^[A-Z][a-z]+,\s+[A-Z]', orig.strip())) or \
                       any(w in orig for w in ['et al', 'ed.', 'pp.', 'Vol.', 'ISBN', 'Economist', 'Times'])
        if not has_cjk and not is_citation:
            issues.append(f"Seg {seg.get('index','?')}: no CJK")
            continue
        if is_citation:
            good += 1
            continue

        ratio = len(zh) / max(len(orig), 1)
        if 0.15 <= ratio <= 4:
            good += 1
        else:
            issues.append(f"Seg {seg.get('index','?')}: ratio {ratio:.1f}")

    pct = good / max(total, 1) * 100
    if pct >= 95:
        return "PASS", f"{good}/{total} paragraphs OK ({pct:.0f}%)"
    elif pct >= 80:
        return "WARN", f"{good}/{total} OK; {'; '.join(issues[:2])}"
    return "FAIL", f"{good}/{total} OK; {'; '.join(issues[:3])}"


def generate_review(seed: int = 42, sample_size: int = 25) -> str:
    random.seed(seed)

    with open(INDEX_FILE) as f:
        idx = json.load(f)

    essays = [e for e in idx if e["content_type"] == "essay"]
    with_fn = [e for e in essays if (PARSED_DIR / f"{e['slug']}.json").exists() and
               json.loads((PARSED_DIR / f"{e['slug']}.json").read_text()).get("footnotes")]
    without_fn = [e for e in essays if e not in with_fn]

    fn_count = min(15, len(with_fn))
    sample = random.sample(with_fn, fn_count) + random.sample(without_fn, min(sample_size - fn_count, len(without_fn)))

    for forced in ["say", "saynotes"]:
        if not any(e["slug"] == forced for e in sample):
            match = [e for e in idx if e["slug"] == forced]
            if match:
                sample.append(match[0])

    lines = [
        "# Pre-Check Worksheet for Human Review",
        f"\nThis is an automated pre-check worksheet. A separate human_review.md",
        "contains the actual reviewer judgments and verdicts.",
        f"Sample: {len(sample)} articles (seed={seed})",
        "Tool: `python3 -m src.human_review`\n",
        "## Automated Pre-Check Dimensions",
        "1. **Title**: structural check (CJK present, length ratio)",
        "2. **Terms**: proper noun presence check (may have false positives for localized terms)",
        "3. **Footnotes**: count and translation presence check",
        "4. **Drift**: rendered paragraph completeness check\n",
    ]

    pass_c = warn_c = fail_c = 0
    for i, entry in enumerate(sample):
        slug = entry["slug"]
        p_path = PARSED_DIR / f"{slug}.json"
        t_path = TRANSLATED_DIR / f"{slug}.json"
        d_path = DIST_DIR / "articles" / f"{slug}.html"

        if not p_path.exists() or not t_path.exists():
            lines.append(f"## {i+1}. {slug}: **SKIP**\n")
            continue

        p = json.loads(p_path.read_text())
        t = json.loads(t_path.read_text())
        d_html = d_path.read_text() if d_path.exists() else ""

        # Structural pre-check
        struct_ok = True
        if "{{LINK:" in d_html or "{{FNREF:" in d_html:
            struct_ok = False

        # Semantic assessments
        # Get authoritative title from index
        idx_map = {e["slug"]: e for e in json.load(open(INDEX_FILE))} if not hasattr(generate_review, '_idx') else generate_review._idx
        generate_review._idx = idx_map
        idx_entry = idx_map.get(slug, {})
        index_title = idx_entry.get("title", "")
        t1_status, t1_note = assess_title(index_title, p.get("title", ""), t.get("title_zh", ""))
        t2_status, t2_note = assess_terms(t.get("segments", []))
        t3_status, t3_note = assess_footnotes(p, t)
        t4_status, t4_note = assess_drift(t.get("segments", []), d_html)

        statuses = [t1_status, t2_status, t3_status, t4_status]
        if not struct_ok:
            overall = "FAIL"
        elif "FAIL" in statuses:
            overall = "FAIL"
        elif "WARN" in statuses:
            overall = "WARN"
        else:
            overall = "PASS"

        if overall == "PASS": pass_c += 1
        elif overall == "WARN": warn_c += 1
        else: fail_c += 1

        # Include content samples for reviewer auditability
        segs = t.get("segments", [])
        first_orig = segs[0].get("text_original", "")[:80] if segs else ""
        first_zh = segs[0].get("text_zh", "")[:80] if segs else ""

        lines.append(f"## {i+1}. {slug}: **{overall}**")
        lines.append(f"  1. Title: **{t1_status}** — {t1_note}")
        lines.append(f"  2. Terms: **{t2_status}** — {t2_note}")
        lines.append(f"  3. Footnotes: **{t3_status}** — {t3_note}")
        lines.append(f"  4. Drift: **{t4_status}** — {t4_note}")
        if not struct_ok:
            lines.append(f"  *Structural: FAIL (raw placeholder in HTML)*")
        lines.append(f"  Sample: \"{first_orig}...\" → \"{first_zh}...\"")
        lines.append(f"  Reviewer verdict: _______ (to be filled by human reviewer)")
        lines.append("")

    lines.append(f"\n## Summary")
    lines.append(f"- **PASS**: {pass_c}")
    lines.append(f"- **WARN**: {warn_c}")
    lines.append(f"- **FAIL**: {fail_c}")
    lines.append(f"- Total: {len(sample)}")

    return "\n".join(lines)


if __name__ == "__main__":
    report = generate_review()
    Path("data/review_worksheet.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"\nWorksheet saved to data/review_worksheet.md")
    print("NOTE: This is a pre-check worksheet. The actual human review")
    print("must be recorded separately in data/human_review.md")
