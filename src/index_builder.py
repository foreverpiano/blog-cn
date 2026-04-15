import json
import re
import time
from collections import deque
from urllib.parse import urljoin, urlparse
import httpx
from bs4 import BeautifulSoup
from src.config import BASE_URL, ARTICLES_URL, INDEX_URL, INDEX_FILE, RAW_DIR, DATA_DIR

SAME_DOMAIN_HOSTS = {"www.paulgraham.com", "paulgraham.com"}

# Pages that are navigation/meta, not content
EXCLUDE_SLUGS = {"articles", "index"}


def canonical_url(url: str) -> str | None:
    """Normalize a URL to canonical form, or return None if not same-domain .html."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host or host not in SAME_DOMAIN_HOSTS:
        return None
    path = parsed.path
    if not path.endswith(".html"):
        return None
    return f"https://www.paulgraham.com{path}"


def extract_links(html: str, base_url: str) -> set[str]:
    """Extract all same-domain .html links from a page."""
    soup = BeautifulSoup(html, "lxml")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        full = urljoin(base_url, href)
        canon = canonical_url(full)
        if canon:
            links.add(canon)
    return links


def parse_articles_list(html: str) -> dict[str, dict]:
    """Parse articles.html to get essay titles. Returns {canonical_url: {title, slug}}."""
    soup = BeautifulSoup(html, "lxml")
    entries = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        full = urljoin(ARTICLES_URL, href)
        canon = canonical_url(full)
        if not canon:
            continue

        title = a.get_text(strip=True)
        if not title:
            continue

        slug = canon.split("/")[-1].replace(".html", "")
        if slug not in EXCLUDE_SLUGS:
            entries[canon] = {"title": title, "slug": slug}

    return entries


def fetch_page(url: str, retries: int = 3) -> str | None:
    """Fetch a page with retry logic."""
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
                time.sleep(1)
    return None


def extract_date_from_html(html: str) -> str:
    """Extract publication date from article page content."""
    match = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}',
        html
    )
    return match.group(0) if match else ""


def build_index() -> list[dict]:
    """Build complete site index using BFS discovery from seed pages."""
    print("Phase 1: Fetching seed pages...")

    # Fetch and save seed pages
    articles_html = fetch_page(ARTICLES_URL)
    if not articles_html:
        print("Error: could not fetch articles.html")
        return []
    (RAW_DIR / "articles.html").write_text(articles_html, encoding="utf-8")

    index_html = fetch_page(INDEX_URL)
    if not index_html:
        print("Error: could not fetch index.html")
        return []
    (RAW_DIR / "index.html").write_text(index_html, encoding="utf-8")

    # Get essay titles from articles.html (authoritative source)
    essay_map = parse_articles_list(articles_html)
    essay_urls = set(essay_map.keys())
    print(f"  Found {len(essay_urls)} essays from articles.html")

    # Phase 2: BFS discovery of all reachable same-domain .html pages
    print("Phase 2: BFS discovery of all reachable pages...")
    visited = set()
    to_visit = deque()

    # Seed with links from articles.html and index.html
    seed_links = extract_links(articles_html, ARTICLES_URL) | extract_links(index_html, INDEX_URL)

    # Explicitly add known non-essay pages that may not be linked from seeds
    KNOWN_PAGES = [
        "faq", "bio", "books", "raq", "ind", "lisp", "info",
        "filters", "blog-post_3",  # Known 404s - included for exclusion evidence
    ]
    for slug in KNOWN_PAGES:
        seed_links.add(f"https://www.paulgraham.com/{slug}.html")

    for link in seed_links:
        slug = link.split("/")[-1].replace(".html", "")
        if slug not in EXCLUDE_SLUGS:
            to_visit.append(link)

    discovered = {}  # canonical_url -> {slug, title, date, raw_html}
    not_found = []   # URLs that returned 404

    while to_visit:
        url = to_visit.popleft()
        if url in visited:
            continue
        visited.add(url)

        slug = url.split("/")[-1].replace(".html", "")
        if slug in EXCLUDE_SLUGS:
            continue

        # Fetch the page
        raw_path = RAW_DIR / f"{slug}.html"
        if raw_path.exists():
            html = raw_path.read_text(encoding="utf-8")
        else:
            html = fetch_page(url)
            if html is None:
                not_found.append(url)
                continue
            raw_path.write_text(html, encoding="utf-8")
            time.sleep(0.3)

        # Extract date from page content
        date = extract_date_from_html(html)

        # Get title: articles.html is authoritative, fallback to page
        if url in essay_map:
            title = essay_map[url]["title"]
        else:
            soup = BeautifulSoup(html, "lxml")
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else slug

        # Determine content type
        if url in essay_urls:
            content_type = "essay"
        else:
            content_type = "other"

        discovered[url] = {
            "url": url,
            "slug": slug,
            "title": title,
            "date": date,
            "content_type": content_type,
        }

        # Discover new links from this page
        new_links = extract_links(html, url)
        for link in new_links:
            if link not in visited:
                link_slug = link.split("/")[-1].replace(".html", "")
                if link_slug not in EXCLUDE_SLUGS:
                    to_visit.append(link)

        if len(discovered) % 50 == 0:
            print(f"  Discovered {len(discovered)} pages so far...")

    # Build final index: essays first (in articles.html order), then other pages
    entries = []
    added_urls = set()

    # Essays in order from articles.html
    for url in essay_map:
        if url in discovered:
            entries.append(discovered[url])
            added_urls.add(url)

    # Other pages (alphabetical by slug)
    other = [(d["slug"], url, d) for url, d in discovered.items() if url not in added_urls]
    other.sort()
    for _, url, d in other:
        entries.append(d)

    print(f"  Total indexed pages: {len(entries)} ({sum(1 for e in entries if e['content_type']=='essay')} essays, {sum(1 for e in entries if e['content_type']!='essay')} other)")
    print(f"  Pages with dates: {sum(1 for e in entries if e['date'])}")
    print(f"  URLs returning 404: {len(not_found)}")

    # Save index
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    # Save 404/exclusion log
    exclusion_log = DATA_DIR / "exclusion_log.json"
    with open(exclusion_log, "w", encoding="utf-8") as f:
        json.dump({
            "not_found_404": sorted(not_found),
            "excluded_meta_pages": [f"https://www.paulgraham.com/{s}.html" for s in EXCLUDE_SLUGS],
            "total_visited": len(visited),
            "total_indexed": len(entries),
        }, f, ensure_ascii=False, indent=2)
    print(f"Exclusion log saved to {exclusion_log}")

    print(f"Index saved to {INDEX_FILE}")
    return entries


if __name__ == "__main__":
    build_index()
