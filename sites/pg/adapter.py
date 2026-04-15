"""Paul Graham site adapter — index building + scraping."""
import json
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://www.paulgraham.com"
ARTICLES_URL = f"{BASE_URL}/articles.html"
INDEX_URL = f"{BASE_URL}/index.html"
SAME_DOMAIN_HOSTS = {"www.paulgraham.com", "paulgraham.com"}
EXCLUDE_SLUGS = {"articles", "index"}

NOISE_PATTERNS = [
    re.compile(r'Want to start a startup\?.*?Y Combinator\.?', re.DOTALL),
    re.compile(r'Get funded by\s*Y Combinator\.?'),
    re.compile(r'Thanks to .+ for reading drafts of this\.?'),
]
PROMO_COLORS = {"#ff9922", "#999999"}
NOTES_HEADING_PATTERNS = [
    "<b>Notes</b>", "<b>Notes:</b>", "<b>Note</b>", "<b>Note:</b>",
    ">Notes<", ">Notes:<", ">Note<", ">Note:<",
]


class PGAdapter:
    site_name = "pg"
    display_name = "Paul Graham 文集"
    author = "Paul Graham"
    base_url = BASE_URL
    source_label = "paulgraham.com"

    # ── Index Building ──────────────────────────────────────────────

    def build_index(self, raw_dir: Path, index_file: Path) -> list[dict]:
        print("Phase 1: Fetching seed pages...")
        articles_html = _fetch_page(ARTICLES_URL)
        if not articles_html:
            print("Error: could not fetch articles.html")
            return []
        (raw_dir / "articles.html").write_text(articles_html, encoding="utf-8")

        index_html = _fetch_page(INDEX_URL)
        if not index_html:
            print("Error: could not fetch index.html")
            return []
        (raw_dir / "index.html").write_text(index_html, encoding="utf-8")

        essay_map = _parse_articles_list(articles_html)
        essay_urls = set(essay_map.keys())
        print(f"  Found {len(essay_urls)} essays from articles.html")

        print("Phase 2: BFS discovery of all reachable pages...")
        visited = set()
        to_visit = deque()

        seed_links = _extract_links(articles_html, ARTICLES_URL) | _extract_links(index_html, INDEX_URL)
        for slug in ["faq", "bio", "books", "raq", "ind", "lisp", "info", "filters"]:
            seed_links.add(f"{BASE_URL}/{slug}.html")

        for link in seed_links:
            slug = link.split("/")[-1].replace(".html", "")
            if slug not in EXCLUDE_SLUGS:
                to_visit.append(link)

        discovered = {}
        not_found = []

        while to_visit:
            url = to_visit.popleft()
            if url in visited:
                continue
            visited.add(url)
            slug = url.split("/")[-1].replace(".html", "")
            if slug in EXCLUDE_SLUGS:
                continue

            raw_path = raw_dir / f"{slug}.html"
            if raw_path.exists():
                html = raw_path.read_text(encoding="utf-8")
            else:
                html = _fetch_page(url)
                if html is None:
                    not_found.append(url)
                    continue
                raw_path.write_text(html, encoding="utf-8")
                time.sleep(0.3)

            date = _extract_date_from_html(html)
            if url in essay_map:
                title = essay_map[url]["title"]
            else:
                soup = BeautifulSoup(html, "lxml")
                title_tag = soup.find("title")
                title = title_tag.get_text(strip=True) if title_tag else slug

            content_type = "essay" if url in essay_urls else "other"
            discovered[url] = {
                "url": url, "slug": slug, "title": title,
                "date": date, "content_type": content_type,
            }

            for link in _extract_links(html, url):
                if link not in visited:
                    ls = link.split("/")[-1].replace(".html", "")
                    if ls not in EXCLUDE_SLUGS:
                        to_visit.append(link)

            if len(discovered) % 50 == 0:
                print(f"  Discovered {len(discovered)} pages so far...")

        entries = []
        added = set()
        for url in essay_map:
            if url in discovered:
                entries.append(discovered[url])
                added.add(url)
        other = sorted([(d["slug"], url, d) for url, d in discovered.items() if url not in added])
        for _, url, d in other:
            entries.append(d)

        print(f"  Total: {len(entries)} pages")

        index_file.parent.mkdir(parents=True, exist_ok=True)
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)

        exclusion_log = index_file.parent / "exclusion_log.json"
        with open(exclusion_log, "w", encoding="utf-8") as f:
            json.dump({"not_found_404": sorted(not_found), "total_indexed": len(entries)},
                      f, ensure_ascii=False, indent=2)

        print(f"Index saved to {index_file}")
        return entries

    # ── Scraping ────────────────────────────────────────────────────

    def scrape_all(self, raw_dir: Path, parsed_dir: Path, index_file: Path) -> dict:
        if not index_file.exists():
            print("Error: index.json not found.")
            return {"success": [], "failed": []}

        with open(index_file, "r", encoding="utf-8") as f:
            index = json.load(f)

        all_slugs = {e["slug"] for e in index}

        print(f"Phase 1: Parsing {len(index)} pages...")
        parsed_results = {}
        success, failed = [], []

        for i, entry in enumerate(index):
            slug = entry["slug"]
            raw_path = raw_dir / f"{slug}.html"
            if not raw_path.exists():
                html = _fetch_page(entry["url"])
                if html is None:
                    failed.append({"slug": slug, "error": "fetch failed"})
                    continue
                raw_path.write_text(html, encoding="utf-8")
                time.sleep(0.3)
            else:
                html = raw_path.read_text(encoding="utf-8")

            parsed = _parse_article(html, entry, all_slugs)
            parsed_results[slug] = parsed
            success.append(slug)
            if (i + 1) % 100 == 0:
                print(f"  Parsed {i+1}/{len(index)}...")

        # Phase 2: cross-page notes
        print("Phase 2: Resolving cross-page notes...")
        notes_pages = {}
        for slug, parsed in parsed_results.items():
            if parsed.get("is_notes_page"):
                linked = [l["target_slug"] for seg in parsed.get("segments", [])
                          for l in seg.get("links", [])]
                notes_pages[slug] = linked

        for slug, parsed in parsed_results.items():
            if parsed.get("cross_page_notes") is None and not parsed.get("is_notes_page"):
                bare_refs = []
                for seg in parsed.get("segments", []):
                    for m in re.findall(r'\[(\d+)\]', seg.get("text", "")):
                        bare_refs.append(int(m))
                if bare_refs:
                    for ns, linked in notes_pages.items():
                        if slug in linked:
                            parsed["cross_page_notes"] = {
                                "notes_page_slug": ns,
                                "ref_numbers": sorted(set(bare_refs)),
                                "ref_count": len(bare_refs),
                            }
                            break

        # Phase 3: write to disk
        print(f"Phase 3: Writing {len(parsed_results)} parsed files...")
        for slug, parsed in parsed_results.items():
            (parsed_dir / f"{slug}.json").write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"Scraping complete: {len(success)} success, {len(failed)} failed")
        return {"success": success, "failed": failed}


