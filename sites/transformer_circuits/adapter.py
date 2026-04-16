"""transformer-circuits.pub adapter — Distill framework articles with math and images."""
import json
import re
import time
from copy import copy
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from src.extraction_rules import get_code_text, is_empty_pre, is_tracking_pixel

BASE_URL = "https://transformer-circuits.pub"


class TransformerCircuitsAdapter:
    site_name = "transformer_circuits"
    display_name = "Transformer Circuits Thread"
    author = "Anthropic"
    base_url = BASE_URL
    source_label = "transformer-circuits.pub"

    def build_index(self, raw_dir: Path, index_file: Path) -> list[dict]:
        print("Fetching homepage...")
        html = _fetch_page(BASE_URL)
        if not html:
            print("Error: could not fetch homepage")
            return []
        (raw_dir / "_homepage.html").write_text(html, encoding="utf-8")

        soup = BeautifulSoup(html, "lxml")
        entries = []
        skipped_external = []

        for container in soup.select(".paper, .note"):
            # The container itself may be the <a> tag
            if container.name == "a" and container.get("href"):
                href = container["href"]
            else:
                links = container.select("a[href]")
                if not links:
                    continue
                href = links[0].get("href", "")
            if not href:
                continue

            # Resolve URL
            full_url = urljoin(BASE_URL + "/", href)
            parsed_url = urlparse(full_url)

            # Skip external links
            if parsed_url.hostname and "transformer-circuits.pub" not in parsed_url.hostname:
                skipped_external.append(full_url)
                continue

            # Extract title
            h3 = container.find("h3")
            title = h3.get_text(strip=True) if h3 else ""
            if not title:
                title = container.get_text(strip=True)

            # Flatten slug from path
            path = parsed_url.path.strip("/")
            slug = _flatten_slug(path)
            if not slug:
                continue

            # Extract date from nearest .date element
            date = ""
            date_el = container.find_previous_sibling(class_="date")
            if date_el:
                date = date_el.get_text(strip=True)

            # Content type from container class
            content_type = "paper" if "paper" in container.get("class", []) else "note"

            # Fetch and save raw HTML
            raw_path = raw_dir / f"{slug}.html"
            if not raw_path.exists():
                page_html = _fetch_page(full_url)
                if page_html is None:
                    print(f"  Failed to fetch {full_url}")
                    continue
                raw_path.write_text(page_html, encoding="utf-8")
                time.sleep(0.5)

            entries.append({
                "url": full_url,
                "slug": slug,
                "title": title,
                "date": date,
                "content_type": content_type,
                "source_path": path,
            })
            if len(entries) % 10 == 0:
                print(f"  Indexed {len(entries)} articles...")

        print(f"Index: {len(entries)} articles, {len(skipped_external)} external links skipped")

        index_file.parent.mkdir(parents=True, exist_ok=True)
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)

        # Save skipped external log
        log_path = index_file.parent / "external_links.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(sorted(set(skipped_external)), f, ensure_ascii=False, indent=2)

        print(f"Index saved to {index_file}")
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


