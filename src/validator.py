import json
import re
from pathlib import Path
from urllib.parse import urljoin

from src.extraction_rules import get_code_text, is_empty_pre, is_tracking_pixel


# ── Translation validation (parsed vs translated) ───────────────────

def validate_translation(slug: str, parsed_dir: Path, translated_dir: Path) -> dict:
    parsed_path = parsed_dir / f"{slug}.json"
    translated_path = translated_dir / f"{slug}.json"

    if not parsed_path.exists():
        return {"slug": slug, "status": "missing_source"}
    if not translated_path.exists():
        return {"slug": slug, "status": "not_translated"}

    parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
    translated = json.loads(translated_path.read_text(encoding="utf-8"))

    issues = []

    src_segs = parsed.get("segments", [])
    tgt_segs = translated.get("segments", [])
    if len(src_segs) != len(tgt_segs):
        issues.append(f"segment_count_mismatch: source={len(src_segs)}, translated={len(tgt_segs)}")

    src_types = [(s["type"], s.get("heading_level")) for s in src_segs]
    tgt_types = [(s["type"], s.get("heading_level")) for s in tgt_segs]
    if src_types != tgt_types:
        issues.append("segment_type_sequence_mismatch")

    for i, seg in enumerate(tgt_segs):
        if i >= len(src_segs):
            break
        src_seg = src_segs[i]

        if src_seg.get("type") in ("code", "math_block", "bibtex"):
            if seg.get("text_zh", "") != src_seg.get("text", ""):
                issues.append(f"{src_seg['type']}_seg_{i}_text_modified")

        if src_seg.get("type") == "heading":
            if src_seg.get("heading_level") != seg.get("heading_level"):
                issues.append(f"heading_seg_{i}_level_mismatch")

        if src_seg.get("type") == "figure":
            src_img = src_seg.get("image_src", "")
            tgt_img = seg.get("image_src", "")
            if src_img and not tgt_img:
                issues.append(f"figure_seg_{i}_image_src_missing")

        if src_seg.get("type") not in ("code", "figure"):
            text_zh = seg.get("text_zh", "")
            text_orig = seg.get("text_original", "")
            src_fnref = len(re.findall(r'\{\{FNREF:\d+\}\}', text_orig))
            tgt_fnref = len(re.findall(r'\{\{FNREF:\d+\}\}', text_zh))
            if src_fnref != tgt_fnref:
                issues.append(f"seg_{i}_fnref_count: source={src_fnref}, translated={tgt_fnref}")
            src_link = len(re.findall(r'\{\{LINK:[^}]+\}\}', text_orig))
            tgt_link = len(re.findall(r'\{\{LINK:[^}]+\}\}', text_zh))
            if src_link != tgt_link:
                issues.append(f"seg_{i}_link_count: source={src_link}, translated={tgt_link}")
            src_math = len(re.findall(r'\{\{MATH:\d+\}\}', text_orig))
            tgt_math = len(re.findall(r'\{\{MATH:\d+\}\}', text_zh))
            if src_math != tgt_math:
                issues.append(f"seg_{i}_math_count: source={src_math}, translated={tgt_math}")
            src_cite = len(re.findall(r'\{\{CITE:\d+\}\}', text_orig))
            tgt_cite = len(re.findall(r'\{\{CITE:\d+\}\}', text_zh))
            if src_cite != tgt_cite:
                issues.append(f"seg_{i}_cite_count: source={src_cite}, translated={tgt_cite}")
            src_code_ph = len(re.findall(r'\{\{CODE:\d+\}\}', text_orig))
            tgt_code_ph = len(re.findall(r'\{\{CODE:\d+\}\}', text_zh))
            if src_code_ph != tgt_code_ph:
                issues.append(f"seg_{i}_code_ph_count: source={src_code_ph}, translated={tgt_code_ph}")

    src_fn = len(parsed.get("footnotes", []))
    tgt_fn = len(translated.get("footnotes", []))
    if src_fn != tgt_fn:
        issues.append(f"footnote_count_mismatch: source={src_fn}, translated={tgt_fn}")

    if translated.get("slug") != parsed.get("slug"):
        issues.append("slug_modified")

    p_cpn = parsed.get("cross_page_notes")
    t_cpn = translated.get("cross_page_notes")
    if p_cpn and not t_cpn:
        issues.append("cross_page_notes_missing")

    if bool(parsed.get("is_notes_page")) != bool(translated.get("is_notes_page")):
        issues.append("is_notes_page_mismatch")

    return {"slug": slug, "status": "pass" if not issues else "issues", "issues": issues}


