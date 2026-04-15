"""Lethain.com (Will Larson) site adapter — sitemap index + semantic HTML parsing."""
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup, Tag


BASE_URL = "https://lethain.com"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"

# URL path prefixes to exclude (not articles)
EXCLUDE_PREFIXES = (
    "/tags/", "/categories/", "/about/", "/talks/", "/books/",
    "/newsletter/", "/feeds", "/featured/", "/posts/",
)
EXCLUDE_EXACT = {"/", ""}


class LethainAdapter:
    site_name = "lethain"
    display_name = "Will Larson 文集"
    author = "Will Larson"
    base_url = BASE_URL
    source_label = "lethain.com"

    # ── Index Building ──────────────────────────────────────────────

    def build_index(self, raw_dir: Path, index_file: Path) -> list[dict]:
        print("Phase 1: Fetching sitemap...")
        sitemap_xml = _fetch_page(SITEMAP_URL)
        if not sitemap_xml:
            print("Error: could not fetch sitemap.xml")
            return []

        # Parse sitemap
        root = ElementTree.fromstring(sitemap_xml)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        all_urls = [loc.text for loc in root.findall(".//sm:loc", ns) if loc.text]

        # Filter to article URLs only
        article_urls = []
        for url in all_urls:
            parsed = urlparse(url)
            path = parsed.path.rstrip("/")
            if not path or path in EXCLUDE_EXACT:
                continue
            if any(path.startswith(p.rstrip("/")) for p in EXCLUDE_PREFIXES):
                continue
            # Article URLs are single-level paths: /some-slug/
            parts = [p for p in path.split("/") if p]
            if len(parts) == 1:
                article_urls.append(url)

        print(f"  Found {len(article_urls)} article URLs from sitemap")

        # Fetch each article to extract title and date
        print("Phase 2: Fetching article metadata...")
        entries = []
        for i, url in enumerate(article_urls):
            parsed_url = urlparse(url)
            slug = parsed_url.path.strip("/")

            raw_path = raw_dir / f"{slug}.html"
            if raw_path.exists():
                html = raw_path.read_text(encoding="utf-8")
            else:
                html = _fetch_page(url)
                if html is None:
                    continue
                raw_path.write_text(html, encoding="utf-8")
                time.sleep(0.2)

            soup = BeautifulSoup(html, "lxml")

            # Extract title
            h1 = soup.find("h1")
            title = h1.get_text(strip=True) if h1 else slug

            # Extract date
            time_tag = soup.find("time")
            date = ""
            if time_tag:
                dt = time_tag.get("datetime", "")
                if dt:
                    date = dt[:10]  # YYYY-MM-DD
                else:
                    date = time_tag.get_text(strip=True)

            entries.append({
                "url": url,
                "slug": slug,
                "title": title,
                "date": date,
                "content_type": "post",
            })

            if (i + 1) % 100 == 0:
                print(f"  Indexed {i+1}/{len(article_urls)}...")

        # Sort by date descending
        entries.sort(key=lambda e: e.get("date", ""), reverse=True)

        index_file.parent.mkdir(parents=True, exist_ok=True)
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)

        print(f"Index saved: {len(entries)} articles -> {index_file}")
        return entries

    # ── Scraping ────────────────────────────────────────────────────

    def scrape_all(self, raw_dir: Path, parsed_dir: Path, index_file: Path) -> dict:
        if not index_file.exists():
            print("Error: index.json not found.")
            return {"success": [], "failed": []}

        with open(index_file, "r", encoding="utf-8") as f:
            index = json.load(f)

        all_slugs = {e["slug"] for e in index}

        print(f"Parsing {len(index)} articles...")
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
                time.sleep(0.2)
            else:
                html = raw_path.read_text(encoding="utf-8")

            parsed = _parse_article(html, entry, all_slugs)
            (parsed_dir / f"{slug}.json").write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
            success.append(slug)

            if (i + 1) % 100 == 0:
                print(f"  Parsed {i+1}/{len(index)}...")

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


def _parse_article(html: str, index_entry: dict, all_slugs: set[str]) -> dict:
    soup = BeautifulSoup(html, "lxml")

    title = index_entry.get("title", "")
    date = index_entry.get("date", "")

    # Find content container
    content_div = soup.select_one("div.nested-copy-line-height.lh-copy.serif")
    if not content_div:
        # Fallback: try article tag
        content_div = soup.find("article")
    if not content_div:
        content_div = soup.find("main") or soup.find("body")

    # Strip noise elements
    if content_div:
        for aside in content_div.find_all("aside"):
            aside.decompose()
        for nav in content_div.find_all("nav"):
            nav.decompose()
        # Remove newsletter signup forms
        for form in content_div.find_all("form"):
            form.decompose()
        # Remove elements with "newsletter" or "subscribe" in class
        for el in content_div.find_all(class_=re.compile(r'newsletter|subscribe', re.I)):
            el.decompose()

    # Extract segments via recursive block walker
    entry_url = index_entry["url"]
    segments = []
    internal_links = []

    if content_div:
        _walk_block(content_div, segments, all_slugs, internal_links, entry_url)

    return {
        "url": index_entry["url"],
        "slug": index_entry["slug"],
        "title": title,
        "date": date,
        "content_type": "post",
        "segments": segments,
        "footnotes": [],
        "internal_links": internal_links,
        "paragraph_count": len(segments),
        "footnote_count": 0,
        "footnote_ref_count": 0,
    }


