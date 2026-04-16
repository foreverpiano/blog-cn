import json
import re
import shutil
from html import escape
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from src.config import PROJECT_ROOT


def load_translated_articles(paths) -> list[dict]:
    if not paths.INDEX_FILE.exists():
        return []
    with open(paths.INDEX_FILE, "r", encoding="utf-8") as f:
        index = json.load(f)
    articles = []
    for entry in index:
        path = paths.TRANSLATED_DIR / f"{entry['slug']}.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                articles.append(json.load(f))
    return articles


def build_slug_set(articles: list[dict]) -> set[str]:
    return {a["slug"] for a in articles}


def render_segment_html(text: str, footnote_ids: set[str], valid_slugs: set[str],
                        title_map: dict[str, str], math_registry: dict | None = None) -> str:
    rendered = escape(text)

    def replace_fnref(m):
        num = m.group(1)
        fn_id = f"f{num}n"
        if fn_id in footnote_ids:
            return f'<sup id="fnref-{fn_id}"><a href="#{fn_id}">[{num}]</a></sup>'
        return f'[{num}]'

    rendered = re.sub(r'\{\{FNREF:(\d+)\}\}', replace_fnref, rendered)

    def replace_link(m):
        slug = m.group(1)
        original_text = m.group(2) if m.group(2) else ""
        if slug not in valid_slugs:
            return original_text
        display = title_map.get(slug, original_text) or slug
        return f'<a href="{slug}.html">{escape(display)}</a>'

    rendered = re.sub(r'\{\{LINK:([^:}]+):([^}]*)\}\}', replace_link, rendered)

    cross_page = title_map.get("_cross_page_notes")
    if cross_page and cross_page.get("notes_page_slug") in valid_slugs:
        notes_slug = cross_page["notes_page_slug"]
        ref_nums = set(str(n) for n in cross_page.get("ref_numbers", []))
        def replace_bare_ref(m):
            num = m.group(1)
            if num in ref_nums:
                return f'<a href="{notes_slug}.html">[{num}]</a>'
            return m.group(0)
        rendered = re.sub(r'\[(\d+)\]', replace_bare_ref, rendered)

    # Render math placeholders
    if math_registry:
        def replace_math(m):
            idx = m.group(1)
            entry = math_registry.get(idx, {})
            tex = entry.get("tex", m.group(0))
            if entry.get("display"):
                return f'<span class="math-display" data-math="{escape(tex)}">{escape(tex)}</span>'
            return f'<span class="math-inline" data-math="{escape(tex)}">{escape(tex)}</span>'
        rendered = re.sub(r'\{\{MATH:(\d+)\}\}', replace_math, rendered)

    # Render citation placeholders
    citation_registry = title_map.get("_citation_registry", {})
    if citation_registry:
        def replace_cite(m):
            idx = m.group(1)
            entry = citation_registry.get(idx, {})
            key = entry.get("key", f"cite:{idx}")
            return f'<cite>[{escape(key)}]</cite>'
        rendered = re.sub(r'\{\{CITE:(\d+)\}\}', replace_cite, rendered)

    # Render inline code placeholders
    inline_code_registry = title_map.get("_inline_code_registry", {})
    if inline_code_registry:
        def replace_code(m):
            idx = m.group(1)
            entry = inline_code_registry.get(idx, {})
            code_text = entry.get("text", "")
            return f'<code>{escape(code_text)}</code>'
        rendered = re.sub(r'\{\{CODE:(\d+)\}\}', replace_code, rendered)

    # Render external link placeholders (pipe-delimited to avoid URL colon conflicts)
    def replace_extlink(m):
        url = m.group(1)
        text = m.group(2)
        return f'<a href="{escape(url)}" target="_blank" rel="noopener">{escape(text)}</a>'
    rendered = re.sub(r'\{\{EXTLINK\|([^|]+)\|([^}]+)\}\}', replace_extlink, rendered)

    # Cleanup remaining raw placeholders
    rendered = re.sub(r'\{\{LINK:[^}]*\}\}', '', rendered)
    rendered = re.sub(r'\{\{FNREF:\d+\}\}', '', rendered)
    rendered = re.sub(r'\{\{MATH:\d+\}\}', '', rendered)
    rendered = re.sub(r'\{\{CITE:\d+\}\}', '', rendered)
    rendered = re.sub(r'\{\{CODE:\d+\}\}', '', rendered)
    # EXTLINK: try to recover mangled placeholders (translator may change | to :)
    def _recover_extlink(m):
        content = m.group(1)
        # Try pipe-delimited first
        if "|" in content:
            parts = content.split("|", 1)
            return f'<a href="{escape(parts[0])}" target="_blank" rel="noopener">{escape(parts[1])}</a>'
        # Try colon-delimited (mangled by translator)
        if "://" in content:
            # URL contains ://, split after the URL
            url_match = re.match(r'(https?://[^:：]+)[：:](.+)', content)
            if url_match:
                return f'<a href="{escape(url_match.group(1))}" target="_blank" rel="noopener">{escape(url_match.group(2))}</a>'
        return content
    rendered = re.sub(r'\{\{EXTLINK[|:]([^}]+)\}\}', _recover_extlink, rendered)
    # Clean up truncated/malformed placeholders (translator may drop closing }})
    rendered = re.sub(r'\{\{EXTLINK[|:][^}]*$', '', rendered)
    rendered = re.sub(r'\{\{EXTLINK[|:][^}]*(?=<)', '', rendered)

    return rendered


