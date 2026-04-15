"""intelligence-curse.ai adapter — small essay series with images and footnotes."""
import json
import re
import time
from copy import copy
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://intelligence-curse.ai"

# All known content pages (no sitemap available)
PAGES = [
    {"slug": "intro", "url": f"{BASE_URL}/intro/", "content_type": "essay"},
    {"slug": "pyramid", "url": f"{BASE_URL}/pyramid/", "content_type": "essay"},
    {"slug": "capital", "url": f"{BASE_URL}/capital/", "content_type": "essay"},
    {"slug": "defining", "url": f"{BASE_URL}/defining/", "content_type": "essay"},
    {"slug": "shaping", "url": f"{BASE_URL}/shaping/", "content_type": "essay"},
    {"slug": "breaking", "url": f"{BASE_URL}/breaking/", "content_type": "essay"},
    {"slug": "history", "url": f"{BASE_URL}/history/", "content_type": "essay"},
    {"slug": "about", "url": f"{BASE_URL}/about/", "content_type": "other"},
]


class IntelligenceCurseAdapter:
    site_name = "intelligence_curse"
    display_name = "The Intelligence Curse"
    author = "intelligence-curse.ai"
    base_url = BASE_URL
    source_label = "intelligence-curse.ai"

    def build_index(self, raw_dir: Path, index_file: Path) -> list[dict]:
        print(f"Fetching {len(PAGES)} pages...")
        entries = []

        for page in PAGES:
            slug = page["slug"]
            url = page["url"]

            raw_path = raw_dir / f"{slug}.html"
            if raw_path.exists():
                html = raw_path.read_text(encoding="utf-8")
            else:
                html = _fetch_page(url)
                if html is None:
                    print(f"  Failed to fetch {url}")
                    continue
                raw_path.write_text(html, encoding="utf-8")
                time.sleep(0.5)

            soup = BeautifulSoup(html, "lxml")

            # Extract title
            title_el = soup.select_one("h2.post-title")
            if title_el:
                title = title_el.get_text(strip=True)
            else:
                og_title = soup.find("meta", property="og:title")
                title = og_title["content"] if og_title else slug

            entries.append({
                "url": url,
                "slug": slug,
                "title": title,
                "date": "",
                "content_type": page["content_type"],
            })
            print(f"  [{slug}] {title}")

        # Download images
        print("Downloading images...")
        img_dir = raw_dir / "images"
        img_dir.mkdir(exist_ok=True)
        _download_all_images(raw_dir, img_dir)

        index_file.parent.mkdir(parents=True, exist_ok=True)
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)

        print(f"Index saved: {len(entries)} pages -> {index_file}")
        return entries

    def scrape_all(self, raw_dir: Path, parsed_dir: Path, index_file: Path) -> dict:
        if not index_file.exists():
            print("Error: index.json not found.")
            return {"success": [], "failed": []}

        with open(index_file, "r", encoding="utf-8") as f:
            index = json.load(f)

        all_slugs = {e["slug"] for e in index}
        img_dir = raw_dir / "images"

        print(f"Parsing {len(index)} pages...")
        success, failed = [], []

        for entry in index:
            slug = entry["slug"]
            raw_path = raw_dir / f"{slug}.html"
            if not raw_path.exists():
                failed.append({"slug": slug, "error": "raw file missing"})
                continue

            html = raw_path.read_text(encoding="utf-8")
            parsed = _parse_article(html, entry, all_slugs, img_dir)
            (parsed_dir / f"{slug}.json").write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
            success.append(slug)

        print(f"Scraping complete: {len(success)} success, {len(failed)} failed")
        return {"success": success, "failed": failed}


# ── Private helpers ──────────────────────────────────────────────────

def _fetch_page(url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            with httpx.Client(follow_redirects=True, timeout=30,
                              headers={"User-Agent": "Mozilla/5.0 (compatible; TranslationBot/1.0)"}) as client:
                resp = client.get(url)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.text
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def _download_image(url: str, save_path: Path) -> bool:
    """Download a single image file."""
    if save_path.exists():
        return True
    try:
        with httpx.Client(follow_redirects=True, timeout=30,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; TranslationBot/1.0)"}) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_bytes(resp.content)
                return True
    except Exception:
        pass
    return False


def _download_all_images(raw_dir: Path, img_dir: Path):
    """Scan all raw HTML files and download referenced images."""
    downloaded = 0
    for html_file in raw_dir.glob("*.html"):
        slug = html_file.stem
        html = html_file.read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "lxml")
        page_url = f"{BASE_URL}/{slug}/"

        for img in soup.find_all("img", src=True):
            src = img["src"]
            if src.startswith("data:") or src.startswith("http"):
                if src.startswith("http"):
                    # Absolute URL — download with original filename
                    parsed = urlparse(src)
                    filename = Path(parsed.path).name
                    if filename and not (img_dir / slug / filename).exists():
                        if _download_image(src, img_dir / slug / filename):
                            downloaded += 1
                continue

            # Resolve relative URL
            abs_url = urljoin(page_url, src)
            # Save locally preserving relative structure under slug/
            local_path = img_dir / slug / src.lstrip("./")
            if _download_image(abs_url, local_path):
                downloaded += 1
            time.sleep(0.1)

    print(f"  Downloaded {downloaded} images")


