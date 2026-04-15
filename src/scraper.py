import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup, NavigableString, Tag
from src.config import RAW_DIR, PARSED_DIR, INDEX_FILE, BASE_URL

# Patterns to strip from body text
NOISE_PATTERNS = [
    re.compile(r'Want to start a startup\?.*?Y Combinator\.?', re.DOTALL),
    re.compile(r'Get funded by\s*Y Combinator\.?'),
    re.compile(r'Thanks to .+ for reading drafts of this\.?'),
]

# Font colors used for promotional/navigation elements (orange YC sidebar)
PROMO_COLORS = {"#ff9922", "#999999"}

# All known Notes section heading patterns in PG HTML (case-insensitive search)
NOTES_HEADING_PATTERNS = [
    "<b>Notes</b>", "<b>Notes:</b>", "<b>Note</b>", "<b>Note:</b>",
    ">Notes<", ">Notes:<", ">Note<", ">Note:<",
]


def find_notes_boundary(html: str) -> int:
    """Find the index where the Notes/Note section begins in the HTML.
    Returns -1 if no notes section found."""
    best = -1
    html_lower = html.lower()
    for pattern in NOTES_HEADING_PATTERNS:
        idx = html_lower.find(pattern.lower())
        if idx != -1 and (best == -1 or idx < best):
            best = idx
    return best


def fetch_page(url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            with httpx.Client(follow_redirects=True, timeout=30) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.text
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"  Failed to fetch {url}: {e}")
                return None


def extract_date(soup: BeautifulSoup) -> str:
    text = soup.get_text()
    match = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}',
        text
    )
    return match.group(0) if match else ""


def strip_promo_elements(soup: BeautifulSoup):
    """Remove promotional/navigation elements before content extraction."""
    to_remove = []

    # Collect font elements with promo colors (YC sidebar),
    # but NEVER remove any element containing footnote ref links
    for font in soup.find_all("font", color=True):
        try:
            color = font.get("color")
            if not isinstance(color, str):
                continue
            # Skip any font element containing a footnote reference, regardless of color
            if font.find("a", href=re.compile(r"#f\d+n")):
                continue
            if color.lower() in PROMO_COLORS:
                to_remove.append(font)
        except (AttributeError, TypeError):
            continue

    # Collect script/style/noscript
    to_remove.extend(soup.find_all(["script", "style", "noscript"]))

    # Collect spacer images
    for img in soup.find_all("img"):
        try:
            src = img.get("src", "")
            if isinstance(src, str) and any(x in src.lower() for x in ["spacer", "trans_1x1", "1x1", "virtumundo"]):
                to_remove.append(img)
        except (AttributeError, TypeError):
            continue

    # Remove all collected elements
    for el in to_remove:
        try:
            el.decompose()
        except Exception:
            pass


def find_content_node(soup: BeautifulSoup) -> Tag | None:
    """Find the main content container, excluding navigation/promo areas."""
    body = soup.find("body")
    if not body:
        return None

    # Strategy: find the largest <font> or <td> by text length
    # but exclude ones that are entirely navigation
    candidates = []

    for font in body.find_all("font"):
        text = font.get_text(strip=True)
        if len(text) > 200:
            candidates.append((len(text), font))

    if not candidates:
        for td in body.find_all("td"):
            text = td.get_text(strip=True)
            if len(text) > 200:
                candidates.append((len(text), td))

    if not candidates:
        return body

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def extract_footnotes_by_anchors(html: str) -> list[dict]:
    """Extract footnotes by finding text between consecutive footnote anchor markers."""
    footnotes = []

    # Find all footnote anchor positions using <a name="fNn">
    anchor_pattern = re.compile(r'<a\s+name="(f\d+n)"')
    positions = [(m.group(1), m.start()) for m in anchor_pattern.finditer(html)]

    if not positions:
        return footnotes

    for i, (anchor_id, start_pos) in enumerate(positions):
        # Text runs from after the "]" that closes this footnote marker
        # to the start of the next footnote marker or end of content
        # Find the "]" after this anchor
        bracket_pos = html.find("]", start_pos)
        if bracket_pos == -1:
            text_start = start_pos + 50  # fallback
        else:
            text_start = bracket_pos + 1

        if i + 1 < len(positions):
            text_end = positions[i + 1][1]
            # Back up to before the "[" that starts the next marker
            bracket_before = html.rfind("[", text_start, text_end)
            if bracket_before > text_start:
                text_end = bracket_before
        else:
            # Last footnote: take until end of the content container
            text_end = min(start_pos + 5000, len(html))

        raw_text = html[text_start:text_end]

        # Clean HTML
        clean = BeautifulSoup(raw_text, "lxml").get_text()
        clean = re.sub(r'\s+', ' ', clean).strip()

        if clean and len(clean) > 1:
            footnotes.append({
                "id": anchor_id,
                "text": clean,
            })

    return footnotes


