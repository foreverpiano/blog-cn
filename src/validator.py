import json
import re
from pathlib import Path


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

    # Segment count
    src_segs = parsed.get("segments", [])
    tgt_segs = translated.get("segments", [])
    if len(src_segs) != len(tgt_segs):
        issues.append(f"segment_count_mismatch: source={len(src_segs)}, translated={len(tgt_segs)}")

    # Segment type sequence
    src_types = [(s["type"], s.get("heading_level")) for s in src_segs]
    tgt_types = [(s["type"], s.get("heading_level")) for s in tgt_segs]
    if src_types != tgt_types:
        issues.append(f"segment_type_sequence_mismatch")

    # Per-segment checks
    for i, seg in enumerate(tgt_segs):
        if i >= len(src_segs):
            break
        src_seg = src_segs[i]
        text_zh = seg.get("text_zh", "")
        text_orig = seg.get("text_original", "")

        # Code segment: text must be identical (not translated)
        if src_seg.get("type") == "code":
            if seg.get("text_zh", "") != src_seg.get("text", ""):
                issues.append(f"code_seg_{i}_text_modified")

        # Heading level must match
        if src_seg.get("type") == "heading":
            src_lvl = src_seg.get("heading_level")
            tgt_lvl = seg.get("heading_level")
            if src_lvl != tgt_lvl:
                issues.append(f"heading_seg_{i}_level_mismatch: source={src_lvl}, translated={tgt_lvl}")

        # Figure: image_src must be preserved
        if src_seg.get("type") == "figure":
            src_img = src_seg.get("image_src", "")
            tgt_img = seg.get("image_src", "")
            if src_img and not tgt_img:
                issues.append(f"figure_seg_{i}_image_src_missing")
            if src_img and not (tgt_img.startswith("http://") or tgt_img.startswith("https://")
                                or tgt_img.startswith("images/")):
                issues.append(f"figure_seg_{i}_image_src_invalid: {tgt_img[:60]}")

        # Placeholder preservation for text segments
        if src_seg.get("type") not in ("code", "figure"):
            src_fnref = len(re.findall(r'\{\{FNREF:\d+\}\}', text_orig))
            tgt_fnref = len(re.findall(r'\{\{FNREF:\d+\}\}', text_zh))
            if src_fnref != tgt_fnref:
                issues.append(f"seg_{i}_fnref_count: source={src_fnref}, translated={tgt_fnref}")

            src_link = len(re.findall(r'\{\{LINK:[^}]+\}\}', text_orig))
            tgt_link = len(re.findall(r'\{\{LINK:[^}]+\}\}', text_zh))
            if src_link != tgt_link:
                issues.append(f"seg_{i}_link_count: source={src_link}, translated={tgt_link}")

    # Footnote checks
    src_fn = len(parsed.get("footnotes", []))
    tgt_fn = len(translated.get("footnotes", []))
    if src_fn != tgt_fn:
        issues.append(f"footnote_count_mismatch: source={src_fn}, translated={tgt_fn}")

    if translated.get("slug") != parsed.get("slug"):
        issues.append("slug_modified")

    # Cross-page notes (PG-specific, safe no-op for other sites)
    p_cpn = parsed.get("cross_page_notes")
    t_cpn = translated.get("cross_page_notes")
    if p_cpn and not t_cpn:
        issues.append("cross_page_notes_missing")

    if bool(parsed.get("is_notes_page")) != bool(translated.get("is_notes_page")):
        issues.append("is_notes_page_mismatch")

    return {"slug": slug, "status": "pass" if not issues else "issues", "issues": issues}


def validate_all(paths=None) -> dict:
    if paths is None:
        from src.config import INDEX_FILE, PARSED_DIR, TRANSLATED_DIR
    else:
        INDEX_FILE = paths.INDEX_FILE
        PARSED_DIR = paths.PARSED_DIR
        TRANSLATED_DIR = paths.TRANSLATED_DIR

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

    return results


def check_links(paths=None) -> dict:
    """Check internal links. Portal links (resolving outside site dist) are allowed
    if the portal index.html exists at the project dist root."""
    if paths is None:
        from src.config import DIST_DIR
    else:
        DIST_DIR = paths.DIST_DIR

    if not DIST_DIR.exists():
        print("Error: dist/ not found.")
        return {}

    from bs4 import BeautifulSoup

    # Check if portal exists at project dist root
    project_dist = DIST_DIR.parent
    portal_exists = (project_dist / "index.html").exists()

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
                    # Link resolves outside site dist — check if it's a portal link
                    if portal_exists:
                        try:
                            resolved.relative_to(project_dist.resolve())
                            continue  # Valid portal link
                        except ValueError:
                            pass
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