# ── Source-vs-Parsed structural validation ───────────────────────────

def validate_source_vs_parsed(slug: str, raw_dir: Path, parsed_dir: Path) -> dict:
    """Compare raw HTML structure against parsed JSON for extraction completeness.
    Uses the same filter rules as the parser: empty <pre> skipped, tracking pixels skipped,
    empty headings skipped."""
    from bs4 import BeautifulSoup

    raw_path = raw_dir / f"{slug}.html"
    parsed_path = parsed_dir / f"{slug}.json"

    if not raw_path.exists() or not parsed_path.exists():
        return {"slug": slug, "status": "skip"}

    html = raw_path.read_text(encoding="utf-8")
    parsed = json.loads(parsed_path.read_text(encoding="utf-8"))

    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one("div.nested-copy-line-height.lh-copy.serif")
    if not content:
        content = soup.find("article")
    if not content:
        return {"slug": slug, "status": "skip"}

    # Strip noise — same as adapter
    for t in content.find_all(["aside", "nav", "form"]):
        t.decompose()
    for el in content.find_all(class_=re.compile(r'newsletter|subscribe', re.I)):
        el.decompose()

    issues = []
    page_url = parsed.get("url", "")

    # Heading sequence
    raw_headings = []
    for h in content.find_all(["h2", "h3", "h4"]):
        if h.get_text(strip=True):
            raw_headings.append(int(h.name[1]))
    parsed_headings = [s.get("heading_level") for s in parsed.get("segments", [])
                       if s.get("type") == "heading"]
    if raw_headings != parsed_headings:
        issues.append(f"heading_sequence: raw={raw_headings}, parsed={parsed_headings}")

    # Code: count + ordered text comparison
    raw_code_texts = [get_code_text(pre) for pre in content.find_all("pre")
                      if not is_empty_pre(get_code_text(pre))]
    parsed_code_texts = [s.get("text", "") for s in parsed.get("segments", [])
                         if s.get("type") == "code"]
    if len(raw_code_texts) != len(parsed_code_texts):
        issues.append(f"code_count: raw={len(raw_code_texts)}, parsed={len(parsed_code_texts)}")
    else:
        for i, (raw_ct, parsed_ct) in enumerate(zip(raw_code_texts, parsed_code_texts)):
            if raw_ct != parsed_ct:
                issues.append(f"code_text_{i}_mismatch")

    # Images: count + ordered URL comparison
    raw_img_urls = []
    for img in content.find_all("img", src=True):
        src = img.get("src", "")
        if not is_tracking_pixel(src):
            abs_url = urljoin(page_url, src) if not src.startswith("http") else src
            raw_img_urls.append(abs_url)
    parsed_fig_urls = [s.get("image_src", "") for s in parsed.get("segments", [])
                       if s.get("type") == "figure"]
    if len(raw_img_urls) != len(parsed_fig_urls):
        if len(raw_img_urls) > 0 and len(parsed_fig_urls) == 0:
            issues.append(f"images_lost: raw={len(raw_img_urls)}, parsed=0")
        else:
            issues.append(f"image_count: raw={len(raw_img_urls)}, parsed={len(parsed_fig_urls)}")
    else:
        for i, (raw_url, parsed_url) in enumerate(zip(raw_img_urls, parsed_fig_urls)):
            if raw_url != parsed_url:
                issues.append(f"image_url_{i}_mismatch: raw={raw_url[:60]}, parsed={parsed_url[:60]}")

    return {"slug": slug, "status": "pass" if not issues else "issues", "issues": issues}