# ── Private helpers ──────────────────────────────────────────────────

def _fetch_page(url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            with httpx.Client(follow_redirects=True, timeout=30) as client:
                resp = client.get(url)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.text
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def _canonical_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host or host not in SAME_DOMAIN_HOSTS:
        return None
    path = parsed.path
    if not path.endswith(".html"):
        return None
    return f"https://www.paulgraham.com{path}"


def _extract_links(html: str, base_url: str) -> set[str]:
    soup = BeautifulSoup(html, "lxml")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        full = urljoin(base_url, href)
        canon = _canonical_url(full)
        if canon:
            links.add(canon)
    return links


def _parse_articles_list(html: str) -> dict[str, dict]:
    soup = BeautifulSoup(html, "lxml")
    entries = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        full = urljoin(ARTICLES_URL, href)
        canon = _canonical_url(full)
        if not canon:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        slug = canon.split("/")[-1].replace(".html", "")
        if slug not in EXCLUDE_SLUGS:
            entries[canon] = {"title": title, "slug": slug}
    return entries


def _extract_date_from_html(html: str) -> str:
    match = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}',
        html)
    return match.group(0) if match else ""


def _find_notes_boundary(html: str) -> int:
    best = -1
    html_lower = html.lower()
    for pattern in NOTES_HEADING_PATTERNS:
        idx = html_lower.find(pattern.lower())
        if idx != -1 and (best == -1 or idx < best):
            best = idx
    return best


def _strip_promo_elements(soup: BeautifulSoup):
    to_remove = []
    for font in soup.find_all("font", color=True):
        try:
            color = font.get("color")
            if not isinstance(color, str):
                continue
            if font.find("a", href=re.compile(r"#f\d+n")):
                continue
            if color.lower() in PROMO_COLORS:
                to_remove.append(font)
        except (AttributeError, TypeError):
            continue
    to_remove.extend(soup.find_all(["script", "style", "noscript"]))
    for img in soup.find_all("img"):
        try:
            src = img.get("src", "")
            if isinstance(src, str) and any(x in src.lower() for x in ["spacer", "trans_1x1", "1x1", "virtumundo"]):
                to_remove.append(img)
        except (AttributeError, TypeError):
            continue
    for el in to_remove:
        try:
            el.decompose()
        except Exception:
            pass


def _extract_footnotes_by_anchors(html: str) -> list[dict]:
    footnotes = []
    anchor_pattern = re.compile(r'<a\s+name="(f\d+n)"')
    positions = [(m.group(1), m.start()) for m in anchor_pattern.finditer(html)]
    if not positions:
        return footnotes
    for i, (anchor_id, start_pos) in enumerate(positions):
        bracket_pos = html.find("]", start_pos)
        text_start = bracket_pos + 1 if bracket_pos != -1 else start_pos + 50
        if i + 1 < len(positions):
            text_end = positions[i + 1][1]
            bracket_before = html.rfind("[", text_start, text_end)
            if bracket_before > text_start:
                text_end = bracket_before
        else:
            text_end = min(start_pos + 5000, len(html))
        raw_text = html[text_start:text_end]
        clean = BeautifulSoup(raw_text, "lxml").get_text()
        clean = re.sub(r'\s+', ' ', clean).strip()
        if clean and len(clean) > 1:
            footnotes.append({"id": anchor_id, "text": clean})
    return footnotes


