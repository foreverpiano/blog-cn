#!/usr/bin/env python3
"""Universal English-to-Chinese Translation Framework.

Usage:
    python main.py --site lethain              # Full pipeline for lethain.com
    python main.py --site pg index             # Only build PG index
    python main.py --site lethain translate     # Only translate lethain
    python main.py portal                       # Generate multi-site portal page
    python main.py                              # Default: full pipeline for pg
"""
import argparse
import sys


def run_index(adapter, paths):
    return adapter.build_index(paths.RAW_DIR, paths.INDEX_FILE)


def run_scrape(adapter, paths):
    return adapter.scrape_all(paths.RAW_DIR, paths.PARSED_DIR, paths.INDEX_FILE)


def run_translate(paths):
    from src.config import OPENROUTER_API_KEY
    if not OPENROUTER_API_KEY:
        print("Error: OPENROUTER_API_KEY not set.")
        print("Copy .env.example to .env and add your API key.")
        sys.exit(1)
    from src.translator import translate_all
    return translate_all(paths)


def run_build(adapter, paths):
    from src.generator import generate_site
    return generate_site(paths, adapter)


def run_validate(paths):
    from src.validator import validate_all, check_links, check_rendered_quality

    failures = []

    print("=== Translation Validation ===")
    v_result = validate_all(paths)
    if v_result.get("issues", 0) > 0:
        failures.append(f"translation: {v_result['issues']} issues")

    print("\n=== Link Check ===")
    l_result = check_links(paths)
    if l_result.get("broken"):
        failures.append(f"links: {len(l_result['broken'])} broken")

    print("\n=== Rendered Quality ===")
    r_result = check_rendered_quality(paths)
    if r_result.get("raw_placeholder_files"):
        failures.append(f"placeholders: {len(r_result['raw_placeholder_files'])} files")
    else:
        print("  PASS: 0 raw placeholder leaks")

    if failures:
        print(f"\n=== VALIDATION FAILED: {', '.join(failures)} ===")
        sys.exit(1)
    else:
        print(f"\n=== ALL GATES PASSED ===")


def run_portal():
    """Generate the multi-site portal index page."""
    from src.generator import generate_portal
    generate_portal()


def main():
    parser = argparse.ArgumentParser(description="Universal translation framework")
    parser.add_argument("--site", default="pg", help="Site adapter name (default: pg)")
    parser.add_argument("stage", nargs="?", default=None,
                        help="Pipeline stage: index, scrape, translate, build, validate, portal")
    args = parser.parse_args()

    # Portal is site-independent
    if args.stage == "portal":
        run_portal()
        return

    from src.config import load_site_adapter, get_site_paths, ensure_dirs
    adapter = load_site_adapter(args.site)
    paths = get_site_paths(args.site)
    ensure_dirs(paths)

    if args.stage is None:
        # Full pipeline
        print("=" * 50)
        print(f"{adapter.display_name} - 全流程构建")
        print("=" * 50)

        print("\n[1/4] Building index...")
        run_index(adapter, paths)

        print("\n[2/4] Scraping pages...")
        run_scrape(adapter, paths)

        print("\n[3/4] Translating content...")
        run_translate(paths)

        print("\n[4/4] Generating site...")
        run_build(adapter, paths)

        print("\n" + "=" * 50)
        print(f"Done! Open dist/{args.site}/index.html to view the site.")
        print("=" * 50)
    else:
        stages = {
            "index": lambda: run_index(adapter, paths),
            "scrape": lambda: run_scrape(adapter, paths),
            "translate": lambda: run_translate(paths),
            "build": lambda: run_build(adapter, paths),
            "validate": lambda: run_validate(paths),
        }
        if args.stage not in stages:
            print(f"Unknown stage: {args.stage}")
            print(f"Available: {', '.join(stages.keys())}, portal")
            sys.exit(1)
        stages[args.stage]()


if __name__ == "__main__":
    main()
