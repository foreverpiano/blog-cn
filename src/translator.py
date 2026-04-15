import asyncio
import json
import re
from pathlib import Path
import httpx
from src.config import (
    OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_API_URL,
)
from src.cache import compute_cache_key, get_cached, save_cache


SYSTEM_PROMPT = """你是一位专业的中英文翻译专家。你的任务是将用户发送的英文文本翻译成自然流畅的中文。

规则：
- 直接输出翻译结果，不要添加"翻译："等前缀、解释或标注
- 使用自然流畅的中文表达，允许适度改写以提升可读性
- 保持原文的语气和风格特点
- 专有名词首次出现时保留英文
- 不要翻译 URL、代码片段、锚点 ID、脚注引用编号
- 保留所有 {{FNREF:N}} 和 {{LINK:slug:text}} 占位符，不要翻译或删除它们
- 输入格式：每段用 <<<PARA_N>>> 标记开头，你必须保留这些标记并在对应位置输出翻译
- 只输出中文翻译，不输出其他任何内容"""

CONCURRENCY = 256


async def translate_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    slug: str,
    parsed: dict,
    cache_dir: Path | None = None,
) -> dict | None:
    """Translate a single article via API with concurrency control."""
    segments = parsed.get("segments", [])
    footnotes = parsed.get("footnotes", [])

    parts = []
    if parsed.get("title"):
        parts.append(f"<<<TITLE>>> {parsed['title']}")
    for i, seg in enumerate(segments):
        # Skip code segments — don't translate
        if seg.get("type") == "code":
            continue
        parts.append(f"<<<PARA_{i}>>> {seg['text']}")
    for i, fn in enumerate(footnotes):
        parts.append(f"<<<NOTE_{i}>>> {fn['text']}")

    full_text = "\n\n".join(parts)

    cache_key = compute_cache_key(full_text, cache_dir=cache_dir)
    cached = get_cached(cache_key, cache_dir=cache_dir)
    if cached is not None:
        translated_text = cached
    else:
        payload = {
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": full_text},
            ],
        }
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }

        async with semaphore:
            for attempt in range(3):
                try:
                    resp = await client.post(
                        OPENROUTER_API_URL, json=payload, headers=headers,
                    )
                    if resp.status_code == 429:
                        await asyncio.sleep(2 ** (attempt + 2))
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    translated_text = data["choices"][0]["message"]["content"]
                    save_cache(cache_key, full_text, translated_text, cache_dir=cache_dir)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2 ** (attempt + 1))

    # Parse response
    translated = parsed.copy()
    translated["title_original"] = parsed.get("title", "")

    title_match = re.search(r'<<<TITLE>>>\s*(.+?)(?=<<<|$)', translated_text, re.DOTALL)
    translated["title_zh"] = title_match.group(1).strip() if title_match else parsed.get("title", "")

    translated_segments = []
    for i, seg in enumerate(segments):
        new_seg = seg.copy()
        new_seg["text_original"] = seg["text"]

        # Code segments: keep original text, no translation
        if seg.get("type") == "code":
            new_seg["text_zh"] = seg["text"]
            translated_segments.append(new_seg)
            continue

        pattern = rf'<<<PARA_{i}>>>\s*(.+?)(?=<<<|$)'
        match = re.search(pattern, translated_text, re.DOTALL)
        text_zh = match.group(1).strip() if match else seg["text"]

        # Post-translation repair: count-based placeholder restoration
        from collections import Counter
        src_fnrefs = re.findall(r'\{\{FNREF:\d+\}\}', seg["text"])
        tgt_fnrefs = re.findall(r'\{\{FNREF:\d+\}\}', text_zh)
        src_fn_counts = Counter(src_fnrefs)
        tgt_fn_counts = Counter(tgt_fnrefs)
        for ph, cnt in src_fn_counts.items():
            deficit = cnt - tgt_fn_counts.get(ph, 0)
            for _ in range(deficit):
                text_zh += ph
        for ph, cnt in tgt_fn_counts.items():
            surplus = cnt - src_fn_counts.get(ph, 0)
            for _ in range(surplus):
                text_zh = text_zh.replace(ph, '', 1)

        src_links = re.findall(r'\{\{LINK:[^}]+\}\}', seg["text"])
        tgt_links = re.findall(r'\{\{LINK:[^}]+\}\}', text_zh)
        src_lk_counts = Counter(src_links)
        tgt_lk_counts = Counter(tgt_links)
        for ph, cnt in src_lk_counts.items():
            deficit = cnt - tgt_lk_counts.get(ph, 0)
            for _ in range(deficit):
                text_zh += ph
        for ph, cnt in tgt_lk_counts.items():
            surplus = cnt - src_lk_counts.get(ph, 0)
            for _ in range(surplus):
                text_zh = text_zh.replace(ph, '', 1)

        new_seg["text_zh"] = text_zh
        translated_segments.append(new_seg)
    translated["segments"] = translated_segments

    translated_footnotes = []
    for i, fn in enumerate(footnotes):
        new_fn = fn.copy()
        new_fn["text_original"] = fn["text"]
        pattern = rf'<<<NOTE_{i}>>>\s*(.+?)(?=<<<|$)'
        match = re.search(pattern, translated_text, re.DOTALL)
        new_fn["text_zh"] = match.group(1).strip() if match else fn["text"]
        translated_footnotes.append(new_fn)
    translated["footnotes"] = translated_footnotes

    return translated


