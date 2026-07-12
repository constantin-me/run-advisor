import re

import markdown

# Telegram's HTML parse mode supports only this small tag set — no headers,
# lists, or paragraphs. https://core.telegram.org/bots/api#html-style
_ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre", "blockquote"}

_HEADER_RE = re.compile(r"<h[1-6]>(.*?)</h[1-6]>", re.DOTALL)
_LI_RE = re.compile(r"<li>(.*?)</li>", re.DOTALL)
_ANY_TAG_RE = re.compile(r"</?([a-zA-Z0-9]+)[^>]*>")


def to_telegram_html(text: str) -> str:
    """Convert the coach's CommonMark output to Telegram's supported HTML
    subset. Headers/lists/paragraphs have no Telegram equivalent, so they're
    flattened to bold text / bullet lines / blank-line-separated text rather
    than dropped."""
    html = markdown.markdown(text, extensions=["fenced_code"])

    html = _HEADER_RE.sub(r"<b>\1</b>\n", html)
    html = _LI_RE.sub(r"• \1\n", html)

    html = html.replace("<ul>", "").replace("</ul>", "")
    html = html.replace("<ol>", "").replace("</ol>", "")
    html = html.replace("<strong>", "<b>").replace("</strong>", "</b>")
    html = html.replace("<em>", "<i>").replace("</em>", "</i>")
    html = re.sub(r"<hr\s*/?>", "\n———\n", html)
    html = html.replace("<p>", "").replace("</p>", "\n\n")

    # Defensive final pass: strip any tag Telegram doesn't support (e.g.
    # <table>, <img>, or anything else this conversion didn't anticipate),
    # keeping the inner text rather than dropping content.
    def _strip_unknown(m: re.Match) -> str:
        return m.group(0) if m.group(1).lower() in _ALLOWED_TAGS else ""

    html = _ANY_TAG_RE.sub(_strip_unknown, html)
    html = re.sub(r"\n{3,}", "\n\n", html)

    return html.strip()
