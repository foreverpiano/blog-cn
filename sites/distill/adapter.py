"""distill.pub adapter — Interactive ML research articles (Distill framework)."""
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

BASE_URL = "https://distill.pub"


class DistillAdapter:
    site_name = "distill"
    display_name = "Distill.pub"
    author = "Distill"
    base_url = BASE_URL
    source_label = "distill.pub"

    def build_index(self, raw_dir: Path, index_file: Path) -> list[dict]:
        print("Fetching RSS feed...")
        rss_xml = _fetch_page(f"{BASE_URL}/rss.xml")
        if not rss_xml:
            print("Error: could not fetch RSS feed")
            return []

        root = ElementTree.fromstring(rss_xml)
        entries = []
        seen_urls = set()

        for item in root.findall(".//item"):
            title_el = item.find("title")
            link_el = item.find("link")
            pub_date_el = item.find("pubDate")

            if link_el is None or not link_el.text:
                continue

            url = link_el.text.strip()
            if url in seen_urls:
                continue
            seen_urls.add(url)

            parsed_url = urlparse(url)
            if parsed_url.hostname and "distill.pub" not in parsed_url.hostname:
                continue

            title = title_el.text.strip() if title_el is not None and title_el.text else ""

            # Parse date
            date = ""
            if pub_date_el is not None and pub_date_el.text:
                import email.utils
                try:
                    dt = email.utils.parsedate_to_datetime(pub_date_el.text)
                    date = dt.strftime("%Y-%m-%d")
                except Exception:
                    # Handle non-standard distill dates like "20:0:0"
                    date_text = pub_date_el.text
                    try:
                        fixed = re.sub(r'(\d+):0:0', r'\1:00:00', date_text)
                        dt = email.utils.parsedate_to_datetime(fixed)
                        date = dt.strftime("%Y-%m-%d")
                    except Exception:
                        date = ""

            # Flatten slug
            path = parsed_url.path.strip("/")
            slug = path.replace("/", "-").strip("-")
            slug = re.sub(r'-+', '-', slug)
            if not slug:
                continue

            # Fetch and save raw HTML
            raw_path = raw_dir / f"{slug}.html"
            if not raw_path.exists():
                html = _fetch_page(url)
                if html is None:
                    continue
                raw_path.write_text(html, encoding="utf-8")
                time.sleep(0.5)

            entries.append({
                "url": url,
                "slug": slug,
                "title": title,
                "date": date,
                "content_type": "paper",
                "source_path": path,
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

    # Find content container (modern + legacy Distill)
    content = (soup.find("d-article") or soup.find("dt-article")
               or soup.find("article") or soup.find("main") or soup.find("body"))
    if not content:
        return _empty_parsed(index_entry)

    content_copy = copy(content)

    # Strip site chrome that may leak into body-level extraction
    for chrome_tag in content_copy.find_all(["distill-header", "distill-footer",
                                              "dt-header", "dt-footer",
                                              "nav", "header", "footer"]):
        chrome_tag.decompose()

    # ── Distill preprocessing (same pipeline as transformer-circuits) ──

    # 1. Citations
    citation_registry = {}
    for d_cite in content_copy.find_all("d-citation"):
        # distill.pub uses d-citation (not d-cite like transformer-circuits)
        key = d_cite.get("key", "").strip()
        if not key:
            key = d_cite.get_text(strip=True)
        if key:
            idx = len(citation_registry)
            citation_registry[str(idx)] = {"key": key}
            d_cite.replace_with(f"[{{{{CITE:{idx}}}}}]")
    # Also handle d-cite
    for d_cite in content_copy.find_all("d-cite"):
        key = d_cite.get("key", "").strip()
        if key:
            idx = len(citation_registry)
            citation_registry[str(idx)] = {"key": key}
            d_cite.replace_with(f"[{{{{CITE:{idx}}}}}]")

    # 2. Code elements
    inline_code_registry = {}
    for d_code in content_copy.find_all("d-code"):
        code_text = d_code.get_text()
        if not code_text.strip():
            continue
        is_block = d_code.has_attr("block") or len(code_text) > 50
        if is_block:
            d_code.replace_with(f"__CODEBLOCK_{len(inline_code_registry)}__")
            inline_code_registry[str(len(inline_code_registry))] = {"text": code_text, "block": True}
        else:
            idx = len(inline_code_registry)
            d_code.replace_with(f"{{{{CODE:{idx}}}}}")
            inline_code_registry[str(idx)] = {"text": code_text, "block": False}

    # 3. Math — KaTeX with source in <annotation>
    math_registry = {}
    # KaTeX rendered output
    for katex_el in content_copy.select(".katex"):
        annotation = katex_el.find("annotation", encoding="application/x-tex")
        if annotation:
            tex = annotation.get_text()
            if not tex.strip():
                continue
            is_display = katex_el.parent and "katex-display" in katex_el.parent.get("class", [])
            idx = len(math_registry)
            if is_display:
                katex_el.parent.replace_with(f"__MATHBLOCK_{idx}__")
                math_registry[str(idx)] = {"tex": tex, "display": True}
            else:
                katex_el.replace_with(f"{{{{MATH:{idx}}}}}")
                math_registry[str(idx)] = {"tex": tex, "display": False}
    # Also handle <d-math>
    for d_math in content_copy.find_all("d-math"):
        tex = d_math.get_text()
        if not tex.strip():
            continue
        is_block = d_math.has_attr("block")
        idx = len(math_registry)
        if is_block:
            d_math.replace_with(f"__MATHBLOCK_{idx}__")
            math_registry[str(idx)] = {"tex": tex, "display": True}
        else:
            d_math.replace_with(f"{{{{MATH:{idx}}}}}")
            math_registry[str(idx)] = {"tex": tex, "display": False}

    # 4. Footnotes
    footnotes = []
    for d_fn in content_copy.find_all("d-footnote"):
        fn_text = d_fn.get_text()
        if not fn_text.strip():
            d_fn.decompose()
            continue
        fn_idx = len(footnotes)
        fn_id = f"f{fn_idx + 1}n"
        footnotes.append({"id": fn_id, "text": fn_text.strip()})
        d_fn.replace_with(f"{{{{FNREF:{fn_idx + 1}}}}}")

    # 5. Bibliography (only if article uses citations)
    bibtex_segments = []
    if citation_registry:
        for bib_script in soup.find_all("script", type="text/bibliography"):
            text = bib_script.get_text().strip()
            if text:
                bibtex_segments.append(text)

    # 6. Handle interactive content — Observable/D3 placeholders
    for obs_div in content_copy.select("[id^='observablehq']"):
        obs_div.replace_with(f"[Interactive visualization — view original: {page_url}]")

    # ── Walk content ──
    segments = []
    internal_links = []

    _walk_block(content_copy, segments, all_slugs, internal_links,
                page_url, slug, img_dir, math_registry, inline_code_registry)

    # Add bibtex
    for bib_text in bibtex_segments:
        segments.append({
            "index": len(segments), "type": "bibtex",
            "text": bib_text, "footnote_refs": [], "links": [],
        })

    return {
        "url": page_url,
        "slug": slug,
        "title": title,
        "date": date,
        "content_type": "paper",
        "segments": segments,
        "footnotes": footnotes,
        "internal_links": internal_links,
        "paragraph_count": len(segments),
        "footnote_count": len(footnotes),
        "footnote_ref_count": sum(len(s.get("footnote_refs", [])) for s in segments),
        "math_registry": math_registry,
        "citation_registry": citation_registry,
        "inline_code_registry": inline_code_registry,
    }


def _empty_parsed(entry: dict) -> dict:
    return {
        "url": entry["url"], "slug": entry["slug"],
        "title": entry.get("title", ""), "date": entry.get("date", ""),
        "content_type": "paper",
        "segments": [], "footnotes": [], "internal_links": [],
        "paragraph_count": 0, "footnote_count": 0, "footnote_ref_count": 0,
        "math_registry": {}, "citation_registry": {}, "inline_code_registry": {},
    }


def _walk_block(el, segments: list, all_slugs: set[str],
                internal_links: list, page_url: str, slug: str,
                img_dir: Path, math_registry: dict,
                inline_code_registry: dict | None = None,
                _emitted: set | None = None):
    """Recursively walk content, emitting segments in document order."""
    if _emitted is None:
        _emitted = set()
    if inline_code_registry is None:
        inline_code_registry = {}

    for child in el.children:
        if not isinstance(child, Tag):
            from bs4 import Comment, Doctype, ProcessingInstruction
            if isinstance(child, (Comment, Doctype, ProcessingInstruction)):
                continue
            if hasattr(child, 'string') and child.string:
                text = str(child.string).strip()
                if text and len(text) >= 2 and not text.startswith("<"):
                    _emit_text(text, "paragraph", segments, math_registry, inline_code_registry)
            continue

        tag = child.name

        if tag in ("h2", "h3", "h4"):
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

        elif tag in ("figure", "d-figure", "dt-figure"):
            img = child.find("img")
            if img and img.get("src"):
                src = img["src"]
                local_src = _resolve_image(src, page_url, slug, img_dir)
                alt = img.get("alt", "")
                cap_el = child.find("figcaption")
                cap = _extract_text(cap_el, all_slugs, internal_links, page_url) if cap_el else ""
                segments.append({
                    "index": len(segments), "type": "figure",
                    "text": cap.strip() or alt, "image_src": local_src,
                    "alt_text": alt, "caption": cap.strip(),
                    "footnote_refs": [], "links": [],
                })
                _emitted.add(id(img))
            else:
                # Interactive figure without static image — emit placeholder
                cap_el = child.find("figcaption")
                cap = cap_el.get_text(strip=True) if cap_el else ""
                placeholder = f"[Interactive figure{': ' + cap if cap else ''} — view original: {page_url}]"
                segments.append({
                    "index": len(segments), "type": "paragraph",
                    "text": placeholder, "footnote_refs": [], "links": [],
                })

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
                _emit_text(text.strip(), "blockquote", segments, math_registry, inline_code_registry)

        elif tag == "p":
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
                _emit_text(text.strip(), "paragraph", segments, math_registry, inline_code_registry)

        elif tag in ("ul", "ol"):
            text = _extract_text(child, all_slugs, internal_links, page_url)
            if text.strip():
                _emit_text(text.strip(), "list", segments, math_registry, inline_code_registry)

        elif tag == "table":
            segments.append({
                "index": len(segments), "type": "table",
                "text": child.get_text(strip=True),
                "raw_html": str(child),
                "footnote_refs": [], "links": [],
            })

        # Skip non-content elements
        elif tag in ("d-contents", "d-toc", "d-byline", "d-title",
                      "d-abstract", "d-appendix", "d-footnote-list",
                      "d-citation-list", "d-bibliography", "distill-header",
                      "distill-footer", "script", "style", "noscript", "nav",
                      "header", "footer"):
            continue

        # Container elements — recurse
        elif tag in ("div", "section", "article", "main", "span", "d-article",
                      "li", "dd", "dt", "td", "th", "details", "summary"):
            _walk_block(child, segments, all_slugs, internal_links,
                        page_url, slug, img_dir, math_registry,
                        inline_code_registry, _emitted)

        elif list(child.children):
            _walk_block(child, segments, all_slugs, internal_links,
                        page_url, slug, img_dir, math_registry,
                        inline_code_registry, _emitted)


def _emit_text(text: str, seg_type: str, segments: list,
               math_registry: dict, inline_code_registry: dict):
    """Emit text segment, splitting out block markers."""
    if "__MATHBLOCK_" in text or "__CODEBLOCK_" in text:
        parts = re.split(r'(__MATHBLOCK_\d+__|__CODEBLOCK_\d+__)', text)
        for part in parts:
            m_math = re.match(r'__MATHBLOCK_(\d+)__', part)
            m_code = re.match(r'__CODEBLOCK_(\d+)__', part)
            if m_math:
                idx = m_math.group(1)
                entry = math_registry.get(idx, {})
                segments.append({
                    "index": len(segments), "type": "math_block",
                    "text": entry.get("tex", ""),
                    "footnote_refs": [], "links": [],
                })
            elif m_code:
                idx = m_code.group(1)
                entry = inline_code_registry.get(idx, {})
                segments.append({
                    "index": len(segments), "type": "code",
                    "text": entry.get("text", ""),
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
    # Ensure page URL has trailing slash for correct relative resolution
    base = page_url if page_url.endswith("/") else page_url + "/"
    abs_url = urljoin(base, src)
    filename = Path(urlparse(abs_url).path).name
    if not filename:
        filename = f"img_{hash(abs_url) % 100000}.png"
    # Deduplicate: add hash suffix if same filename from different paths
    local_dir = img_dir / slug
    local_path = local_dir / filename
    if local_path.exists() and local_path.stat().st_size > 0:
        return f"../images/{slug}/{filename}"
    success = _download_image(abs_url, local_path)
    if success and local_path.exists() and local_path.stat().st_size > 0:
        return f"../images/{slug}/{filename}"
    # Download failed — use local path (validator will catch missing file)
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
        if parsed_u.hostname and "distill.pub" in parsed_u.hostname:
            path = parsed_u.path.strip("/")
            link_slug = path.replace("/", "-").strip("-")
            link_slug = re.sub(r'-+', '-', link_slug)
            if link_slug in all_slugs:
                a.replace_with(f"{{{{LINK:{link_slug}:{link_text}}}}}")
                internal_links.append({"text": link_text, "target_slug": link_slug})
        elif full.startswith("http"):
            a.replace_with(f"{{{{EXTLINK|{full}|{link_text}}}}}")
    return el.get_text()


def _seg_links(text: str) -> list[dict]:
    return [{"target_slug": s, "text": t}
            for s, t in re.findall(r'\{\{LINK:([^:}]+):([^}]*)\}\}', text)]