def extract_footnotes_by_text_markers(html: str) -> list[dict]:
    """Fallback: extract footnotes from Notes section using plain [N] text markers."""
    footnotes = []

    notes_start = find_notes_boundary(html)
    if notes_start == -1:
        return footnotes

    notes_html = html[notes_start:]

    # Normalize br tags
    notes_html = re.sub(r'<br\s*/?\s*>\s*<br\s*/?\s*>', '\n\n', notes_html)
    notes_html = re.sub(r'<br\s*/?\s*>', '\n', notes_html)

    # Get plain text
    notes_text = BeautifulSoup(notes_html, "lxml").get_text()

    # Find [N] markers and extract text between them
    marker_pattern = re.compile(r'\[(\d+)\]\s*')
    positions = [(m.group(1), m.start(), m.end()) for m in marker_pattern.finditer(notes_text)]

    for i, (num, start, text_start) in enumerate(positions):
        if i + 1 < len(positions):
            text_end = positions[i + 1][0]
            text_end_pos = positions[i + 1][1]
        else:
            text_end_pos = len(notes_text)

        raw = notes_text[text_start:text_end_pos].strip()
        raw = re.sub(r'\s+', ' ', raw)

        if raw and len(raw) > 1:
            footnotes.append({
                "id": f"f{num}n",
                "text": raw,
            })

    return footnotes


def extract_footnote_refs(html: str, footnote_ids: set[str] | None = None) -> list[str]:
    """Count footnote references in the body (before Notes section).

    Only counts [N] as footnote refs if a matching footnote exists.
    """
    notes_idx = find_notes_boundary(html)
    body_html = html[:notes_idx] if notes_idx > 0 else html

    # Try anchor-based refs first
    refs = re.findall(r'<a\s+href="#(f\d+n)"', body_html)
    if refs:
        return refs

    # Fallback: count [N] in body plain text, but ONLY if matching footnote exists
    if not footnote_ids:
        return []
    body_text = BeautifulSoup(body_html, "lxml").get_text()
    text_refs = []
    for n in re.findall(r'\[(\d+)\]', body_text):
        fn_id = f"f{n}n"
        if fn_id in footnote_ids:
            text_refs.append(fn_id)
    return text_refs


