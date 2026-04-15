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

        if src_seg.get("type") == "code":
            if seg.get("text_zh", "") != src_seg.get("text", ""):
                issues.append(f"code_seg_{i}_text_modified")

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

    # Source-vs-parsed structural validation (lethain only — uses shared content selector)
    site_name = paths.DATA_DIR.name if paths else ""
    if RAW_DIR and RAW_DIR.exists() and site_name == "lethain":
        struct_results = {"pass": 0, "issues": 0, "skip": 0, "details": []}
        for entry in index:
            sr = validate_source_vs_parsed(entry["slug"], RAW_DIR, PARSED_DIR)
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
        if "{{LINK:" in html_text or "{{FNREF:" in html_text:
            results["raw_placeholder_files"].append(slug)

    raw_count = len(results["raw_placeholder_files"])
    print(f"Rendered quality: {results['articles_checked']} articles, {raw_count} with raw placeholders")
    if raw_count > 0:
        print(f"  RAW PLACEHOLDER LEAK in: {results['raw_placeholder_files'][:10]}")

    return results