def _get_code_text(pre: Tag) -> str:
    """Extract code text from a <pre> element.
    Use only a direct child <code> (recursive=False); otherwise use pre's own text."""
    code_el = pre.find("code", recursive=False)
    return code_el.get_text() if code_el else pre.get_text()


def _walk_block(el: Tag, segments: list, all_slugs: set[str],
                internal_links: list, entry_url: str,
                _emitted_pres: set | None = None, _emitted_imgs: set | None = None):
    """Recursively walk a block element tree, emitting segments in document order.

    Never mutates the source tree. Tracks emitted pre/img nodes by id() to avoid
    double-emitting when the same node is reachable from multiple paths.
    """
    if _emitted_pres is None:
        _emitted_pres = set()
    if _emitted_imgs is None:
        _emitted_imgs = set()

    for child in el.children:
        if not isinstance(child, Tag):
            continue

        tag = child.name

        # Headings
        if tag in ("h2", "h3", "h4"):
            text = _process_element_text(child, all_slugs, internal_links)
            if text.strip():
                seg = _make_seg(len(segments), "heading", text.strip())
                seg["heading_level"] = int(tag[1])
                segments.append(seg)

        # Code blocks — emit all <pre> nodes including nested ones
        elif tag == "pre":
            if id(child) not in _emitted_pres:
                _emitted_pres.add(id(child))
                code_text = _get_code_text(child)
                if code_text.strip():
                    segments.append({
                        "index": len(segments), "type": "code",
                        "text": code_text, "footnote_refs": [], "links": [],
                    })
                # Also emit any nested <pre> nodes (highlight wrappers)
                for nested_pre in child.find_all("pre"):
                    if id(nested_pre) not in _emitted_pres:
                        _emitted_pres.add(id(nested_pre))
                        ct = _get_code_text(nested_pre)
                        if ct.strip():
                            segments.append({
                                "index": len(segments), "type": "code",
                                "text": ct, "footnote_refs": [], "links": [],
                            })

        # Figure elements
        elif tag == "figure":
            _extract_figure(child, segments, entry_url)
            for img in child.find_all("img", src=True):
                _emitted_imgs.add(id(img))

        # Standalone <img> (not inside p or figure)
        elif tag == "img" and child.get("src"):
            if id(child) not in _emitted_imgs:
                _emitted_imgs.add(id(child))
                _extract_img(child, segments, entry_url)

        # Paragraphs
        elif tag == "p":
            img = child.find("img")
            if img and img.get("src") and id(img) not in _emitted_imgs:
                _emitted_imgs.add(id(img))
                _extract_img(img, segments, entry_url)
                # Also extract text if there's meaningful content beyond the image
                clone = _clone_without(child, {"pre", "img", "figure"})
                text = _process_element_text(clone, all_slugs, internal_links)
                if text.strip() and len(text.strip()) >= 3:
                    segments.append(_make_seg(len(segments), "paragraph", text.strip()))
            else:
                text = _process_element_text(child, all_slugs, internal_links)
                if text.strip() and len(text.strip()) >= 3:
                    segments.append(_make_seg(len(segments), "paragraph", text.strip()))

        # Blockquotes — emit nested pre/img first, then text from cleaned clone
        elif tag == "blockquote":
            for nested_pre in child.find_all("pre"):
                if id(nested_pre) not in _emitted_pres:
                    _emitted_pres.add(id(nested_pre))
                    ct = _get_code_text(nested_pre)
                    if ct.strip():
                        segments.append({
                            "index": len(segments), "type": "code",
                            "text": ct, "footnote_refs": [], "links": [],
                        })
            for nested_img in child.find_all("img", src=True):
                if id(nested_img) not in _emitted_imgs:
                    _emitted_imgs.add(id(nested_img))
                    _extract_img(nested_img, segments, entry_url)
            clone = _clone_without(child, {"pre", "img", "figure"})
            text = _process_element_text(clone, all_slugs, internal_links)
            if text.strip():
                segments.append(_make_seg(len(segments), "blockquote", text.strip()))

        # Lists — emit nested pre/img first, then text from cleaned clone
        elif tag in ("ul", "ol"):
            for nested_pre in child.find_all("pre"):
                if id(nested_pre) not in _emitted_pres:
                    _emitted_pres.add(id(nested_pre))
                    ct = _get_code_text(nested_pre)
                    if ct.strip():
                        segments.append({
                            "index": len(segments), "type": "code",
                            "text": ct, "footnote_refs": [], "links": [],
                        })
            for nested_img in child.find_all("img", src=True):
                if id(nested_img) not in _emitted_imgs:
                    _emitted_imgs.add(id(nested_img))
                    _extract_img(nested_img, segments, entry_url)
            clone = _clone_without(child, {"pre", "img", "figure"})
            text = _process_element_text(clone, all_slugs, internal_links)
            if text.strip():
                segments.append(_make_seg(len(segments), "list", text.strip()))

        # Container elements — recurse into
        elif tag in ("div", "section", "article", "main", "span",
                     "li", "dd", "dt", "td", "th", "details", "summary"):
            _walk_block(child, segments, all_slugs, internal_links, entry_url,
                        _emitted_pres, _emitted_imgs)

        # Unknown tags with children — recurse
        elif list(child.children):
            _walk_block(child, segments, all_slugs, internal_links, entry_url,
                        _emitted_pres, _emitted_imgs)


