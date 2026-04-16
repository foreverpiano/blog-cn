"""lilianweng.github.io adapter — Lilian Weng's ML blog (Hugo static site)."""
import json
import re
import time
from copy import copy
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup, Tag
from src.extraction_rules import get_code_text, is_empty_pre, is_tracking_pixel

BASE_URL = "https://lilianweng.github.io"


class LilianWengAdapter:
    site_name = "lilianweng"
    display_name = "Lil'Log (Lilian Weng)"
    author = "Lilian Weng"
    base_url = BASE_URL
    source_label = "lilianweng.github.io"

    def build_index(self, raw_dir: Path, index_file: Path) -> list[dict]:
        print("Fetching RSS feed...")
        rss_xml = _fetch_page(f"{BASE_URL}/index.xml")
        if not rss_xml:
            print("Error: could not fetch RSS feed")
            return []

        root = ElementTree.fromstring(rss_xml)
        entries = []

        for item in root.findall(".//item"):
            title_el = item.find("title")
            link_el = item.find("link")
            pub_date_el = item.find("pubDate")

            if link_el is None or not link_el.text:
                continue

            url = link_el.text.strip()
            parsed_url = urlparse(url)
            if parsed_url.hostname and "lilianweng.github.io" not in parsed_url.hostname:
                continue

            title = title_el.text.strip() if (title_el is not None and title_el.text) else ""
            date = ""
            if pub_date_el is not None and pub_date_el.text:
                # Parse RFC 822 date to YYYY-MM-DD
                import email.utils
                try:
                    dt = email.utils.parsedate_to_datetime(pub_date_el.text)
                    date = dt.strftime("%Y-%m-%d")
                except Exception:
                    date = pub_date_el.text[:10]

            # Extract slug from URL path
            path = parsed_url.path.strip("/")
            slug = path.replace("/", "-").rstrip("-")
            if not slug:
                continue

            # Fetch and save raw HTML
            raw_path = raw_dir / f"{slug}.html"
            if not raw_path.exists():
                html = _fetch_page(url)
                if html is None:
                    continue
                raw_path.write_text(html, encoding="utf-8")
                time.sleep(0.3)

            entries.append({
                "url": url,
                "slug": slug,
                "title": title,
                "date": date,
                "content_type": "post",
            })
            if len(entries) % 10 == 0:
                print(f"  Indexed {len(entries)} articles...")

        entries.sort(key=lambda e: e.get("date", ""), reverse=True)

        index_file.parent.mkdir(parents=True, exist_ok=True)
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)

        print(f"Index saved: {len(entries)} articles -> {index_file}")
        return entries

    def scrape_all(self, raw_dir: Path, parsed_dir: Path, index_file: Path) -> dict:
        if not index_file.exists():
            print("Error: index.json not found.")
            return {"success": [], "failed": []}

        with open(index_file, "r", encoding="utf-8") as f:
            index = json.load(f)

        all_slugs = {e["slug"] for e in index}
        img_dir = raw_dir / "images"
        img_dir.mkdir(exist_ok=True)

        print(f"Parsing {len(index)} articles...")
        success, failed = [], []

        for i, entry in enumerate(index):
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

            if (i + 1) % 10 == 0:
                print(f"  Parsed {i+1}/{len(index)}...")

        print(f"Scraping complete: {len(success)} success, {len(failed)} failed")
        return {"success": success, "failed": failed}