def validate_transformer_circuits(slug: str, raw_dir: Path, parsed_dir: Path,
                                  dist_dir: Path) -> dict:
    """Transformer-circuits specific structural validation."""
    from bs4 import BeautifulSoup

    raw_path = raw_dir / f"{slug}.html"
    parsed_path = parsed_dir / f"{slug}.json"

    if not raw_path.exists() or not parsed_path.exists():
        return {"slug": slug, "status": "skip"}

    parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
    issues = []

    # 1. Math registry coverage: every non-display entry must be referenced in some segment
    math_reg = parsed.get("math_registry", {})
    all_segment_text = " ".join(s.get("text", "") for s in parsed.get("segments", []))
    all_fn_text = " ".join(fn.get("text", "") for fn in parsed.get("footnotes", []))
    all_text = all_segment_text + " " + all_fn_text

    # Also count math_block segments (display math)
    math_block_texts = {s.get("text", "") for s in parsed.get("segments", [])
                        if s.get("type") == "math_block"}

    unreferenced_math = []
    for idx, entry in math_reg.items():
        if entry.get("display"):
            # Display math should appear as math_block segment
            if entry.get("tex", "") not in math_block_texts:
                unreferenced_math.append(idx)
        else:
            # Inline math should appear as {{MATH:N}} in some text
            if f"{{{{MATH:{idx}}}}}" not in all_text:
                unreferenced_math.append(idx)

    if unreferenced_math:
        issues.append(f"unreferenced_math_registry: {unreferenced_math[:5]}")

    # 2. Check local image files exist and are non-zero
    img_dir = raw_dir / "images" / slug
    for seg in parsed.get("segments", []):
        if seg.get("type") == "figure":
            src = seg.get("image_src", "")
            if src.startswith("data:"):
                continue
            # Local path: ../images/{slug}/{file} → check raw_dir/images/{slug}/{file}
            if src.startswith("../images/"):
                local_file = raw_dir / "images" / src[len("../images/"):]
                if not local_file.exists():
                    issues.append(f"missing_image: {src}")
                elif local_file.stat().st_size == 0:
                    issues.append(f"zero_byte_image: {src}")

    # 3. Check rendered HTML has no escaped table tags
    dist_html_path = dist_dir / "articles" / f"{slug}.html"
    if dist_html_path.exists():
        dist_html = dist_html_path.read_text(encoding="utf-8")
        if "&lt;table" in dist_html and "<div class=\"table-container\">" in dist_html:
            issues.append("escaped_table_html_in_output")

    # 4. Footnote count consistency (within content container only, matching adapter scope)
    raw_html = raw_path.read_text(encoding="utf-8")
    raw_soup = BeautifulSoup(raw_html, "lxml")
    content_el = (raw_soup.find("d-article") or raw_soup.find("article")
                  or raw_soup.find("main") or raw_soup.find("body"))
    raw_fn_count = len(content_el.find_all("d-footnote")) if content_el else 0
    parsed_fn_count = len(parsed.get("footnotes", []))
    if raw_fn_count != parsed_fn_count:
        issues.append(f"footnote_count: raw={raw_fn_count}, parsed={parsed_fn_count}")

    # 5. Citation fidelity: per-key occurrence count must match
    from collections import Counter as Ctr
    citation_reg = parsed.get("citation_registry", {})
    if content_el:
        raw_cite_keys = [d.get("key", "").strip() for d in content_el.find_all("d-cite")
                         if d.get("key", "").strip()]
        parsed_cite_keys = [e.get("key", "") for e in citation_reg.values()]
        if Ctr(raw_cite_keys) != Ctr(parsed_cite_keys):
            issues.append(f"citation_key_distribution_mismatch: "
                          f"raw_total={len(raw_cite_keys)}, parsed_total={len(parsed_cite_keys)}")

    # 6. Bibliography: only required when article uses citations
    bib_segments = [s for s in parsed.get("segments", []) if s.get("type") == "bibtex"]
    has_citations = len(parsed.get("citation_registry", {})) > 0
    if has_citations:
        # Article uses citations — must have real bibtex content
        if not bib_segments:
            issues.append("article_with_citations_missing_bibliography")
        elif all(s.get("text", "").startswith("% Bibliography:") for s in bib_segments):
            issues.append("article_with_citations_only_has_source_reference")
    if not has_citations and bib_segments:
        # Article has no citations but still outputs bibliography — noise
        issues.append("bibliography_segment_without_citations")

    # 7. d-code preservation: exact count comparison
    if content_el:
        raw_dcode_count = len(content_el.find_all("d-code"))
        inline_code_reg = parsed.get("inline_code_registry", {})
        parsed_dcode_count = len(inline_code_reg)
        if raw_dcode_count != parsed_dcode_count:
            issues.append(f"d_code_count: raw={raw_dcode_count}, parsed={parsed_dcode_count}")

    return {"slug": slug, "status": "pass" if not issues else "issues", "issues": issues}


