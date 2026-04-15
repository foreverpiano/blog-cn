import os
from pathlib import Path
from types import SimpleNamespace
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.parent

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "x-ai/grok-4.20-beta")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

PROMPT_VERSION = "v1"
STYLE_CONFIG = "natural_fluent_chinese"
SEGMENT_SCHEMA_VERSION = "v1"


def get_site_paths(site_name: str) -> SimpleNamespace:
    """Return all data/dist paths for a given site."""
    data_dir = PROJECT_ROOT / "data" / site_name
    dist_dir = PROJECT_ROOT / "dist" / site_name
    return SimpleNamespace(
        DATA_DIR=data_dir,
        RAW_DIR=data_dir / "raw",
        PARSED_DIR=data_dir / "parsed",
        TRANSLATED_DIR=data_dir / "translated",
        CACHE_DIR=data_dir / "cache",
        INDEX_FILE=data_dir / "index.json",
        DIST_DIR=dist_dir,
        TEMPLATES_DIR=PROJECT_ROOT / "templates",
    )


def load_site_adapter(site_name: str):
    """Dynamically import and return a site adapter instance."""
    import importlib
    module = importlib.import_module(f"sites.{site_name}")
    return module.get_adapter()


def ensure_dirs(paths: SimpleNamespace):
    """Create all data directories for a site."""
    for d in [paths.RAW_DIR, paths.PARSED_DIR, paths.TRANSLATED_DIR,
              paths.CACHE_DIR, paths.DIST_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# Backward compatibility: default paths point to data/pg/
_default = get_site_paths("pg")
DATA_DIR = _default.DATA_DIR
RAW_DIR = _default.RAW_DIR
PARSED_DIR = _default.PARSED_DIR
TRANSLATED_DIR = _default.TRANSLATED_DIR
CACHE_DIR = _default.CACHE_DIR
DIST_DIR = _default.DIST_DIR
TEMPLATES_DIR = _default.TEMPLATES_DIR
INDEX_FILE = _default.INDEX_FILE
BASE_URL = "https://www.paulgraham.com"
ARTICLES_URL = f"{BASE_URL}/articles.html"
INDEX_URL = f"{BASE_URL}/index.html"