def _replace_placeholders_in_html(html: str, math_registry: dict,
                                  citation_registry: dict,
                                  inline_code_registry: dict) -> str:
    """Replace placeholders in raw HTML without escaping the HTML structure."""
    if math_registry:
        def _repl_math(m):
            idx = m.group(1)
            entry = math_registry.get(idx, {})
            tex = entry.get("tex", m.group(0))
            cls = "math-display" if entry.get("display") else "math-inline"
            return f'<span class="{cls}" data-math="{escape(tex)}">{escape(tex)}</span>'
        html = re.sub(r'\{\{MATH:(\d+)\}\}', _repl_math, html)

    if citation_registry:
        def _repl_cite(m):
            idx = m.group(1)
            entry = citation_registry.get(idx, {})
            key = entry.get("key", f"cite:{idx}")
            return f'<cite>[{escape(key)}]</cite>'
        html = re.sub(r'\{\{CITE:(\d+)\}\}', _repl_cite, html)

    if inline_code_registry:
        def _repl_code(m):
            idx = m.group(1)
            entry = inline_code_registry.get(idx, {})
            return f'<code>{escape(entry.get("text", ""))}</code>'
        html = re.sub(r'\{\{CODE:(\d+)\}\}', _repl_code, html)

    # Cleanup any remaining
    html = re.sub(r'\{\{MATH:\d+\}\}', '', html)
    html = re.sub(r'\{\{CITE:\d+\}\}', '', html)
    html = re.sub(r'\{\{CODE:\d+\}\}', '', html)
    return html