async def translate_all_async(paths=None) -> dict:
    """Translate all articles concurrently."""
    if paths is None:
        from src.config import PARSED_DIR, TRANSLATED_DIR, INDEX_FILE, CACHE_DIR
    else:
        PARSED_DIR = paths.PARSED_DIR
        TRANSLATED_DIR = paths.TRANSLATED_DIR
        INDEX_FILE = paths.INDEX_FILE
        CACHE_DIR = paths.CACHE_DIR

    if not INDEX_FILE.exists():
        print("Error: index.json not found.")
        return {"success": [], "failed": [], "cached": []}

    if not OPENROUTER_API_KEY:
        print("Error: OPENROUTER_API_KEY not set.")
        return {"success": [], "failed": [], "cached": []}

    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        index = json.load(f)

    to_translate = []
    cached = []
    for entry in index:
        slug = entry["slug"]
        parsed_path = PARSED_DIR / f"{slug}.json"
        translated_path = TRANSLATED_DIR / f"{slug}.json"
        if not parsed_path.exists():
            continue
        if translated_path.exists():
            cached.append(slug)
            continue
        parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
        to_translate.append((slug, parsed))

    print(f"Translating: {len(to_translate)} new, {len(cached)} cached, concurrency={CONCURRENCY}")

    if not to_translate:
        print("Nothing to translate.")
        return {"success": [], "failed": [], "cached": cached}

    semaphore = asyncio.Semaphore(CONCURRENCY)
    success = []
    failed = []
    done_count = 0
    total = len(to_translate)

    async with httpx.AsyncClient(timeout=300) as client:
        async def process(slug: str, parsed: dict):
            nonlocal done_count
            try:
                translated = await translate_one(client, semaphore, slug, parsed, cache_dir=CACHE_DIR)
                path = TRANSLATED_DIR / f"{slug}.json"
                path.write_text(json.dumps(translated, ensure_ascii=False, indent=2), encoding="utf-8")
                success.append(slug)
            except Exception as e:
                failed.append({"slug": slug, "error": str(e)})
            finally:
                done_count += 1
                if done_count % 20 == 0 or done_count == total:
                    print(f"  Progress: {done_count}/{total} ({len(success)} ok, {len(failed)} failed)")

        tasks = [process(slug, parsed) for slug, parsed in to_translate]
        await asyncio.gather(*tasks)

    print(f"\nTranslation complete: {len(success)} new, {len(cached)} cached, {len(failed)} failed")

    if failed:
        failed_path = TRANSLATED_DIR / "_failed.json"
        failed_path.write_text(json.dumps(failed, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"success": success, "failed": failed, "cached": cached}


def translate_all(paths=None) -> dict:
    """Synchronous wrapper for translate_all_async."""
    return asyncio.run(translate_all_async(paths))


if __name__ == "__main__":
    translate_all()
