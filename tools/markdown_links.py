"""Markdown destinations shared by packaging and documentation checks."""
import re
from urllib.parse import urljoin, urlsplit, quote

QUOTE = re.compile(r"^(?: {0,3}> ?)*")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# A code span closes only on a backtick run of exactly its opening length.
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
TITLE = r"""(?:\s+(?:"[^"]*"|'[^']*'|\([^)]*\)))?"""
# A destination in angle brackets may contain spaces; a bare one ends at whitespace.
DESTINATION = r"(?:<([^<>\n]*)>|((?:[^<\s()]|\([^\s()]*\))(?:[^\s()]|\([^\s()]*\))*))"
INLINE_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*" + DESTINATION + TITLE + r"\s*\)")
REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s*" + DESTINATION + TITLE + r"\s*$", re.MULTILINE)
HEADING_LINK = re.compile(r"!?\[((?:[^\[\]]|\[[^\[\]]*\])*)\](?:\(\s*" + DESTINATION + TITLE + r"\s*\)|\[[^\]]*\])")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
EXPLICIT_ANCHOR = re.compile(r"""<a\s[^>]*?(?<![\w-])(?:id|name)\s*=\s*(?:"([^"]+)"|'([^']+)')""", re.IGNORECASE)
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

def prose(text: str) -> str:
    """Blank fenced blocks and code spans, keeping offsets and line numbers.

    A link or heading shown as an example inside code is not one, and an
    example of a broken link must not fail the suite.
    """
    lines = text.split("\n")
    fence = None
    for index, line in enumerate(lines):
        match = FENCE.match(QUOTE.sub("", line, count=1))
        if fence is None and match:
            fence = match.group(1)
            lines[index] = " " * len(line)
        elif fence is not None:
            # Only a bare fence closes the block; a fence with an info string is content.
            closes = match and not match.group(2).strip()
            if closes and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            lines[index] = " " * len(line)
    blanked = "\n".join(lines)
    return CODE_SPAN.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), blanked)


def destinations(text):
    """Keep offsets so packaging changes URLs without reformatting Markdown."""
    visible = prose(text)
    found = {}
    # Overlapping matches retain the image destination inside a linked badge.
    # Blank quote markers rather than removing them, so definition offsets stay valid.
    unquoted = "\n".join(QUOTE.sub(lambda m: " " * len(m.group()), line, count=1)
                         for line in visible.split("\n"))
    for pattern, searched in ((re.compile("(?=" + INLINE_LINK.pattern + ")"), visible),
                              (REFERENCE_DEFINITION, unquoted)):
        for match in pattern.finditer(searched):
            group = 1 if match.group(1) is not None else 2
            span = match.span(group)
            found.setdefault(span, (match.group(group), visible[match.start():].startswith("!")))
    for match in re.finditer(r'''\b(href|src)\s*=\s*(["'])(.*?)\2''', visible, re.IGNORECASE):
        found.setdefault(match.span(3), (match.group(3), match.group(1).lower() == "src"))
    return [(start, end, target, image) for (start, end), (target, image) in sorted(found.items())]


def absolute_links(text, page_url, image_url):
    for start, end, target, image in reversed(destinations(text)):
        url = urlsplit(target)
        if url.scheme or url.netloc:
            continue
        if target.startswith("/"):
            raise ValueError("Repository links must be relative to the document, not the host root")
        replacement = urljoin(image_url if image else page_url, quote(target, safe="/%#?=&:+@~"))
        text = text[:start] + replacement + text[end:]
    return text

