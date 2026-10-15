"""Relative links and anchors in the user documentation must resolve.

README.md and ``docs/`` link to each other's pages and headings, and those
headings move whenever a page is split or retitled. GitHub renders a broken
link without complaint, so a reader finds it only by following it. This test
resolves every relative link the way GitHub does: the target file or
directory must exist and an ``#anchor`` must name a heading slug or an
explicit ``<a id>``/``name`` on the target page. External URLs are not
checked; their availability is not something this repository controls.
"""
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1", re.DOTALL)
INLINE_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+\"[^\"]*\")?\s*$", re.MULTILINE)
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
EXPLICIT_ANCHOR = re.compile(r"<a\s[^>]*\b(?:id|name)\s*=\s*\"([^\"]+)\"", re.IGNORECASE)
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def documentation_files(root: Path) -> list[Path]:
    files = [root / "README.md"] if (root / "README.md").is_file() else []
    return files + sorted((root / "docs").rglob("*.md"))


def prose(text: str) -> str:
    """Blank fenced blocks and code spans, keeping offsets and line numbers.

    A link or heading shown as an example inside code is not one, and an
    example of a broken link must not fail the suite.
    """
    lines = text.split("\n")
    fence = None
    for index, line in enumerate(lines):
        match = FENCE.match(line)
        if fence is None and match:
            fence = match.group(1)
            lines[index] = ""
        elif fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            lines[index] = ""
    blanked = "\n".join(lines)
    return CODE_SPAN.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), blanked)


def slug(heading: str) -> str:
    """GitHub's heading id: rendered text, lowercased, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text).replace("`", "").lower()
    text = "".join(ch for ch in text if ch.isalnum() or ch in "_- ")
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    found = set(EXPLICIT_ANCHOR.findall(text))
    seen: dict[str, int] = {}
    for raw, visible in zip(text.split("\n"), prose(text).split("\n")):
        # The blanked line says whether this is a heading at all; the raw one keeps its code text.
        if not HEADING.match(visible):
            continue
        match = HEADING.match(raw)
        base = slug(match.group(2))
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return found


def broken_links(root: Path) -> list[str]:
    problems = []
    cache: dict[Path, set[str]] = {}
    for source in documentation_files(root):
        text = prose(source.read_text(encoding="utf-8"))
        targets = [(m.start(1), m.group(1)) for m in INLINE_LINK.finditer(text)]
        targets += [(m.start(1), m.group(1)) for m in REFERENCE_DEFINITION.finditer(text)]
        for offset, target in sorted(targets):
            if not target or SCHEME.match(target):
                continue
            where = f"{source.relative_to(root)}:{text.count(chr(10), 0, offset) + 1}: {target}"
            path_part, _, anchor = target.partition("#")
            resolved = (source.parent / unquote(path_part)).resolve() if path_part else source
            if not resolved.is_relative_to(root.resolve()):
                problems.append(f"{where} (outside the repository)")
                continue
            if not resolved.exists():
                problems.append(f"{where} (no such file)")
                continue
            if not anchor:
                continue
            page = resolved / "README.md" if resolved.is_dir() else resolved
            if page.suffix != ".md":
                continue
            if not page.is_file():
                problems.append(f"{where} (directory has no README.md for the anchor)")
                continue
            if page not in cache:
                cache[page] = anchors(page)
            if unquote(anchor) not in cache[page]:
                problems.append(f"{where} (no such anchor)")
    return problems


class DocumentationLinkTests(unittest.TestCase):
    def test_every_relative_link_and_anchor_resolves(self):
        problems = broken_links(ROOT)
        self.assertEqual(problems, [], "broken documentation links:\n" + "\n".join(problems))


class LinkCheckerTests(unittest.TestCase):
    """The checker must catch what readers would hit, or the guard above proves nothing."""

    def check(self, files: dict[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            return broken_links(root)

    def test_missing_file_is_reported_with_its_line(self):
        problems = self.check({"README.md": "# Title\n\nSee [usage](docs/usage.md).\n"})
        self.assertEqual(problems, ["README.md:3: docs/usage.md (no such file)"])

    def test_missing_anchor_on_another_page_is_reported(self):
        problems = self.check({
            "README.md": "# Title\n\n[setup](docs/guide.md#install-it)\n",
            "docs/guide.md": "# Guide\n\n## Installation\n",
        })
        self.assertEqual(problems, ["README.md:3: docs/guide.md#install-it (no such anchor)"])

    def test_same_page_anchor_follows_github_slugs(self):
        """Code, punctuation and duplicate headings change the id GitHub generates."""
        problems = self.check({"README.md": (
            "# Title\n\n## Use `config ai` (guided)\n\n## Notes\n\n## Notes\n\n"
            "[a](#use-config-ai-guided) [b](#notes-1) [c](#notes-2)\n"
        )})
        self.assertEqual(problems, ["README.md:9: #notes-2 (no such anchor)"])

    def test_links_shown_inside_code_are_not_checked(self):
        problems = self.check({"README.md": (
            "# Title\n\n```markdown\n[example](missing.md)\n```\n\nInline `[x](gone.md)` too.\n"
        )})
        self.assertEqual(problems, [])

    def test_link_text_may_wrap_across_lines(self):
        problems = self.check({"docs/a.md": "# A\n\nSee the [long\nlink text](b.md#nowhere).\n", "docs/b.md": "# B\n"})
        self.assertEqual(problems, ["docs/a.md:4: b.md#nowhere (no such anchor)"])

    def test_explicit_anchor_and_directory_links_resolve(self):
        problems = self.check({
            "README.md": '# Title\n\n[i](docs/) [j](docs/#overview) [k](docs/page.md#legacy-id)\n',
            "docs/README.md": "# Docs\n\n## Overview\n",
            "docs/page.md": '# Page\n\n<a id="legacy-id"></a>\n',
        })
        self.assertEqual(problems, [])

    def test_external_urls_are_not_checked(self):
        problems = self.check({"README.md": "# T\n\n[gh](https://github.com/x/y#z) [m](mailto:a@b.c)\n"})
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