# ── Main validation orchestrator ─────────────────────────────────────

def validate_all(paths=None) -> dict:
    if paths is None:
        from src.config import INDEX_FILE, PARSED_DIR, TRANSLATED_DIR
        RAW_DIR = None
    else:
        INDEX_FILE = paths.INDEX_FILE
        PARSED_DIR = paths.PARSED_DIR
        TRANSLATED_DIR = paths.TRANSLATED_DIR
        RAW_DIR = paths.RAW_DIR

    if not INDEX_FILE.exists():
        print("Error: index.json not found")
        return {}

    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        index = json.load(f)

    results = {"pass": 0, "issues": 0, "not_translated": 0, "missing_source": 0, "details": []}

    for entry in index:
        result = validate_translation(entry["slug"], PARSED_DIR, TRANSLATED_DIR)
        results["details"].append(result)
        results[result["status"]] = results.get(result["status"], 0) + 1

    print(f"Validation: {results['pass']} pass, {results['issues']} issues, "
          f"{results['not_translated']} not translated")

    if results["issues"] > 0:
        print("\nArticles with issues:")
        for r in results["details"]:
            if r["status"] == "issues":
                for issue in r["issues"][:3]:
                    print(f"  {r['slug']}: {issue}")

    # Source-vs-parsed structural validation (site-specific)
    site_name = paths.DATA_DIR.name if paths else ""
    if RAW_DIR and RAW_DIR.exists() and site_name in ("lethain", "transformer_circuits"):
        struct_results = {"pass": 0, "issues": 0, "skip": 0, "details": []}

        if site_name == "lethain":
            for entry in index:
                sr = validate_source_vs_parsed(entry["slug"], RAW_DIR, PARSED_DIR)
                struct_results["details"].append(sr)
                struct_results[sr["status"]] = struct_results.get(sr["status"], 0) + 1
        elif site_name == "transformer_circuits":
            for entry in index:
                sr = validate_transformer_circuits(entry["slug"], RAW_DIR, PARSED_DIR,
                                                   paths.DIST_DIR)
                struct_results["details"].append(sr)
                struct_results[sr["status"]] = struct_results.get(sr["status"], 0) + 1

        print(f"\nStructural validation: {struct_results['pass']} pass, "
              f"{struct_results['issues']} issues, {struct_results['skip']} skip")

        if struct_results["issues"] > 0:
            print("\nStructural issues:")
            for r in struct_results["details"]:
                if r["status"] == "issues":
                    for issue in r["issues"][:2]:
                        print(f"  {r['slug']}: {issue}")
            results["structural_issues"] = struct_results["issues"]
            results["issues"] += struct_results["issues"]

    # Universal image + escaped HTML validation (all sites)
    if RAW_DIR and RAW_DIR.exists():
        img_issues = 0
        html_leak_issues = 0
        for entry in index:
            slug = entry["slug"]
            parsed_path = PARSED_DIR / f"{slug}.json"
            if not parsed_path.exists():
                continue
            parsed = json.loads(parsed_path.read_text(encoding="utf-8"))

            # Check local image files exist
            for seg in parsed.get("segments", []):
                if seg.get("type") == "figure":
                    src = seg.get("image_src", "")
                    if src.startswith("data:") or src.startswith("http"):
                        continue
                    if src.startswith("../images/"):
                        local_file = RAW_DIR / "images" / src[len("../images/"):]
                        if not local_file.exists() or local_file.stat().st_size == 0:
                            img_issues += 1

            # Check for escaped HTML in non-code segment text (comment pollution)
            for seg in parsed.get("segments", []):
                if seg.get("type") in ("code", "bibtex", "table"):
                    continue  # HTML tags in code/bibtex/table are legitimate
                text = seg.get("text", "")
                if any(tag in text for tag in ("<p>", "<figure>", "<ol>", "<ul>",
                                                "<d-math>", "<script>", "</p>", "</figure>")):
                    html_leak_issues += 1
                    break  # One per article

        if img_issues > 0 or html_leak_issues > 0:
            print(f"\nUniversal checks: {img_issues} missing images, {html_leak_issues} articles with HTML pollution")
            results["issues"] += img_issues + html_leak_issues

    return results