def _extract_footnotes_by_text_markers(html: str) -> list[dict]:
    footnotes = []
    notes_start = _find_notes_boundary(html)
    if notes_start == -1:
        return footnotes
    notes_html = html[notes_start:]
    notes_html = re.sub(r'<br\s*/?\s*>\s*<br\s*/?\s*>', '\n\n', notes_html)
    notes_html = re.sub(r'<br\s*/?\s*>', '\n', notes_html)
    notes_text = BeautifulSoup(notes_html, "lxml").get_text()
    marker_pattern = re.compile(r'\[(\d+)\]\s*')
    positions = [(m.group(1), m.start(), m.end()) for m in marker_pattern.finditer(notes_text)]
    for i, (num, start, text_start) in enumerate(positions):
        text_end_pos = positions[i + 1][1] if i + 1 < len(positions) else len(notes_text)
        raw = re.sub(r'\s+', ' ', notes_text[text_start:text_end_pos]).strip()
        if raw and len(raw) > 1:
            footnotes.append({"id": f"f{num}n", "text": raw})
    return footnotes


def _extract_body_segments(soup: BeautifulSoup, raw_html: str, footnote_ids: set[str] | None = None):
    body = soup.find("body")
    if not body:
        return [], []
    body_html = str(body)
    notes_idx = _find_notes_boundary(body_html)
    if notes_idx == -1:
        notes_idx = len(body_html)
    body_html = body_html[:notes_idx]
    temp_soup = BeautifulSoup(body_html, "lxml")

    internal_links = []
    for a in temp_soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#"):
            fn_match = re.match(r'#(f\d+n)', href)
            if fn_match:
                fn_num = re.match(r'f(\d+)n', fn_match.group(1)).group(1)
                a.replace_with(f"{{{{FNREF:{fn_num}}}}}")
            continue
        full = urljoin(BASE_URL + "/", href)
        if "paulgraham.com" in full and full.endswith(".html"):
            link_text = a.get_text(strip=True)
            slug = full.split("/")[-1].replace(".html", "")
            if not link_text or slug in ("index", "articles"):
                a.decompose()
                continue
            a.replace_with(f"{{{{LINK:{slug}:{link_text}}}}}")
            internal_links.append({"text": link_text, "target_slug": slug})

    body_str = str(temp_soup)
    body_str = re.sub(r'<br\s*/?\s*>\s*<br\s*/?\s*>', '\n\n', body_str)
    body_str = re.sub(r'<br\s*/?\s*>', '\n', body_str)
    raw_text = BeautifulSoup(body_str, "lxml").get_text()

    if footnote_ids:
        def replace_text_fnref(m):
            num = m.group(1)
            if f"f{num}n" in footnote_ids:
                return f"{{{{FNREF:{num}}}}}"
            return m.group(0)
        raw_text = re.sub(r'\[(\d+)\]', replace_text_fnref, raw_text)

    paragraphs = re.split(r'\n\s*\n', raw_text)
    segments = []
    for para in paragraphs:
        text = re.sub(r'\s+', ' ', para).strip()
        if not text or len(text) < 3:
            continue
        if any(p.search(text) for p in NOISE_PATTERNS):
            continue
        if len(text) < 10 and not any(c.isalpha() for c in text):
            continue
        fn_refs = re.findall(r'\{\{FNREF:(\d+)\}\}', text)
        seg_links = re.findall(r'\{\{LINK:([^:}]+):([^}]+)\}\}', text)
        segments.append({
            "index": len(segments), "type": "paragraph", "text": text,
            "footnote_refs": fn_refs,
            "links": [{"target_slug": s, "text": t} for s, t in seg_links],
        })
    return segments, internal_links


def _parse_article(html: str, index_entry: dict, all_slugs: set[str] | None = None) -> dict:
    soup = BeautifulSoup(html, "lxml")
    _strip_promo_elements(soup)

    title = index_entry.get("title", "")
    if not title:
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else index_entry["slug"]

    date = index_entry.get("date", "") or _extract_date_from_html(html)

    footnotes = _extract_footnotes_by_anchors(html)
    if not footnotes:
        footnotes = _extract_footnotes_by_text_markers(html)
    footnote_ids = {fn["id"] for fn in footnotes}

    notes_boundary = _find_notes_boundary(html)
    is_notes_page = notes_boundary != -1 and notes_boundary < 500
    if is_notes_page:
        footnotes = []
        footnote_ids = set()

    segments, internal_links = _extract_body_segments(soup, html, footnote_ids)

    return {
        "url": index_entry["url"],
        "slug": index_entry["slug"],
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
        "cross_page_notes": None,
    }