def extract_body_segments(soup: BeautifulSoup, raw_html: str, _footnote_ids: set[str] | None = None) -> list[dict]:
    """Extract body text as structured paragraphs, excluding footnotes section.

    Uses the full page body (not just a content node) to avoid missing
    fnref anchors that live in sibling font/td elements.
    """
    body = soup.find("body")
    if not body:
        return [], []

    # Work with the full body HTML, cut at Notes boundary
    body_html = str(body)
    notes_idx = find_notes_boundary(body_html)
    if notes_idx == -1:
        notes_idx = len(body_html)

    body_html = body_html[:notes_idx]

    # Parse body HTML
    temp_soup = BeautifulSoup(body_html, "lxml")

    # Replace internal links with placeholders BEFORE converting to text
    internal_links = []
    for a in temp_soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#"):
            # Footnote reference: replace with placeholder
            fn_match = re.match(r'#(f\d+n)', href)
            if fn_match:
                fn_id = fn_match.group(1)
                fn_num = re.match(r'f(\d+)n', fn_id).group(1)
                a.replace_with(f"{{{{FNREF:{fn_num}}}}}")
            continue
        full = urljoin(BASE_URL + "/", href)
        if "paulgraham.com" in full and full.endswith(".html"):
            link_text = a.get_text(strip=True)
            slug = full.split("/")[-1].replace(".html", "")
            # Skip links with no visible text (navigation images, etc.)
            if not link_text or slug in ("index", "articles"):
                a.decompose()
                continue
            # Replace <a> with placeholder in DOM
            a.replace_with(f"{{{{LINK:{slug}:{link_text}}}}}")
            internal_links.append({
                "text": link_text,
                "target_slug": slug,
            })

    # Now convert to text with placeholders preserved
    # Normalize br tags
    body_str = str(temp_soup)
    body_str = re.sub(r'<br\s*/?\s*>\s*<br\s*/?\s*>', '\n\n', body_str)
    body_str = re.sub(r'<br\s*/?\s*>', '\n', body_str)

    raw_text = BeautifulSoup(body_str, "lxml").get_text()

    # Convert remaining plain text [N] to {{FNREF:N}} if article has matching footnotes.
    # This handles pages where footnote refs are plain text, not <a href="#fNn">.
    # We pass footnote_ids from the caller to know which [N] are real refs.
    if _footnote_ids:
        def replace_text_fnref(m):
            num = m.group(1)
            fn_id = f"f{num}n"
            if fn_id in _footnote_ids:
                return f"{{{{FNREF:{num}}}}}"
            return m.group(0)
        raw_text = re.sub(r'\[(\d+)\]', replace_text_fnref, raw_text)

    paragraphs = re.split(r'\n\s*\n', raw_text)

    segments = []
    for para in paragraphs:
        text = para.strip()
        text = re.sub(r'\s+', ' ', text)

        if not text or len(text) < 3:
            continue

        # Filter noise
        is_noise = False
        for pattern in NOISE_PATTERNS:
            if pattern.search(text):
                is_noise = True
                break
        if is_noise:
            continue

        # Skip very short navigational text
        if len(text) < 10 and not any(c.isalpha() for c in text):
            continue

        # Count footnote ref placeholders in this paragraph
        fn_refs = re.findall(r'\{\{FNREF:(\d+)\}\}', text)

        # Count link placeholders in this paragraph
        seg_links = re.findall(r'\{\{LINK:([^:}]+):([^}]+)\}\}', text)

        segments.append({
            "index": len(segments),
            "type": "paragraph",
            "text": text,
            "footnote_refs": fn_refs,
            "links": [{"target_slug": slug, "text": lt} for slug, lt in seg_links],
        })

    return segments, internal_links


def extract_title_from_page(soup: BeautifulSoup) -> str:
    title_tag = soup.find("title")
    if title_tag:
        return title_tag.get_text(strip=True)
    for tag in ["h1", "h2", "h3"]:
        heading = soup.find(tag)
        if heading:
            return heading.get_text(strip=True)
    return ""


def parse_article(html: str, index_entry: dict, all_slugs: set[str] | None = None) -> dict:
    """Parse a single article page into structured content."""
    soup = BeautifulSoup(html, "lxml")

    # Strip promo/nav elements first
    strip_promo_elements(soup)

    title = index_entry.get("title", "") or extract_title_from_page(soup)
    date = index_entry.get("date", "") or extract_date(soup)

    content_node = find_content_node(soup)

    # Extract footnotes FIRST so we know which [N] are real refs
    footnotes = extract_footnotes_by_anchors(html)
    if not footnotes:
        footnotes = extract_footnotes_by_text_markers(html)
    footnote_ids = {fn["id"] for fn in footnotes}

    # For notes-only pages (Notes heading very early), the page IS the notes.
    # Don't convert body [N] to FNREF and don't keep separate footnotes (prevents double rendering).
    notes_boundary = find_notes_boundary(html)
    is_notes_page = notes_boundary != -1 and notes_boundary < 500
    if is_notes_page:
        footnotes = []  # Body IS the notes, no separate footnote section
        footnote_ids = set()

    footnote_refs = extract_footnote_refs(html, footnote_ids)
    segments, internal_links = extract_body_segments(soup, html, footnote_ids)

    slug = index_entry["slug"]

    result = {
        "url": index_entry["url"],
        "slug": slug,
        "title": title,
        "date": date,
        "content_type": index_entry.get("content_type", "essay"),
        "segments": segments,
        "footnotes": footnotes,
        "internal_links": internal_links,
        "paragraph_count": len(segments),
        "footnote_count": len(footnotes),
        "footnote_ref_count": sum(len(seg.get("footnote_refs", [])) for seg in segments),
        "is_notes_page": is_notes_page,
        "cross_page_notes": None,  # Populated in phase 2 of scrape_all
    }

    return result