def _clone_without(el: Tag, strip_tags: set) -> Tag:
    """Create a deep copy of an element with specified tag types removed."""
    from copy import copy
    clone = copy(el)
    for tag_name in strip_tags:
        for node in clone.find_all(tag_name):
            node.decompose()
    return clone


def _make_seg(index: int, seg_type: str, text: str) -> dict:
    fn_refs = re.findall(r'\{\{FNREF:(\d+)\}\}', text)
    links = [{"target_slug": s, "text": t}
             for s, t in re.findall(r'\{\{LINK:([^:}]+):([^}]*)\}\}', text)]
    return {
        "index": index, "type": seg_type, "text": text,
        "footnote_refs": fn_refs, "links": links,
    }


def _process_element_text(el: Tag, all_slugs: set[str], internal_links: list) -> str:
    """Replace internal links with placeholders, then extract text."""
    # Clone to avoid mutating the original
    from copy import copy
    el = copy(el)

    for a in el.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        # Check if it's an internal lethain.com link
        parsed = urlparse(href)
        host = parsed.hostname
        path = parsed.path.strip("/")

        is_internal = False
        if not host and path and not path.startswith("http"):
            # Relative link
            is_internal = True
        elif host and ("lethain.com" in host):
            is_internal = True

        if is_internal and path:
            slug = path.split("/")[0] if "/" in path else path
            if slug in all_slugs:
                link_text = a.get_text(strip=True)
                if link_text:
                    a.replace_with(f"{{{{LINK:{slug}:{link_text}}}}}")
                    internal_links.append({"text": link_text, "target_slug": slug})
                    continue

    return el.get_text()


def _extract_seg_links(text: str) -> list[dict]:
    """Extract link placeholders from segment text."""
    links = []
    for m in re.findall(r'\{\{LINK:([^:}]+):([^}]*)\}\}', text):
        links.append({"target_slug": m[0], "text": m[1]})
    return links


def _extract_figure(el: Tag, segments: list, page_url: str):
    """Extract a figure element (img + optional caption). Skip tracking pixels."""
    img = el.find("img")
    if not img or not img.get("src") or _is_tracking_pixel(img["src"]):
        return
    src = img["src"]
    abs_url = urljoin(page_url, src) if not src.startswith("http") else src
    alt_text = img.get("alt", "")
    caption_el = el.find("figcaption")
    caption = caption_el.get_text(strip=True) if caption_el else ""
    segments.append({
        "index": len(segments), "type": "figure",
        "text": caption or alt_text,
        "image_src": abs_url, "alt_text": alt_text, "caption": caption,
        "footnote_refs": [], "links": [],
    })


def _is_tracking_pixel(src: str) -> bool:
    lower = src.lower()
    return any(x in lower for x in [
        "assoc-amazon.com", "amazon-adsystem.com", "doubleclick.net",
        "1x1", "pixel", "beacon", "spacer", "trans_1x1",
    ])


def _extract_img(img: Tag, segments: list, page_url: str):
    """Extract a standalone img element. Skip tracking pixels."""
    src = img.get("src", "")
    if not src or _is_tracking_pixel(src):
        return
    abs_url = urljoin(page_url, src) if not src.startswith("http") else src
    alt_text = img.get("alt", "")
    segments.append({
        "index": len(segments), "type": "figure",
        "text": alt_text,
        "image_src": abs_url, "alt_text": alt_text, "caption": "",
        "footnote_refs": [], "links": [],
    })