def prepare_article(article: dict, valid_slugs: set[str], title_map: dict[str, str]) -> dict:
    footnote_ids = {fn["id"] for fn in article.get("footnotes", [])}
    title_map_with_slug = dict(title_map)
    title_map_with_slug["_cross_page_notes"] = article.get("cross_page_notes")
    math_registry = article.get("math_registry", {})
    title_map_with_slug["_citation_registry"] = article.get("citation_registry", {})
    title_map_with_slug["_inline_code_registry"] = article.get("inline_code_registry", {})

    for seg in article.get("segments", []):
        seg_type = seg.get("type", "")

        # Untranslatable segments: render as-is
        if seg_type == "code":
            seg["rendered_html"] = escape(seg.get("text_zh") or seg.get("text", ""))
            continue
        if seg_type == "bibtex":
            seg["rendered_html"] = escape(seg.get("text", ""))
            continue
        if seg_type == "math_block":
            tex = seg.get("text_zh") or seg.get("text", "")
            seg["rendered_html"] = f'<span class="math-display" data-math="{escape(tex)}">{escape(tex)}</span>'
            continue
        if seg_type == "table":
            table_html = seg.get("raw_html", escape(seg.get("text", "")))
            # Replace placeholders directly in raw HTML (no escape — HTML must stay intact)
            if any(ph in table_html for ph in ("{{MATH:", "{{CITE:", "{{CODE:")):
                table_html = _replace_placeholders_in_html(
                    table_html, math_registry,
                    title_map_with_slug.get("_citation_registry", {}),
                    title_map_with_slug.get("_inline_code_registry", {}))
            seg["rendered_html"] = table_html
            continue
        if seg_type == "figure":
            caption = seg.get("caption") or seg.get("text_zh") or seg.get("text", "")
            if caption and any(ph in caption for ph in ("{{MATH:", "{{CITE:", "{{CODE:", "{{LINK:", "{{EXTLINK", "{{FNREF:")):
                seg["rendered_html"] = render_segment_html(
                    caption, footnote_ids, valid_slugs, title_map_with_slug, math_registry)
            else:
                seg["rendered_html"] = escape(caption) if caption else ""
            continue

        text = seg.get("text_zh") or seg.get("text", "")
        seg["rendered_html"] = render_segment_html(
            text, footnote_ids, valid_slugs, title_map_with_slug, math_registry)

    rendered_fnref_ids = set()
    for seg in article.get("segments", []):
        html = seg.get("rendered_html", "")
        for m in re.findall(r'id="fnref-(f\d+n)"', html):
            rendered_fnref_ids.add(m)

    for fn in article.get("footnotes", []):
        fn["has_visible_ref"] = fn["id"] in rendered_fnref_ids
        # Render placeholders in footnote text (MATH, CITE, CODE, LINK, FNREF)
        fn_text = fn.get("text_zh") or fn.get("text", "")
        if fn_text and any(ph in fn_text for ph in ("{{MATH:", "{{CITE:", "{{CODE:", "{{LINK:", "{{FNREF:")):
            fn_text = render_segment_html(fn_text, footnote_ids, valid_slugs,
                                          title_map_with_slug, math_registry)
        fn["rendered_text"] = fn_text

    return article


def group_by_type(articles: list[dict]) -> dict[str, list[dict]]:
    groups = {}
    for article in articles:
        ct = article.get("content_type", "essay")
        groups.setdefault(ct, []).append(article)
    return groups


def generate_site(paths, adapter):
    print("Loading translated articles...")
    articles = load_translated_articles(paths)
    if not articles:
        print("No translated articles found. Run translator first.")
        return

    print(f"Generating site for {len(articles)} articles...")

    valid_slugs = build_slug_set(articles)
    title_map = {}
    for article in articles:
        slug = article.get("slug", "")
        zh_title = article.get("title_zh", "")
        if slug and zh_title:
            title_map[slug] = zh_title

    env = Environment(
        loader=FileSystemLoader(str(paths.TEMPLATES_DIR)),
        autoescape=False,
    )

    if paths.DIST_DIR.exists():
        shutil.rmtree(paths.DIST_DIR)
    paths.DIST_DIR.mkdir(parents=True)
    (paths.DIST_DIR / "articles").mkdir()

    for article in articles:
        prepare_article(article, valid_slugs, title_map)

    groups = group_by_type(articles)
    essays = groups.get("essay", []) or groups.get("post", [])

    # Template variables
    tpl_vars = {
        "site_name": adapter.display_name,
        "source_url": adapter.base_url,
        "source_label": adapter.source_label,
        "site_key": adapter.site_name,
    }

    index_template = env.get_template("index.html")
    index_html = index_template.render(
        articles=essays,
        other_pages=[a for ct, arts in groups.items() if ct not in ("essay", "post") for a in arts],
        total_count=len(articles),
        **tpl_vars,
    )
    (paths.DIST_DIR / "index.html").write_text(index_html, encoding="utf-8")

    article_template = env.get_template("article.html")
    for article in articles:
        html = article_template.render(article=article, **tpl_vars)
        page_path = paths.DIST_DIR / "articles" / f"{article['slug']}.html"
        page_path.write_text(html, encoding="utf-8")

    css_dir = paths.DIST_DIR / "static" / "css"
    css_dir.mkdir(parents=True, exist_ok=True)
    (css_dir / "style.css").write_text(generate_css(), encoding="utf-8")

    # Copy images if they exist (for sites with downloaded images)
    img_src = paths.RAW_DIR / "images"
    if img_src.exists():
        img_dst = paths.DIST_DIR / "images"
        if img_dst.exists():
            shutil.rmtree(img_dst)
        shutil.copytree(img_src, img_dst)
        img_count = sum(1 for _ in img_dst.rglob("*") if _.is_file())
        print(f"  - {img_count} images copied")

    total_cross_links = 0
    total_visible_fnrefs = 0
    for article in articles:
        for seg in article.get("segments", []):
            html = seg.get("rendered_html", "")
            total_cross_links += len(re.findall(r'href="[^#][^"]*\.html"', html))
            total_visible_fnrefs += len(re.findall(r'<sup id="fnref-', html))

    print(f"Site generated at {paths.DIST_DIR}/")
    print(f"  - 1 index page, {len(articles)} article pages")
    print(f"  - {total_cross_links} cross-links, {total_visible_fnrefs} footnote refs")