def _fetch_page(url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            with httpx.Client(follow_redirects=True, timeout=30,
                              headers={"User-Agent": "Mozilla/5.0 (compatible; TranslationBot/1.0)"}) as client:
                resp = client.get(url)
                if resp.status_code in (404, 403):
                    return None
                resp.raise_for_status()
                return resp.text
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def _download_image(url: str, save_path: Path) -> bool:
    if save_path.exists() and save_path.stat().st_size > 0:
        return True
    try:
        with httpx.Client(follow_redirects=True, timeout=30,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; TranslationBot/1.0)"}) as client:
            resp = client.get(url)
            if resp.status_code == 200 and len(resp.content) > 0:
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_bytes(resp.content)
                return True
    except Exception:
        pass
    return False


def _parse_article(html: str, index_entry: dict, all_slugs: set[str],
                   img_dir: Path) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = index_entry.get("title", "")
    date = index_entry.get("date", "")
    slug = index_entry["slug"]
    page_url = index_entry["url"]

    # Find content: <article> or .post-content
    content = soup.find("article")
    if not content:
        content = soup.select_one(".post-content")
    if not content:
        content = soup.find("main") or soup.find("body")

    if not content:
        return _empty_parsed(index_entry)

    # Clone and preprocess
    content_copy = copy(content)

    # Process math: MathJax uses $...$ in the raw HTML text
    math_registry = {}
    _process_mathjax(content_copy, math_registry)

    # Walk content
    segments = []
    internal_links = []
    _walk_block(content_copy, segments, all_slugs, internal_links,
                page_url, slug, img_dir, math_registry)

    return {
        "url": page_url,
        "slug": slug,
        "title": title,
        "date": date,
        "content_type": "post",
        "segments": segments,
        "footnotes": [],
        "internal_links": internal_links,
        "paragraph_count": len(segments),
        "footnote_count": 0,
        "footnote_ref_count": 0,
        "math_registry": math_registry,
    }


def _empty_parsed(entry: dict) -> dict:
    return {
        "url": entry["url"], "slug": entry["slug"],
        "title": entry.get("title", ""), "date": entry.get("date", ""),
        "content_type": "post",
        "segments": [], "footnotes": [], "internal_links": [],
        "paragraph_count": 0, "footnote_count": 0, "footnote_ref_count": 0,
        "math_registry": {},
    }


def _process_mathjax(content, math_registry: dict):
    """Replace MathJax rendered output with MATH placeholders.

    MathJax in the raw HTML appears as:
    - <mjx-container> elements (rendered output)
    - Raw $...$ or $$...$$ in text nodes
    """
    # Handle <mjx-container> elements (rendered MathJax)
    for mjx in content.find_all("mjx-container"):
        # Try to get LaTeX source from aria-label or from nested <math> annotation
        tex = ""
        # Check aria-label first (MathJax puts LaTeX there)
        if mjx.get("aria-label"):
            tex = mjx["aria-label"]
        # Check nested annotation
        if not tex:
            annotation = mjx.find("annotation", encoding="application/x-tex")
            if annotation:
                tex = annotation.get_text()
        if not tex:
            tex = mjx.get_text(strip=True)

        if not tex:
            continue

        is_display = mjx.get("display") == "true" or "display" in mjx.get("class", [])
        idx = len(math_registry)

        if is_display:
            mjx.replace_with(f"__MATHBLOCK_{idx}__")
            math_registry[str(idx)] = {"tex": tex, "display": True}
        else:
            mjx.replace_with(f"{{{{MATH:{idx}}}}}")
            math_registry[str(idx)] = {"tex": tex, "display": False}

    # Handle raw $...$ in text (if MathJax hasn't rendered yet)
    for text_node in content.find_all(string=True):
        parent = text_node.parent
        if parent and parent.name in ("script", "style", "pre", "code"):
            continue
        text = str(text_node)
        if "$$" in text or ("$" in text and re.search(r'\$[^$]+\$', text)):
            new_text = text
            # Display math: $$...$$
            for m in re.finditer(r'\$\$(.+?)\$\$', new_text, re.DOTALL):
                idx = len(math_registry)
                math_registry[str(idx)] = {"tex": m.group(1), "display": True}
                new_text = new_text.replace(m.group(0), f"__MATHBLOCK_{idx}__", 1)
            # Inline math: $...$
            for m in re.finditer(r'(?<!\$)\$([^$\n]+?)\$(?!\$)', new_text):
                idx = len(math_registry)
                math_registry[str(idx)] = {"tex": m.group(1), "display": False}
                new_text = new_text.replace(m.group(0), f"{{{{MATH:{idx}}}}}", 1)
            if new_text != text:
                text_node.replace_with(new_text)


def _walk_block(el, segments: list, all_slugs: set[str],
                internal_links: list, page_url: str, slug: str,
                img_dir: Path, math_registry: dict,
                _emitted: set | None = None):
    """Recursively walk content, emitting segments."""
    if _emitted is None:
        _emitted = set()

    for child in el.children:
        if not isinstance(child, Tag):
            if hasattr(child, 'string') and child.string:
                text = str(child.string).strip()
                if text and len(text) >= 2:
                    _emit_text(text, "paragraph", segments, math_registry)
            continue

        tag = child.name

        if tag in ("h1", "h2", "h3", "h4"):
            text = child.get_text(strip=True)
            if text:
                segments.append({
                    "index": len(segments), "type": "heading",
                    "heading_level": int(tag[1]),
                    "text": text, "footnote_refs": [], "links": [],
                })

        elif tag == "pre":
            if id(child) not in _emitted:
                _emitted.add(id(child))
                code_text = get_code_text(child)
                if not is_empty_pre(code_text):
                    segments.append({
                        "index": len(segments), "type": "code",
                        "text": code_text, "footnote_refs": [], "links": [],
                    })

        elif tag == "figure":
            img = child.find("img")
            if img and img.get("src"):
                src = img["src"]
                local_src = _resolve_image(src, page_url, slug, img_dir)
                alt = img.get("alt", "")
                cap_el = child.find("figcaption")
                cap = cap_el.get_text(strip=True) if cap_el else ""
                segments.append({
                    "index": len(segments), "type": "figure",
                    "text": cap or alt, "image_src": local_src,
                    "alt_text": alt, "caption": cap,
                    "footnote_refs": [], "links": [],
                })
                _emitted.add(id(img))

        elif tag == "img" and child.get("src"):
            if id(child) not in _emitted:
                _emitted.add(id(child))
                src = child["src"]
                if not is_tracking_pixel(src):
                    local_src = _resolve_image(src, page_url, slug, img_dir)
                    segments.append({
                        "index": len(segments), "type": "figure",
                        "text": child.get("alt", ""), "image_src": local_src,
                        "alt_text": child.get("alt", ""), "caption": "",
                        "footnote_refs": [], "links": [],
                    })

        elif tag == "blockquote":
            text = _extract_text(child, all_slugs, internal_links, page_url)
            if text.strip():
                _emit_text(text.strip(), "blockquote", segments, math_registry)

        elif tag == "p":
            # Check for image-only paragraphs
            img = child.find("img")
            if img and img.get("src") and id(img) not in _emitted:
                _emitted.add(id(img))
                src = img["src"]
                if not is_tracking_pixel(src):
                    local_src = _resolve_image(src, page_url, slug, img_dir)
                    segments.append({
                        "index": len(segments), "type": "figure",
                        "text": img.get("alt", ""), "image_src": local_src,
                        "alt_text": img.get("alt", ""), "caption": "",
                        "footnote_refs": [], "links": [],
                    })

            text = _extract_text(child, all_slugs, internal_links, page_url)
            if text.strip() and len(text.strip()) >= 3:
                _emit_text(text.strip(), "paragraph", segments, math_registry)

        elif tag in ("ul", "ol"):
            text = _extract_text(child, all_slugs, internal_links, page_url)
            if text.strip():
                _emit_text(text.strip(), "list", segments, math_registry)

        elif tag == "table":
            segments.append({
                "index": len(segments), "type": "table",
                "text": child.get_text(strip=True),
                "raw_html": str(child),
                "footnote_refs": [], "links": [],
            })

        elif tag in ("header", "footer", "nav", "script", "style", "noscript"):
            continue

        elif tag in ("div", "section", "article", "main", "span",
                      "li", "dd", "dt", "td", "th", "details", "summary"):
            _walk_block(child, segments, all_slugs, internal_links,
                        page_url, slug, img_dir, math_registry, _emitted)

        elif list(child.children):
            _walk_block(child, segments, all_slugs, internal_links,
                        page_url, slug, img_dir, math_registry, _emitted)


def _emit_text(text: str, seg_type: str, segments: list, math_registry: dict):
    """Emit text segment, splitting out math blocks."""
    if "__MATHBLOCK_" in text:
        parts = re.split(r'(__MATHBLOCK_\d+__)', text)
        for part in parts:
            m = re.match(r'__MATHBLOCK_(\d+)__', part)
            if m:
                idx = m.group(1)
                entry = math_registry.get(idx, {})
                segments.append({
                    "index": len(segments), "type": "math_block",
                    "text": entry.get("tex", ""),
                    "footnote_refs": [], "links": [],
                })
            elif part.strip() and len(part.strip()) >= 3:
                fn_refs = re.findall(r'\{\{FNREF:(\d+)\}\}', part)
                segments.append({
                    "index": len(segments), "type": seg_type,
                    "text": part.strip(), "footnote_refs": fn_refs,
                    "links": _seg_links(part),
                })
    else:
        fn_refs = re.findall(r'\{\{FNREF:(\d+)\}\}', text)
        segments.append({
            "index": len(segments), "type": seg_type,
            "text": text, "footnote_refs": fn_refs,
            "links": _seg_links(text),
        })


def _resolve_image(src: str, page_url: str, slug: str, img_dir: Path) -> str:
    if src.startswith("data:"):
        return src
    abs_url = urljoin(page_url, src)
    filename = Path(urlparse(abs_url).path).name
    if not filename:
        filename = f"img_{hash(abs_url) % 100000}.png"
    local_dir = img_dir / slug
    _download_image(abs_url, local_dir / filename)
    return f"../images/{slug}/{filename}"


def _extract_text(el: Tag, all_slugs: set[str], internal_links: list,
                  page_url: str) -> str:
    el = copy(el)
    for a in el.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        full = urljoin(page_url, href)
        parsed_u = urlparse(full)
        link_text = a.get_text(strip=True)
        if not link_text:
            continue
        if parsed_u.hostname and "lilianweng.github.io" in parsed_u.hostname:
            path = parsed_u.path.strip("/")
            link_slug = path.replace("/", "-").rstrip("-")
            if link_slug in all_slugs:
                a.replace_with(f"{{{{LINK:{link_slug}:{link_text}}}}}")
                internal_links.append({"text": link_text, "target_slug": link_slug})
        elif full.startswith("http"):
            a.replace_with(f"{{{{EXTLINK|{full}|{link_text}}}}}")
    return el.get_text()


def _seg_links(text: str) -> list[dict]:
    return [{"target_slug": s, "text": t}
            for s, t in re.findall(r'\{\{LINK:([^:}]+):([^}]*)\}\}', text)]