def _flatten_slug(path: str) -> str:
    """Flatten a nested URL path to a single-level slug."""
    slug = path.replace("/", "-").replace(".html", "").replace("index", "").strip("-")
    slug = re.sub(r'-+', '-', slug)  # Collapse multiple hyphens
    return slug


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

    # Find content container with fallback chain
    content = (soup.find("d-article") or soup.find("article")
               or soup.find("main") or soup.find("body"))
    if not content:
        return _empty_parsed(index_entry)

    # Clone content to avoid mutating original
    content_copy = copy(content)

    # ── Distill preprocessing pipeline (fixed order) ────────────

    # 1. Replace d-cite with citation placeholders
    citation_registry = {}
    for d_cite in content_copy.find_all("d-cite"):
        key = d_cite.get("key", "").strip()
        if key:
            idx = len(citation_registry)
            citation_registry[str(idx)] = {"key": key}
            d_cite.replace_with(f"[{{{{CITE:{idx}}}}}]")
        else:
            d_cite.decompose()

    # 2. Replace d-code with code placeholders (inline) or markers (block)
    inline_code_registry = {}
    for d_code in content_copy.find_all("d-code"):
        code_text = d_code.get_text()
        if not code_text.strip():
            continue
        # Heuristic: if d-code has block attribute or is a direct child of d-article, treat as block
        is_block = d_code.has_attr("block") or (d_code.parent and d_code.parent.name in ("d-article", "div", "section"))
        if is_block and len(code_text) > 50:
            d_code.replace_with(f"__CODEBLOCK_{len(inline_code_registry)}__")
            inline_code_registry[str(len(inline_code_registry))] = {"text": code_text, "block": True}
        else:
            idx = len(inline_code_registry)
            d_code.replace_with(f"{{{{CODE:{idx}}}}}")
            inline_code_registry[str(idx)] = {"text": code_text, "block": False}

    # 3. Replace d-math with placeholders
    math_registry = {}
    for d_math in content_copy.find_all("d-math"):
        tex = d_math.get_text()
        if not tex.strip():
            continue
        is_block = d_math.has_attr("block")
        if is_block:
            d_math.replace_with(f"__MATHBLOCK_{len(math_registry)}__")
            math_registry[str(len(math_registry))] = {"tex": tex, "display": True}
        else:
            idx = len(math_registry)
            d_math.replace_with(f"{{{{MATH:{idx}}}}}")
            math_registry[str(idx)] = {"tex": tex, "display": False}

    # 4. Extract d-footnote elements → footnotes array + FNREF placeholders
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

    # 5. Extract bibliography from d-bibliography src or script type=text/bibliography
    bibtex_segments = []
    seen_bib_srcs = set()
    for d_bib in soup.find_all("d-bibliography"):
        bib_src = d_bib.get("src", "")
        if bib_src and bib_src not in seen_bib_srcs:
            seen_bib_srcs.add(bib_src)
            bib_url = urljoin(page_url, bib_src)
            bib_content = _fetch_page(bib_url)
            if bib_content and not bib_content.strip().startswith("<?xml"):
                bibtex_segments.append(bib_content.strip())
            else:
                # Record bibliography source reference even if fetch failed
                bibtex_segments.append(f"% Bibliography: {bib_url} (source reference)")
    # Also check: script type=text/bibliography (inline)
    for bib_script in soup.find_all("script", type="text/bibliography"):
        text = bib_script.get_text().strip()
        if text:
            bibtex_segments.append(text)

    # ── Walk content tree ───────────────────────────────────────
    segments = []
    internal_links = []

    _walk_block(content_copy, segments, all_slugs, internal_links,
                page_url, slug, img_dir, math_registry, inline_code_registry)

    # Add abstract as first paragraph if present
    abstract_el = soup.find("d-abstract")
    abstract = abstract_el.get_text(strip=True) if abstract_el else ""
    if abstract and (not segments or segments[0].get("type") != "paragraph"):
        segments.insert(0, {
            "index": 0, "type": "paragraph", "text": abstract,
            "footnote_refs": [], "links": [],
        })
        for i, s in enumerate(segments):
            s["index"] = i

    # Add BibTeX segments
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
        "content_type": index_entry.get("content_type", "paper"),
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
        "content_type": entry.get("content_type", "paper"),
        "segments": [], "footnotes": [], "internal_links": [],
        "paragraph_count": 0, "footnote_count": 0, "footnote_ref_count": 0,
        "math_registry": {},
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
            # Emit ALL non-Tag text (including inline math placeholders)
            if hasattr(child, 'string') and child.string:
                text = str(child.string).strip()
                if text and len(text) >= 2:
                    _emit_text_segment(text, "paragraph", segments,
                                       math_registry, inline_code_registry)
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

        elif tag == "figure":
            img = child.find("img")
            if img and img.get("src"):
                src = img["src"]
                local_src = _resolve_and_download_image(src, page_url, slug, img_dir)
                alt_text = img.get("alt", "")
                caption_el = child.find("figcaption")
                caption = _extract_text(caption_el, all_slugs, internal_links, page_url) if caption_el else ""
                segments.append({
                    "index": len(segments), "type": "figure",
                    "text": caption.strip() or alt_text,
                    "image_src": local_src, "alt_text": alt_text, "caption": caption.strip(),
                    "footnote_refs": [], "links": [],
                })
                _emitted.add(id(img))

        elif tag == "img" and child.get("src"):
            if id(child) not in _emitted:
                _emitted.add(id(child))
                src = child["src"]
                local_src = _resolve_and_download_image(src, page_url, slug, img_dir)
                alt_text = child.get("alt", "")
                segments.append({
                    "index": len(segments), "type": "figure",
                    "text": alt_text,
                    "image_src": local_src, "alt_text": alt_text, "caption": "",
                    "footnote_refs": [], "links": [],
                })

        elif tag == "blockquote":
            text = _extract_text(child, all_slugs, internal_links, page_url)
            if text.strip():
                _emit_text_segment(text.strip(), "blockquote", segments, math_registry, inline_code_registry)

        elif tag == "p":
            img = child.find("img")
            if img and img.get("src") and id(img) not in _emitted:
                _emitted.add(id(img))
                src = img["src"]
                local_src = _resolve_and_download_image(src, page_url, slug, img_dir)
                alt_text = img.get("alt", "")
                segments.append({
                    "index": len(segments), "type": "figure",
                    "text": alt_text,
                    "image_src": local_src, "alt_text": alt_text, "caption": "",
                    "footnote_refs": [], "links": [],
                })

            text = _extract_text(child, all_slugs, internal_links, page_url)
            if text.strip() and len(text.strip()) >= 3:
                _emit_text_segment(text.strip(), "paragraph", segments, math_registry, inline_code_registry)

        elif tag in ("ul", "ol"):
            text = _extract_text(child, all_slugs, internal_links, page_url)
            if text.strip():
                _emit_text_segment(text.strip(), "list", segments, math_registry, inline_code_registry)

        elif tag == "table":
            table_html = str(child)
            segments.append({
                "index": len(segments), "type": "table",
                "text": child.get_text(strip=True),
                "raw_html": table_html,
                "footnote_refs": [], "links": [],
            })

        # Skip known non-content elements (d-footnote already extracted in preprocessing)
        elif tag in ("d-contents", "d-toc", "d-byline", "d-title",
                      "d-abstract", "d-appendix", "d-footnote-list",
                      "d-citation-list", "d-bibliography", "distill-header",
                      "distill-footer", "script", "style", "noscript", "nav"):
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


