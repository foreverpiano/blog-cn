"""Shared extraction filter rules used by both site adapters and the validator.

These functions define the canonical behavior for code-text extraction,
empty-pre filtering, and tracking-pixel filtering. Both the parser and
validator MUST use these same functions to ensure consistency.
"""


def get_code_text(pre) -> str:
    """Extract code text from a <pre> element.
    Use only a direct child <code> (recursive=False); otherwise use pre's own text."""
    code_el = pre.find("code", recursive=False)
    return code_el.get_text() if code_el else pre.get_text()


def is_empty_pre(pre_text: str) -> bool:
    """A <pre> is skipped if its text content is empty or whitespace-only."""
    return not pre_text.strip()


def is_tracking_pixel(img_src: str) -> bool:
    """An <img> is skipped if it's a tracking pixel / affiliate beacon."""
    lower = img_src.lower()
    return any(x in lower for x in [
        "assoc-amazon.com", "amazon-adsystem.com", "doubleclick.net",
        "1x1", "pixel", "beacon", "spacer", "trans_1x1",
    ])
