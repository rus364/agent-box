"""Bringing third-party skills into this repository, reproducibly.

``agent-box skills vendor <name>`` is a **maintainer** command, not a user
one. It clones an upstream project, takes the few skills agent-box actually
ships, applies the small adaptation they need to work standalone, checks the
result, and writes it into ``agent_box/skills/``. Users never run it: they get
the checked-in copies, installed by ``agent-box install``.

The alternative shapes were both worse. A git submodule would drag an entire
plugin repository -- hooks, marketplace manifests, dozens of skills nobody
asked for -- into every clone of agent-box. Fetching at install time would
make an offline install impossible and hand a third party the ability to
change what runs on a user's machine between two identical commands. Checking
in the files means what ships is what was reviewed, and this command is how
that copy is refreshed and how anyone can verify where it came from.

The adaptation is one substitution, and it is deliberately narrow. Upstream
skills reference each other as ``superpowers:<name>``, which resolves only
inside the Claude plugin that contains them; a reference to a skill agent-box
actually vendors becomes ``$<name>``, which is how a skill in
``~/.agents/skills`` is referenced. A reference to a skill that is *not*
vendored is left exactly as written: rewriting it would produce a link to
something that does not exist, which is worse than prose naming an upstream
skill the reader can go and find. Such a reference is reported, and is an
error if it appears in a ``SKILL.md``, where it would be a runtime dependency
rather than a remark.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .atomicio import atomic_write_json
from .errors import AgentBoxError
from .paths import MANAGED_SKILLS_SOURCE

#: Records where each vendored skill came from, so the provenance of a
#: checked-in copy is answerable without a network round trip.
VENDOR_MANIFEST = MANAGED_SKILLS_SOURCE / "VENDORED.json"

#: A stable upstream release, not a moving branch.
RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
MARKDOWN_LINK = re.compile(r"\]\(([^)]+)\)")


@dataclass(frozen=True)
class VendorSpec:
    """One upstream project and the skills taken from it."""

    name: str
    repo: str
    skills: tuple[str, ...]
    license_file: str = "LICENSE"
    #: Prefix that marks a cross-reference into the upstream plugin, e.g.
    #: ``superpowers:`` in ``superpowers:test-driven-development``.
    reference_prefix: str = ""


SPECS: dict[str, VendorSpec] = {
    "superpowers": VendorSpec(
        name="superpowers",
        repo="https://github.com/obra/superpowers.git",
        skills=("systematic-debugging", "test-driven-development",
                "verification-before-completion"),
        reference_prefix="superpowers:",
    ),
}


def _run(command: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            command, cwd=None if cwd is None else str(cwd),
            text=True, capture_output=True)
    except OSError as exc:
        raise AgentBoxError(f"{' '.join(command)} failed: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise AgentBoxError(f"{' '.join(command)} failed: {detail}")
    return result.stdout


def latest_release(repo: str) -> str:
    """The highest ``vMAJOR.MINOR.PATCH`` tag the upstream publishes."""
    output = _run(["git", "ls-remote", "--tags", repo])
    tags = {
        line.rsplit("/", 1)[-1] for line in output.splitlines()
        if "/tags/" in line and not line.endswith("^{}")
    }
    releases = sorted(
        (tag for tag in tags if RELEASE_TAG.match(tag)),
        key=lambda tag: tuple(int(part) for part in RELEASE_TAG.match(tag).groups()),
    )
    if not releases:
        raise AgentBoxError(f"no stable release tag found in {repo}")
    return releases[-1]


def _apply_rewrites(root: Path, spec: VendorSpec) -> list[str]:
    """Point cross-references at the vendored copies; report the rest.

    Only a reference to a skill this spec actually vendors is rewritten. One
    naming a skill that stays upstream is prose, and turning it into
    ``$name`` would fabricate a link to a skill the user does not have.
    """
    if not spec.reference_prefix:
        return []
    # A skill name may contain '.' and '-' but never ends with one, so the
    # trailing punctuation of an ordinary sentence stays out of the capture.
    # Swallowing it would be worse than cosmetic: 'upstream:beta.' would then
    # not match any vendored skill and would silently go un-rewritten.
    pattern = re.compile(
        re.escape(spec.reference_prefix) + r"([A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)*)")
    untouched: list[str] = []

    def replace(match: re.Match[str]) -> str:
        target = match.group(1)
        if target in spec.skills:
            return f"${target}"
        untouched.append(match.group(0))
        return match.group(0)

    for path in sorted(root.rglob("*.md")):
        original = path.read_text(encoding="utf-8")
        text = pattern.sub(replace, original)
        if text != original:
            path.write_text(text, encoding="utf-8")
    return untouched


def _check_skill(root: Path, name: str, spec: VendorSpec) -> None:
    """Everything that must hold before a vendored skill is written out.

    Each check exists because its failure would be silent otherwise: a skill
    whose frontmatter name stopped matching its directory is not the skill
    agent-box installs, a nested ``SKILL.md`` means the upstream restructured
    into something this vendoring does not understand, a plugin reference left
    in a ``SKILL.md`` is a runtime dependency on something the user does not
    have, and a broken relative link is a page an agent will try to read and
    fail to find.
    """
    skill_file = root / "SKILL.md"
    if not skill_file.is_file():
        raise AgentBoxError(f"{name}: no SKILL.md")
    declared = ""
    for line in skill_file.read_text(encoding="utf-8").splitlines()[:15]:
        if line.startswith("name:"):
            declared = line.split(":", 1)[1].strip()
            break
    if declared != name:
        raise AgentBoxError(f"{name}: frontmatter name is '{declared}'")
    nested = [p for p in root.rglob("SKILL.md") if p != skill_file]
    if nested:
        raise AgentBoxError(f"{name}: unexpected nested SKILL.md: {nested[0]}")
    prefix = spec.reference_prefix
    for path in sorted(root.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        if prefix and path.name == "SKILL.md" and prefix in text:
            raise AgentBoxError(
                f"{name}: SKILL.md still depends on {prefix}... which this "
                f"vendoring does not provide")
        for raw in MARKDOWN_LINK.findall(text):
            target = raw.split("#", 1)[0].split("?", 1)[0].strip()
            if not target or "://" in target or target.startswith("/"):
                continue
            if target.endswith(".md") and not (path.parent / target).exists():
                raise AgentBoxError(
                    f"{name}: broken link to {target} in {path.relative_to(root)}")


def vendor(spec: VendorSpec, destination: Path | None = None) -> dict[str, str]:
    """Fetch, adapt, check and write out one upstream's skills."""
    destination = MANAGED_SKILLS_SOURCE if destination is None else destination
    tag = latest_release(spec.repo)
    notes: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        checkout = Path(tmp) / "upstream"
        _run(["git", "clone", "--depth", "1", "--branch", tag, spec.repo, str(checkout)])
        commit = _run(["git", "rev-parse", "HEAD"], cwd=checkout).strip()
        license_text = ""
        license_path = checkout / spec.license_file
        if license_path.is_file():
            license_text = license_path.read_text(encoding="utf-8")
        staged = Path(tmp) / "staged"
        staged.mkdir()
        for name in spec.skills:
            source = checkout / "skills" / name
            if not source.is_dir():
                raise AgentBoxError(f"{spec.name} {tag} has no skills/{name}")
            target = staged / name
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            for reference in _apply_rewrites(target, spec):
                notes.append(f"{name}: left as prose, not vendored: {reference}")
            # Vendoring a copy carries its licence with it; the upstream's own
            # terms travel in the directory a user ends up with, not only in a
            # note in this repository.
            if license_text:
                (target / "LICENSE").write_text(license_text, encoding="utf-8")
            _check_skill(target, name, spec)
        destination.mkdir(parents=True, exist_ok=True)
        for name in spec.skills:
            final = destination / name
            shutil.rmtree(final, ignore_errors=True)
            shutil.copytree(staged / name, final)
    for note in notes:
        print(f"agent-box: {note}")
    return {"source": spec.repo, "tag": tag, "commit": commit}


def read_manifest(path: Path | None = None) -> dict:
    from .atomicio import read_json
    path = VENDOR_MANIFEST if path is None else path
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def vendor_main(args: list[str]) -> int:
    """``agent-box skills vendor <name>`` -- maintainer-only."""
    if len(args) != 2 or args[0] != "vendor":
        raise AgentBoxError(
            f"expected: agent-box skills vendor <{'|'.join(sorted(SPECS))}>")
    spec = SPECS.get(args[1])
    if spec is None:
        raise AgentBoxError(
            f"unknown upstream '{args[1]}'; known: {', '.join(sorted(SPECS))}")
    record = vendor(spec)
    manifest = read_manifest()
    manifest[spec.name] = {**record, "skills": list(spec.skills)}
    atomic_write_json(VENDOR_MANIFEST, manifest)
    print(f"agent-box: vendored {spec.name} {record['tag']} ({record['commit'][:12]})")
    for name in spec.skills:
        print(f"  {MANAGED_SKILLS_SOURCE / name}")
    print(f"agent-box: recorded in {VENDOR_MANIFEST}")
    return 0
