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

from tools.markdown_links import (
    QUOTE, CODE_SPAN, INLINE_LINK, REFERENCE_DEFINITION,
    HEADING_LINK, HEADING, EXPLICIT_ANCHOR, SCHEME, prose,
)

ROOT = Path(__file__).resolve().parent.parent


def documentation_files(root: Path) -> list[Path]:
    files = [root / name for name in ("README.md", "CHANGELOG.md") if (root / name).is_file()]
    return files + sorted((root / "docs").rglob("*.md"))


def slug(heading: str) -> str:
    """GitHub's heading id: rendered text, lowercased, punctuation dropped, spaces to hyphens."""
    def rendered(part: str) -> str:
        part = re.sub(r"<[^>]+>", "", part)
        # Emphasis delimiters flank words; an underscore inside a word is literal text.
        return re.sub(r"(?<!\w)[*_]+|[*_]+(?!\w)", "", part)

    # A link renders as its text, which may itself hold code; link syntax shown inside code stays.
    code = [(m.start(), m.end()) for m in CODE_SPAN.finditer(heading)]
    unlinked, end = [], 0
    for link in HEADING_LINK.finditer(heading):
        if not any(start <= link.start() < stop for start, stop in code):
            unlinked += [heading[end:link.start()], link.group(1)]
            end = link.end()
    heading = "".join(unlinked) + heading[end:]
    # Inline code renders verbatim, so only the text around it loses HTML tags and emphasis.
    parts, end = [], 0
    for code in CODE_SPAN.finditer(heading):
        parts += [rendered(heading[end:code.start()]), code.group(2)]
        end = code.end()
    parts.append(rendered(heading[end:]))
    text = "".join(ch for ch in "".join(parts).lower() if ch.isalnum() or ch in "_- ")
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    found = {double or single for double, single in EXPLICIT_ANCHOR.findall(prose(text))}
    occurrences: dict[str, int] = {}
    for raw, visible in zip(text.split("\n"), prose(text).split("\n")):
        # The blanked line says whether this is a heading at all; the raw one keeps its code text.
        if not HEADING.match(QUOTE.sub("", visible, count=1)):
            continue
        match = HEADING.match(QUOTE.sub("", raw, count=1))
        # github-slugger: a suffix already taken by another heading is skipped.
        base = result = slug(match.group(2))
        while result in occurrences:
            occurrences[base] += 1
            result = f"{base}-{occurrences[base]}"
        occurrences[result] = 0
        found.add(result)
    return found