def get_index_slugs() -> set[str]:
    """Get all slugs from index.json for companion page validation."""
    if not INDEX_FILE.exists():
        return set()
    with open(INDEX_FILE) as f:
        return {e["slug"] for e in json.load(f)}


def scrape_all() -> dict:
    """Scrape all indexed pages."""
    if not INDEX_FILE.exists():
        print("Error: index.json not found. Run index_builder first.")
        return {"success": [], "failed": []}

    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        index = json.load(f)

    all_slugs = {e["slug"] for e in index}

    # PHASE 1: Parse all pages into memory (no cross_page_notes yet)
    print(f"Phase 1: Parsing {len(index)} pages...")
    parsed_results = {}  # slug -> parsed dict
    success = []
    failed = []

    for i, entry in enumerate(index):
        slug = entry["slug"]
        raw_path = RAW_DIR / f"{slug}.html"

        if not raw_path.exists():
            html = fetch_page(entry["url"])
            if html is None:
                failed.append({"slug": slug, "url": entry["url"], "error": "fetch failed"})
                continue
            raw_path.write_text(html, encoding="utf-8")
            time.sleep(0.3)
        else:
            html = raw_path.read_text(encoding="utf-8")

        parsed = parse_article(html, entry, all_slugs)
        parsed_results[slug] = parsed
        success.append(slug)

        if (i + 1) % 100 == 0:
            print(f"  Parsed {i+1}/{len(index)}...")

    # PHASE 2: Derive cross_page_notes from in-memory results
    print(f"Phase 2: Resolving cross-page notes relationships...")
    # Build notes-page lookup: {slug: [target_slugs_it_links_to]}
    notes_pages = {}
    for slug, parsed in parsed_results.items():
        if parsed.get("is_notes_page"):
            linked_slugs = [l["target_slug"] for seg in parsed.get("segments", [])
                            for l in seg.get("links", [])]
            notes_pages[slug] = linked_slugs

    # For each article with bare [N] refs, find its companion notes page
    for slug, parsed in parsed_results.items():
        if parsed.get("cross_page_notes") is None and not parsed.get("is_notes_page"):
            bare_refs = []
            for seg in parsed.get("segments", []):
                for m in re.findall(r'\[(\d+)\]', seg.get("text", "")):
                    bare_refs.append(int(m))
            if bare_refs:
                # Find a notes page that links back to this article
                for notes_slug, linked in notes_pages.items():
                    if slug in linked:
                        parsed["cross_page_notes"] = {
                            "notes_page_slug": notes_slug,
                            "ref_numbers": sorted(set(bare_refs)),
                            "ref_count": len(bare_refs),
                        }
                        break

    # PHASE 3: Write all parsed results to disk
    print(f"Phase 3: Writing {len(parsed_results)} parsed files...")
    for slug, parsed in parsed_results.items():
        parsed_path = PARSED_DIR / f"{slug}.json"
        with open(parsed_path, "w", encoding="utf-8") as f:
            json.dump(parsed, f, ensure_ascii=False, indent=2)

    print(f"Scraping complete: {len(success)} success, {len(failed)} failed")

    print(f"\nScraping complete: {len(success)} success, {len(failed)} failed")
    if failed:
        failed_path = PARSED_DIR / "_failed.json"
        with open(failed_path, "w", encoding="utf-8") as f:
            json.dump(failed, f, ensure_ascii=False, indent=2)
        print(f"Failed pages saved to {failed_path}")

    return {"success": success, "failed": failed}


if __name__ == "__main__":
    scrape_all()