def _parse_article(html: str, index_entry: dict, all_slugs: set[str], img_dir: Path) -> dict:
    soup = BeautifulSoup(html, "lxml")

    title = index_entry.get("title", "")
    date = index_entry.get("date", "")
    slug = index_entry["slug"]

    # Find content container
    content_div = soup.select_one("div.post-content")
    if not content_div:
        content_div = soup.find("main") or soup.find("body")

    # Extract footnotes from the footnote block at the end
    footnotes = []
    footnote_div = content_div.select_one("div.footnote") if content_div else None
    if footnote_div:
        for li in footnote_div.select("ol > li"):
            fn_id = li.get("id", "")  # e.g. "fn:1"
            # Remove backref link
            for backref in li.select("a.footnote-backref"):
                backref.decompose()
            text = li.get_text(strip=True)
            if fn_id and text:
                # Convert fn:1 → f1n to match generator's expected format
                num = re.search(r'\d+', fn_id)
                normalized_id = f"f{num.group()}n" if num else fn_id.replace(":", "")
                footnotes.append({"id": normalized_id, "text": text})
        footnote_div.decompose()  # Remove from content to avoid double processing

    footnote_ids = {fn["id"] for fn in footnotes}

    # Extract segments
    segments = []
    internal_links = []

    if content_div:
        for child in content_div.children:
            if not isinstance(child, Tag):
                continue

            tag_name = child.name

            # Skip the post-title and post-meta (already extracted)
            if child.get("class") and any(c in ("post-title", "post-meta") for c in child.get("class", [])):
                continue

            if tag_name in ("h2", "h3", "h4"):
                text = _process_element(child, all_slugs, internal_links, footnote_ids)
                if text.strip():
                    seg = _make_segment(len(segments), "heading", text.strip())
                    seg["heading_level"] = int(tag_name[1])
                    segments.append(seg)

            elif tag_name == "blockquote":
                text = _process_element(child, all_slugs, internal_links, footnote_ids)
                if text.strip():
                    segments.append(_make_segment(len(segments), "blockquote", text.strip()))

            elif tag_name == "figure":
                img = child.find("img")
                caption_el = child.find("figcaption")
                if img and img.get("src"):
                    img_src = img["src"]
                    page_url = index_entry["url"]
                    abs_url = urljoin(page_url, img_src) if not img_src.startswith("http") else img_src
                    alt_text = img.get("alt", "")
                    caption = caption_el.get_text(strip=True) if caption_el else ""
                    segments.append({
                        "index": len(segments), "type": "figure",
                        "text": caption or alt_text,
                        "image_src": abs_url, "alt_text": alt_text, "caption": caption,
                        "footnote_refs": [], "links": [],
                    })

            elif tag_name in ("ul", "ol"):
                text = _process_element(child, all_slugs, internal_links, footnote_ids)
                if text.strip():
                    segments.append(_make_segment(len(segments), "list", text.strip()))

            elif tag_name == "p":
                text = _process_element(child, all_slugs, internal_links, footnote_ids)
                if text.strip() and len(text.strip()) >= 3:
                    segments.append(_make_segment(len(segments), "paragraph", text.strip()))

            elif tag_name == "hr":
                continue  # Skip horizontal rules

    return {
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
        "footnote_ref_count": sum(len(s.get("footnote_refs", [])) for s in segments),
    }


def _make_segment(index: int, seg_type: str, text: str) -> dict:
    fn_refs = re.findall(r'\{\{FNREF:(\d+)\}\}', text)
    links = [{"target_slug": s, "text": t}
             for s, t in re.findall(r'\{\{LINK:([^:}]+):([^}]*)\}\}', text)]
    return {
        "index": index, "type": seg_type, "text": text,
        "footnote_refs": fn_refs, "links": links,
    }


def _process_element(el: Tag, all_slugs: set[str], internal_links: list,
                     footnote_ids: set[str]) -> str:
    """Replace internal links and footnote refs with placeholders, extract text."""
    el = copy(el)

    # Handle footnote references: <sup id="fnref:N"><a ...>N</a></sup>
    for sup in el.find_all("sup"):
        sup_id = sup.get("id", "")
        fn_match = re.match(r'fnref:(\d+)', sup_id)
        if fn_match:
            num = fn_match.group(1)
            fn_id = f"f{num}n"
            if fn_id in footnote_ids:
                sup.replace_with(f"{{{{FNREF:{num}}}}}")
                continue

    # Handle internal links
    for a in el.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue

        parsed = urlparse(href)
        host = parsed.hostname
        path = parsed.path.strip("/")

        is_internal = False
        if not host and path and not href.startswith("http"):
            is_internal = True
        elif host and "intelligence-curse.ai" in host:
            is_internal = True

        if is_internal and path:
            slug = path.split("/")[0]
            if slug in all_slugs:
                link_text = a.get_text(strip=True)
                if link_text:
                    a.replace_with(f"{{{{LINK:{slug}:{link_text}}}}}")
                    internal_links.append({"text": link_text, "target_slug": slug})

    return el.get_text()