def generate_portal():
    """Generate the multi-site portal page at dist/index.html."""
    from src.config import get_site_paths

    # Discover all sites that have been built
    dist_root = PROJECT_ROOT / "dist"
    sites = []
    for site_dir in sorted(dist_root.iterdir()):
        if site_dir.is_dir() and (site_dir / "index.html").exists():
            site_name = site_dir.name
            try:
                adapter = __import__(f"sites.{site_name}", fromlist=["get_adapter"]).get_adapter()
                paths = get_site_paths(site_name)
                count = 0
                if paths.INDEX_FILE.exists():
                    with open(paths.INDEX_FILE) as f:
                        count = len(json.load(f))
                sites.append({
                    "name": adapter.display_name,
                    "key": site_name,
                    "url": f"{site_name}/index.html",
                    "source_url": adapter.base_url,
                    "source_label": adapter.source_label,
                    "count": count,
                })
            except Exception:
                sites.append({
                    "name": site_name,
                    "key": site_name,
                    "url": f"{site_name}/index.html",
                    "source_url": "",
                    "source_label": site_name,
                    "count": 0,
                })

    env = Environment(
        loader=FileSystemLoader(str(PROJECT_ROOT / "templates")),
        autoescape=False,
    )
    template = env.get_template("portal.html")
    html = template.render(sites=sites)
    dist_root.mkdir(parents=True, exist_ok=True)
    (dist_root / "index.html").write_text(html, encoding="utf-8")
    print(f"Portal generated at dist/index.html with {len(sites)} sites")


