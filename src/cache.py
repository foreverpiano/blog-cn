import hashlib
import json
from pathlib import Path
from src.config import OPENROUTER_MODEL, PROMPT_VERSION, STYLE_CONFIG, SEGMENT_SCHEMA_VERSION


def _get_cache_dir(cache_dir=None):
    if cache_dir is not None:
        return cache_dir
    from src.config import CACHE_DIR
    return CACHE_DIR


def compute_cache_key(
    source_text: str,
    model_id: str = OPENROUTER_MODEL,
    prompt_version: str = PROMPT_VERSION,
    style_config: str = STYLE_CONFIG,
    schema_version: str = SEGMENT_SCHEMA_VERSION,
    cache_dir=None,  # unused, kept for API compat
) -> str:
    """Compute composite cache key from all translation parameters."""
    composite = f"{source_text}|{model_id}|{prompt_version}|{style_config}|{schema_version}"
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()


def get_cached(cache_key: str, cache_dir=None) -> str | None:
    """Retrieve cached translation if it exists."""
    cache_file = _get_cache_dir(cache_dir) / f"{cache_key}.json"
    if cache_file.exists():
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        return data.get("translated_text")
    return None


def save_cache(cache_key: str, source_text: str, translated_text: str,
               metadata: dict | None = None, cache_dir=None):
    """Save translation to cache with metadata."""
    cache_file = _get_cache_dir(cache_dir) / f"{cache_key}.json"
    data = {
        "cache_key": cache_key,
        "source_text": source_text,
        "translated_text": translated_text,
        "model_id": OPENROUTER_MODEL,
        "prompt_version": PROMPT_VERSION,
        "style_config": STYLE_CONFIG,
        "schema_version": SEGMENT_SCHEMA_VERSION,
    }
    if metadata:
        data["metadata"] = metadata
    cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