def _emit_text_with_markers(text: str, segments: list, math_registry: dict,
                            inline_code_registry: dict):
    """Emit text that may contain MATHBLOCK or CODEBLOCK markers as separate segments."""
    if not ("__MATHBLOCK_" in text or "__CODEBLOCK_" in text):
        return
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
            segments.append({
                "index": len(segments), "type": "paragraph",
                "text": part.strip(), "footnote_refs": [],
                "links": _seg_links(part),
            })


def _emit_text_segment(text: str, seg_type: str, segments: list,
                       math_registry: dict, inline_code_registry: dict):
    """Emit a text segment, splitting out any block markers first."""
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


def _resolve_and_download_image(src: str, page_url: str, slug: str,
                                img_dir: Path) -> str:
    """Resolve image URL, download if needed, return local path."""
    if src.startswith("data:"):
        return src  # Keep data URIs inline

    abs_url = urljoin(page_url, src)
    filename = Path(urlparse(abs_url).path).name
    if not filename:
        filename = f"img_{hash(abs_url) % 100000}.png"

    local_dir = img_dir / slug
    local_path = local_dir / filename
    _download_image(abs_url, local_path)

    # Return path relative to dist site root (articles are at articles/{slug}.html)
    return f"../images/{slug}/{filename}"


def _extract_text(el: Tag, all_slugs: set[str], internal_links: list,
                  page_url: str) -> str:
    """Extract text with internal link placeholders."""
    el = copy(el)

    for a in el.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue

        full = urljoin(page_url, href)
        parsed = urlparse(full)

        if parsed.hostname and "transformer-circuits.pub" in parsed.hostname:
            path = parsed.path.strip("/")
            link_slug = _flatten_slug(path)
            if link_slug in all_slugs:
                link_text = a.get_text(strip=True)
                if link_text:
                    a.replace_with(f"{{{{LINK:{link_slug}:{link_text}}}}}")
                    internal_links.append({"text": link_text, "target_slug": link_slug})

    return el.get_text()


def _seg_links(text: str) -> list[dict]:
    return [{"target_slug": s, "text": t}
            for s, t in re.findall(r'\{\{LINK:([^:}]+):([^}]*)\}\}', text)]