def generate_css() -> str:
    return """\
:root {
  --text-primary: #1a1a2e;
  --text-secondary: #4a4a6a;
  --bg-primary: #fafaf8;
  --bg-secondary: #ffffff;
  --accent: #2d5a7b;
  --accent-light: #e8f0f7;
  --border: #e5e5e0;
  --max-width: 720px;
  --font-size-body: 17px;
  --line-height: 1.8;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

html {
  font-size: 16px;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

body {
  font-family: -apple-system, "PingFang SC", "Hiragino Sans GB",
    "Microsoft YaHei", "Noto Sans SC", "Source Han Sans SC",
    "WenQuanYi Micro Hei", sans-serif;
  font-size: var(--font-size-body);
  line-height: var(--line-height);
  color: var(--text-primary);
  background: var(--bg-primary);
}

.container { max-width: var(--max-width); margin: 0 auto; padding: 0 24px; }

.site-header { padding: 40px 0 20px; border-bottom: 1px solid var(--border); margin-bottom: 40px; }
.site-header h1 { font-size: 1.5rem; font-weight: 600; letter-spacing: -0.02em; }
.site-header h1 a { color: inherit; text-decoration: none; }
.site-header .subtitle { color: var(--text-secondary); font-size: 0.9rem; margin-top: 4px; }

.nav-section { margin-bottom: 24px; }
.nav-section h2 { font-size: 0.85rem; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }

.article-list { list-style: none; }
.article-list li { padding: 12px 0; border-bottom: 1px solid var(--border); }
.article-list li:last-child { border-bottom: none; }
.article-list a { color: var(--text-primary); text-decoration: none; font-size: 1rem; line-height: 1.5; display: block; }
.article-list a:hover { color: var(--accent); }
.article-list .date { color: var(--text-secondary); font-size: 0.85rem; margin-top: 2px; }

.article-header { margin-bottom: 32px; padding-bottom: 20px; border-bottom: 1px solid var(--border); }
.article-header h1 { font-size: 1.8rem; font-weight: 700; line-height: 1.3; margin-bottom: 8px; }
.article-header .meta { color: var(--text-secondary); font-size: 0.9rem; }

.article-content p { margin-bottom: 1.2em; text-align: justify; }
.article-content h2 { font-size: 1.4rem; font-weight: 600; margin: 2em 0 0.8em; }
.article-content h3 { font-size: 1.2rem; font-weight: 600; margin: 1.5em 0 0.6em; }
.article-content a { color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent-light); }
.article-content a:hover { border-bottom-color: var(--accent); }
.article-content sup { font-size: 0.75em; line-height: 0; vertical-align: super; }
.article-content sup a { border-bottom: none; color: var(--accent); }
.article-content blockquote { margin: 1.5em 0; padding: 12px 20px; border-left: 3px solid var(--accent); background: var(--accent-light); color: var(--text-secondary); }
.article-content pre { margin: 1.5em 0; padding: 16px; background: #f5f5f0; border-radius: 4px; overflow-x: auto; font-size: 0.9rem; line-height: 1.5; }
.article-content code { font-family: "SF Mono", "Fira Code", Menlo, monospace; font-size: 0.9em; }
.article-content ul, .article-content ol { margin: 1em 0; padding-left: 2em; }
.article-content li { margin-bottom: 0.5em; }
.article-content figure { margin: 2em 0; text-align: center; }
.article-content figure img { max-width: 100%; height: auto; border-radius: 4px; }
.article-content figcaption { margin-top: 8px; font-size: 0.85rem; color: var(--text-secondary); }
.math-block { margin: 1.5em 0; text-align: center; overflow-x: auto; }
.math-inline { display: inline; }
.bibtex { font-size: 0.85rem; background: #f5f5f0; }
.table-container { overflow-x: auto; margin: 1.5em 0; }
.table-container table { border-collapse: collapse; width: 100%; }
.table-container th, .table-container td { border: 1px solid var(--border); padding: 8px 12px; text-align: left; }

.footnotes { margin-top: 48px; padding-top: 24px; border-top: 1px solid var(--border); font-size: 0.9rem; color: var(--text-secondary); }
.footnotes h2 { font-size: 1rem; margin-bottom: 16px; }
.footnote-item { margin-bottom: 8px; padding-left: 2em; text-indent: -2em; }
.footnote-item a { color: var(--accent); text-decoration: none; }

.back-link { display: inline-block; margin: 32px 0; color: var(--accent); text-decoration: none; font-size: 0.9rem; }
.back-link:hover { text-decoration: underline; }

.site-footer { margin-top: 60px; padding: 24px 0; border-top: 1px solid var(--border); color: var(--text-secondary); font-size: 0.8rem; text-align: center; }

.site-card { display: block; padding: 24px; margin-bottom: 16px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 8px; text-decoration: none; color: inherit; transition: border-color 0.2s; }
.site-card:hover { border-color: var(--accent); }
.site-card h3 { font-size: 1.2rem; margin-bottom: 4px; color: var(--text-primary); }
.site-card .meta { color: var(--text-secondary); font-size: 0.85rem; }

@media (max-width: 768px) {
  .container { padding: 0 16px; }
  .site-header { padding: 24px 0 16px; margin-bottom: 24px; }
  .article-header h1 { font-size: 1.4rem; }
  :root { --font-size-body: 16px; --line-height: 1.7; }
}

@media (max-width: 480px) {
  .article-header h1 { font-size: 1.2rem; }
}
"""


if __name__ == "__main__":
    from src.config import load_site_adapter, get_site_paths, ensure_dirs
    adapter = load_site_adapter("pg")
    paths = get_site_paths("pg")
    ensure_dirs(paths)
    generate_site(paths, adapter)