# ── Link check ───────────────────────────────────────────────────────

def check_links(paths=None) -> dict:
    if paths is None:
        from src.config import DIST_DIR
    else:
        DIST_DIR = paths.DIST_DIR

    if not DIST_DIR.exists():
        print("Error: dist/ not found.")
        return {}

    from bs4 import BeautifulSoup

    # The only valid outside-site target is the portal index
    portal_path = (DIST_DIR.parent / "index.html").resolve()

    html_files = list(DIST_DIR.rglob("*.html"))
    file_anchors = {}
    existing_files = {}
    for html_file in html_files:
        rel = str(html_file.relative_to(DIST_DIR))
        existing_files[rel] = html_file
        soup = BeautifulSoup(html_file.read_text(encoding="utf-8"), "lxml")
        anchors = set()
        for el in soup.find_all(id=True):
            anchors.add(el["id"])
        for el in soup.find_all("a", attrs={"name": True}):
            anchors.add(el["name"])
        file_anchors[rel] = anchors

    broken = []
    total_links = 0

    for html_file in html_files:
        rel = str(html_file.relative_to(DIST_DIR))
        soup = BeautifulSoup(html_file.read_text(encoding="utf-8"), "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") or href.startswith("mailto:"):
                continue
            # Skip interactive JS app references (not part of static output)
            if "static_js/" in href or "static_html/" in href:
                continue
            total_links += 1
            if "#" in href:
                file_part, anchor_part = href.split("#", 1)
            else:
                file_part, anchor_part = href, ""

            if file_part:
                resolved = (html_file.parent / file_part).resolve()
                try:
                    target_path = str(resolved.relative_to(DIST_DIR.resolve()))
                except ValueError:
                    # Only allow the specific portal index.html
                    if resolved == portal_path and portal_path.exists():
                        continue
                    broken.append({"source": rel, "href": href, "issue": "resolves_outside_dist"})
                    continue
            else:
                target_path = rel

            target_path = target_path.replace("\\", "/")
            if file_part and target_path not in existing_files:
                broken.append({"source": rel, "href": href, "issue": f"file_not_found: {target_path}"})
                continue
            if anchor_part and target_path in file_anchors:
                if anchor_part not in file_anchors[target_path]:
                    # Demote same-page anchor misses to warnings (may be JS-generated)
                    if not file_part:
                        pass  # Same-page anchor — likely runtime-generated, skip
                    else:
                        broken.append({"source": rel, "href": href, "issue": f"anchor_not_found: #{anchor_part}"})

    print(f"Link check: {total_links} internal links, {len(broken)} broken")
    if broken:
        for b in broken[:20]:
            print(f"  {b['source']} -> {b['href']} ({b['issue']})")

    return {"total": total_links, "broken": broken}


# ── Rendered quality check ───────────────────────────────────────────

def check_rendered_quality(paths=None) -> dict:
    if paths is None:
        from src.config import DIST_DIR
    else:
        DIST_DIR = paths.DIST_DIR

    if not DIST_DIR.exists():
        return {}

    articles_dir = DIST_DIR / "articles"
    if not articles_dir.exists():
        return {}

    results = {"articles_checked": 0, "raw_placeholder_files": []}

    for html_file in articles_dir.glob("*.html"):
        html_text = html_file.read_text(encoding="utf-8")
        slug = html_file.stem
        results["articles_checked"] += 1
        if any(ph in html_text for ph in ("{{LINK:", "{{FNREF:", "{{MATH:", "{{CITE:", "{{CODE:", "{{EXTLINK|")):
            results["raw_placeholder_files"].append(slug)

    raw_count = len(results["raw_placeholder_files"])
    print(f"Rendered quality: {results['articles_checked']} articles, {raw_count} with raw placeholders")
    if raw_count > 0:
        print(f"  RAW PLACEHOLDER LEAK in: {results['raw_placeholder_files'][:10]}")

    return results
