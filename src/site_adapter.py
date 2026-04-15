"""Site adapter protocol for the universal translation framework."""
from __future__ import annotations
from typing import Protocol, runtime_checkable
from pathlib import Path


@runtime_checkable
class SiteAdapter(Protocol):
    """Protocol that every site adapter must satisfy."""

    site_name: str          # e.g. "pg", "lethain"
    display_name: str       # e.g. "Paul Graham 文集"
    author: str             # e.g. "Paul Graham"
    base_url: str           # e.g. "https://www.paulgraham.com"
    source_label: str       # e.g. "paulgraham.com"

    def build_index(self, raw_dir: Path, index_file: Path) -> list[dict]:
        """Discover all pages, save raw HTML, write index.json."""
        ...

    def scrape_all(self, raw_dir: Path, parsed_dir: Path, index_file: Path) -> dict:
        """Parse all indexed pages into structured JSON. Returns {success, failed}."""
        ...
