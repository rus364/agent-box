"""Build-only metadata; release identity comes from pyproject, never Git or the network."""
from pathlib import Path
import re
import tomllib

from tools.markdown_links import absolute_links


def configuration(root):
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    release = data["tool"]["agent-box"]["release"]
    version = data["project"]["version"]
    values = {"repository": release["repository"].rstrip("/"), "version": version, "tag": "v" + version}
    return release, values


def published_text(text, document, values):
    return absolute_links(text, f"{values['repository']}/blob/{values['tag']}/{document}",
                          f"{values['repository']}/raw/{values['tag']}/{document}")


def distribution_metadata(root):
    root = Path(root)
    release, values = configuration(root)
    return {
        "long_description": published_text((root / release["readme"]).read_text(encoding="utf-8"),
                                           release["readme"], values),
        "long_description_content_type": "text/markdown",
        "project_urls": {name: template.format(**values) for name, template in release["urls"].items()},
    }


def release_notes(root):
    release, values = configuration(root)
    text = (root / release["changelog"]).read_text(encoding="utf-8")
    sections = re.split(r"^## ([^\n]+)\n", text, flags=re.MULTILINE)
    entries = [body.strip() for title, body in zip(sections[1::2], sections[2::2])
               if title.strip() == values["version"]]
    if len(entries) != 1 or not entries[0]:
        raise ValueError("Changelog must contain one nonempty entry for the release version")
    return published_text(entries[0], release["changelog"], values) + "\n"