def broken_links(root: Path) -> list[str]:
    problems = []
    cache: dict[Path, set[str]] = {}
    for source in documentation_files(root):
        text = prose(source.read_text(encoding="utf-8"))
        # Quote markers are dropped per line, so a definition inside a blockquote keeps its line number.
        unquoted = "\n".join(QUOTE.sub("", line, count=1) for line in text.split("\n"))
        targets = []
        for pattern, searched in ((INLINE_LINK, text), (REFERENCE_DEFINITION, unquoted)):
            for m in pattern.finditer(searched):
                group = 1 if m.group(1) is not None else 2
                targets.append((searched.count("\n", 0, m.start(group)), m.group(group)))
        for newlines, target in sorted(targets):
            if not target or SCHEME.match(target):
                continue
            where = f"{source.relative_to(root)}:{newlines + 1}: {target}"
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

    def test_duplicate_suffix_skips_an_existing_heading_id(self):
        problems = self.check({"README.md": "# T\n\n## Notes\n\n## Notes-1\n\n## Notes\n\n[a](#notes-2)\n"})
        self.assertEqual(problems, [])

    def test_fenced_example_inside_a_blockquote_is_not_checked(self):
        problems = self.check({"README.md": "# T\n\n> ~~~markdown\n> [example](missing.md)\n> ~~~\n"})
        self.assertEqual(problems, [])

    def test_emphasis_does_not_reach_the_slug(self):
        problems = self.check({"README.md": "# T\n\n## _Setup_ for snake_case\n\n[a](#setup-for-snake_case)\n"})
        self.assertEqual(problems, [])

    def test_fence_with_an_info_string_does_not_close_a_block(self):
        problems = self.check({"README.md": (
            "# T\n\n```\n```python\n```\n\n[x](missing.md)\n"
        )})
        self.assertEqual(problems, ["README.md:7: missing.md (no such file)"])

    def test_heading_inside_a_blockquote_is_an_anchor(self):
        problems = self.check({"README.md": "# T\n\n> ## Setup\n\n[go](#setup)\n"})
        self.assertEqual(problems, [])

    def test_linked_code_in_a_heading_keeps_only_the_code_text(self):
        problems = self.check({"README.md": (
            "# T\n\n## [`config ai`](README.md)\n\n## Show `[x](y)`\n\n"
            "[a](#config-ai) [b](#config-aiconfigurationmd) [c](#show-xy)\n"
        )})
        self.assertEqual(problems, ["README.md:7: #config-aiconfigurationmd (no such anchor)"])

    def test_heading_link_with_parentheses_in_its_destination(self):
        problems = self.check({
            "README.md": "# T\n\n## [Setup](docs/guide(v2).md)\n\n[a](#setup) [b](#setupmd)\n",
            "docs/guide(v2).md": "# Guide\n",
        })
        self.assertEqual(problems, ["README.md:5: #setupmd (no such anchor)"])

    def test_reference_link_label_does_not_reach_the_slug(self):
        problems = self.check({"README.md": (
            "# T\n\n## [Setup][guide]\n\n[guide]: README.md\n\n[a](#setup) [b](#setupguide)\n"
        )})
        self.assertEqual(problems, ["README.md:7: #setupguide (no such anchor)"])

    def test_code_span_closes_only_on_an_equal_backtick_run(self):
        problems = self.check({"README.md": "# T\n\nA `tick [bad](missing.md) `` run.\n"})
        self.assertEqual(problems, ["README.md:3: missing.md (no such file)"])

    def test_reference_definition_inside_a_blockquote_is_checked(self):
        problems = self.check({"README.md": "# T\n\n> [Guide][g]\n>\n> [g]: missing.md\n> [h]: README.md#nowhere\n"})
        self.assertEqual(problems, [
            "README.md:5: missing.md (no such file)",
            "README.md:6: README.md#nowhere (no such anchor)",
        ])

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

    def test_links_with_any_title_delimiter_are_checked(self):
        problems = self.check({"README.md": "# T\n\n[a](x.md \"X\") [b](y.md 'Y') [c](z.md (Z))\n"})
        self.assertEqual(problems, [
            "README.md:3: x.md (no such file)",
            "README.md:3: y.md (no such file)",
            "README.md:3: z.md (no such file)",
        ])

    def test_anchor_shown_inside_code_does_not_count(self):
        """An example anchor in a code block is not on the rendered page, so a link to it is broken."""
        problems = self.check({"README.md": '# T\n\n```html\n<a id="example"></a>\n```\n\n[e](#example)\n'})
        self.assertEqual(problems, ["README.md:7: #example (no such anchor)"])

    def test_only_real_id_and_name_attributes_are_anchors(self):
        problems = self.check({"README.md": (
            "# T\n\n<a data-id=\"fake\"></a>\n<a id='single'></a>\n\n[f](#fake) [s](#single)\n"
        )})
        self.assertEqual(problems, ["README.md:6: #fake (no such anchor)"])

    def test_inline_code_keeps_angle_brackets_in_the_slug(self):
        """GitHub keeps text inside code, so `<NAME>` contributes "name" to the id."""
        problems = self.check({"README.md": "# T\n\n## `--env=<NAME>`\n\n[a](#--envname) [b](#--env)\n"})
        self.assertEqual(problems, ["README.md:5: #--env (no such anchor)"])

    def test_angle_bracket_destination_may_contain_spaces(self):
        problems = self.check({"README.md": "# T\n\n[g](<docs/user guide.md#missing>)\n", "docs/user guide.md": "# Guide\n"})
        self.assertEqual(problems, ["README.md:3: docs/user guide.md#missing (no such anchor)"])

    def test_destination_may_contain_balanced_parentheses(self):
        problems = self.check({"README.md": "# T\n\n[a](docs/guide(v2).md) [b][g]\n\n[g]: docs/other(v2).md\n"})
        self.assertEqual(problems, [
            "README.md:3: docs/guide(v2).md (no such file)",
            "README.md:5: docs/other(v2).md (no such file)",
        ])

    def test_external_urls_are_not_checked(self):
        problems = self.check({"README.md": "# T\n\n[gh](https://github.com/x/y#z) [m](mailto:a@b.c)\n"})
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
