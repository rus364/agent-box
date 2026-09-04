"""Getting ``agent-box`` onto PATH, and back off it.

Installing agent-box is a ``git clone``; what makes it *runnable* is a symlink
in a directory the shell already searches, plus -- only when no such directory
exists -- a line in a shell rc file. Both live in places the user owns, so
they are two separate questions, each declinable, each printing the exact
command or line to run by hand instead.

The asymmetry between writing and removing is deliberate. Appending a marked
block to ``~/.bashrc`` is additive and reversible by anyone reading the file.
Deleting one is not: the block may have been edited since, so a match on the
markers alone could take away lines the user wrote themselves. Removal
therefore requires the contents between the markers to be, character for
character, the single line agent-box generates -- for whatever directory that
line names, since by then nothing records which directory was chosen. Every
other case -- an extra line, an altered guard, doubled markers, a marker
without its pair -- leaves the file untouched and is reported with its
location, for the user to decide about.
"""
from __future__ import annotations

import os
import pwd
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .atomicio import atomic_write_text
from .errors import AgentBoxError
from .paths import HOST_HOME

#: The name the shell must resolve.
COMMAND = "agent-box"

#: Where agent-box suggests putting the symlink when nothing better exists.
#: A convention (systemd, pipx, cargo and the Codex installer all use it)
#: rather than a preference, so a user who already has it on PATH -- the
#: common case -- needs no shell rc change at all.
DEFAULT_BIN_DIR = HOST_HOME / ".local/bin"

#: Fenced exactly like the block Codex's installer writes, because the point
#: of a marker is to be recognisable to a human scrolling their rc file, and
#: a second convention would only be a second thing to learn.
RC_BEGIN = "# >>> agent-box >>>"
RC_END = "# <<< agent-box <<<"

RC_FILES = {"bash": HOST_HOME / ".bashrc", "zsh": HOST_HOME / ".zshrc"}

#: The one line the block contains. The guard makes re-sourcing idempotent.
RC_LINE = '[[ ":${{PATH}}:" == *":{dir}:"* ]] || export PATH="{dir}:${{PATH}}"'


def checkout_root() -> Path:
    """The directory holding the ``agent_box`` package and the shim next to it."""
    return Path(__file__).resolve().parent.parent


def shim_path() -> Path:
    """The executable a symlink should point at."""
    return checkout_root() / COMMAND


def path_entries(raw: str | None = None) -> list[Path]:
    """PATH, as absolute directories, order preserved and duplicates dropped.

    Unlike every other path in agent-box this one *must* come from the
    environment: the question being answered is what the user's shell would
    actually resolve, and that is defined by their PATH and nothing else.
    """
    value = os.environ.get("PATH", "") if raw is None else raw
    seen: list[Path] = []
    for item in value.split(os.pathsep):
        if not item:
            continue
        entry = Path(item)
        if entry.is_absolute() and entry not in seen:
            seen.append(entry)
    return seen


def is_agent_box_shim(path: Path) -> bool:
    """Does this file look like an agent-box launcher, rather than a namesake?"""
    return (path.parent / "agent_box" / "__init__.py").is_file()


@dataclass(frozen=True)
class LinkState:
    """What ``agent-box`` currently resolves to for this user's shell."""

    #: ``ours`` (this checkout), ``other`` (a different agent-box checkout),
    #: ``foreign`` (something else entirely) or ``missing``.
    kind: str
    #: The name found on PATH, before resolution.
    found: Path | None = None
    #: What it resolves to.
    target: Path | None = None


def link_state() -> LinkState:
    found = shutil.which(COMMAND)
    if not found:
        return LinkState("missing")
    found_path = Path(found)
    target = found_path.resolve()
    if target == shim_path().resolve():
        return LinkState("ours", found_path, target)
    if is_agent_box_shim(target):
        return LinkState("other", found_path, target)
    return LinkState("foreign", found_path, target)


def _writable(directory: Path) -> bool:
    """Can this directory be written to, creating it if need be?"""
    probe = directory
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            return False
        probe = parent
    return probe.is_dir() and os.access(probe, os.W_OK)


@dataclass(frozen=True)
class BinDir:
    """A directory the symlink could go in, and what putting it there costs."""

    path: Path
    on_path: bool
    exists: bool

    @property
    def needs_rc_line(self) -> bool:
        return not self.on_path

    def actions(self) -> list[str]:
        """Every change installing here would make, in the order it happens.

        Spelled out in full before anything is asked, because "create a
        directory" and "edit your shell startup file" are not what someone
        expects from the word "symlink".
        """
        steps: list[str] = []
        if not self.exists:
            steps.append(f"create the directory {self.path}")
        steps.append(f"create the symlink {self.path / COMMAND} -> {shim_path()}")
        if self.needs_rc_line:
            steps.append(f"add {self.path} to PATH in your shell startup file")
        return steps


def bin_dir_choices() -> list[BinDir]:
    """Where the symlink could go, best first.

    Directories already on PATH come first: choosing one of them is the whole
    job, with no shell file to edit and nothing to restart. Only when the user
    has none that agent-box may write to does the fallback -- creating
    ``~/.local/bin`` and putting it on PATH -- appear, and it appears last so
    the cheap answer is the default answer.

    Everything outside the user's own home is excluded. ``/usr/local/bin``
    would need root, and a launcher that asks for root to install a symlink
    has misunderstood what it is.
    """
    choices: list[BinDir] = []
    for entry in path_entries():
        if entry != HOST_HOME and HOST_HOME not in entry.parents:
            continue
        if not entry.is_dir() or not _writable(entry):
            continue
        choices.append(BinDir(entry, on_path=True, exists=True))
    if not any(choice.path == DEFAULT_BIN_DIR for choice in choices):
        if _writable(DEFAULT_BIN_DIR):
            choices.append(BinDir(
                DEFAULT_BIN_DIR, on_path=False, exists=DEFAULT_BIN_DIR.is_dir()))
    return choices


def install_symlink(directory: Path) -> str:
    """Point ``directory/agent-box`` at this checkout's shim.

    Refuses to replace anything that is not a symlink into an agent-box
    checkout. A real file called ``agent-box`` in a PATH directory is the
    user's, whatever it is, and inheriting the name silently would be the one
    mistake this whole flow exists to avoid.
    """
    link = directory / COMMAND
    shim = shim_path()
    if link.is_symlink():
        current = link.resolve()
        if current == shim.resolve():
            return f"{link} already points at this checkout"
        if not is_agent_box_shim(current):
            raise AgentBoxError(
                f"{link} is a symlink to {current}, which is not an agent-box "
                f"checkout; remove it yourself if you want this one there")
        link.unlink()
    elif link.exists():
        raise AgentBoxError(
            f"{link} exists and is not a symlink; remove it yourself if you "
            f"want agent-box there")
    directory.mkdir(parents=True, exist_ok=True)
    link.symlink_to(shim)
    return f"linked {link} -> {shim}"


def installed_symlinks() -> list[Path]:
    """Every symlink on PATH, or in the default directory, that is ours.

    Ours means "resolves to an agent-box shim" rather than "resolves to *this*
    checkout": uninstalling from a second clone should still find the link,
    and a link left pointing at a checkout that no longer exists is exactly
    the dangling command this cleans up.
    """
    found: list[Path] = []
    for directory in [*path_entries(), DEFAULT_BIN_DIR]:
        link = directory / COMMAND
        if link in found or not link.is_symlink():
            continue
        target = link.resolve()
        if is_agent_box_shim(target) or not target.exists():
            found.append(link)
    return found


def remove_symlinks() -> list[Path]:
    removed: list[Path] = []
    for link in installed_symlinks():
        try:
            link.unlink()
        except OSError:
            continue
        removed.append(link)
    return removed


# -- the shell rc block ---------------------------------------------------

def login_shell() -> str:
    """The user's login shell name, from passwd rather than ``$SHELL``.

    Same reasoning as :data:`agent_box.paths.HOST_HOME`: ``$SHELL`` describes
    whatever spawned this process, which is not necessarily the shell whose
    startup file the user wants edited, and is settable by anything.
    """
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_shell or "").name
    except KeyError:
        return ""


def rc_file() -> Path | None:
    """Which startup file to offer, or ``None`` when agent-box will not guess.

    fish is deliberately absent: its PATH syntax is not POSIX (``fish_add_path``),
    so writing a bash line into ``config.fish`` would break the shell rather
    than extend it. Anything unrecognised gets printed instructions instead.
    """
    return RC_FILES.get(login_shell())


def rc_block(bin_dir: Path) -> str:
    """The exact text agent-box writes -- and the only text it will remove.

    ``${HOME}`` rather than the passwd home spelled out: this line is
    evaluated by the user's own shell, next to their own PATH lines, and the
    rule that ``$HOME`` never moves an agent-box path is about paths agent-box
    itself resolves, not about text it hands to a shell.

    The guard makes re-sourcing the file idempotent, so the block is harmless
    if the rc runs twice -- and matches, character for character, what the
    Codex installer writes a few lines above it on a typical machine.
    """
    if HOST_HOME in bin_dir.parents:
        dir_expr = f"${{HOME}}/{bin_dir.relative_to(HOST_HOME)}"
    else:
        dir_expr = str(bin_dir)
    return f"{RC_BEGIN}\n{RC_LINE.format(dir=dir_expr)}\n{RC_END}\n"


#: The same line, as a pattern, so a block can be recognised as ours without
#: knowing which directory it was written for -- ``uninstall`` runs long after
#: that choice was made, and asking it to guess would mean either refusing to
#: remove a block agent-box did write or matching one it did not.
RC_LINE_PATTERN = re.compile(
    r'^\[\[ ":\$\{PATH\}:" == \*":(?P<dir>[^"]+):"\* \]\] \|\| '
    r'export PATH="(?P<again>[^"]+):\$\{PATH\}"$')


def _is_our_block(body: list[str]) -> bool:
    """Is this block byte-for-byte a block agent-box writes, for some directory?"""
    if len(body) != 1:
        return False
    match = RC_LINE_PATTERN.match(body[0].rstrip("\n"))
    return bool(match) and match.group("dir") == match.group("again")


@dataclass(frozen=True)
class RcState:
    """What agent-box found in a shell rc file, and whether it may touch it."""

    #: ``absent``, ``exact`` (byte-identical to what we write), ``modified``
    #: (marked block, different contents), ``duplicated`` (more than one) or
    #: ``partial`` (one marker without its pair).
    kind: str
    #: 1-based line range of the block, for telling the user where to look.
    span: tuple[int, int] | None = None


def rc_state(path: Path) -> RcState:
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return RcState("absent")
    starts = [i for i, line in enumerate(lines) if line.strip() == RC_BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == RC_END]
    if not starts and not ends:
        return RcState("absent")
    if len(starts) != 1 or len(ends) != 1:
        return RcState("duplicated")
    start, end = starts[0], ends[0]
    if end < start:
        return RcState("partial")
    span = (start + 1, end + 1)
    if _is_our_block(lines[start + 1:end]):
        return RcState("exact", span)
    return RcState("modified", span)


def _real_rc_path(path: Path) -> Path:
    """Follow a symlinked rc file to the file it actually is.

    Dotfiles are routinely symlinks into a repository managed by stow,
    chezmoi or the like. Writing through ``os.replace`` at the symlink would
    replace the link with a regular file and quietly detach the user's shell
    from their dotfiles repo, which is a far worse outcome than not editing
    anything.
    """
    return path.resolve() if path.is_symlink() else path


def append_rc_block(path: Path, bin_dir: Path) -> str:
    """Append the marked block, preserving what the file already is.

    Two properties the naive version loses. ``atomic_write_text`` creates its
    temporary file with ``mkstemp``'s ``0600``, so the mode has to be carried
    over explicitly or a world-readable ``~/.bashrc`` silently becomes private
    -- harmless here, surprising anywhere it matters. And ``os.replace``
    breaks hard links, so a file with more than one link is reported rather
    than rewritten.
    """
    real = _real_rc_path(path)
    state = rc_state(real)
    if state.kind == "exact":
        return f"{real} already has the agent-box PATH block"
    if state.kind != "absent":
        raise AgentBoxError(
            f"{real} has an agent-box block at lines {state.span[0]}-{state.span[1]} "
            f"that does not match what agent-box writes; leaving it alone")
    mode: int | None = None
    if real.exists():
        info = real.stat()
        if info.st_nlink > 1:
            raise AgentBoxError(
                f"{real} has {info.st_nlink} hard links; agent-box will not "
                f"rewrite it. Add the line by hand:\n{rc_block(bin_dir)}")
        mode = stat.S_IMODE(info.st_mode)
        existing = real.read_text(encoding="utf-8")
    else:
        existing = ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    separator = "\n" if existing else ""
    atomic_write_text(real, existing + separator + rc_block(bin_dir), mode=mode)
    return f"added the agent-box PATH block to {real}"


def remove_rc_block(path: Path) -> tuple[bool, str | None]:
    """Delete the block, and only when it is exactly the block we wrote.

    Returns whether anything was removed and, when it was not, a line saying
    what is still there and where -- never an exception, because "I did not
    touch your shell config" is a normal outcome here, not a failure. See this
    module's docstring for why removal is the direction with the strict rule.
    """
    real = _real_rc_path(path)
    state = rc_state(real)
    if state.kind == "absent":
        return False, None
    where = f"{real} lines {state.span[0]}-{state.span[1]}" if state.span else str(real)
    reason = {
        "modified": "it has been edited since agent-box wrote it",
        "duplicated": "it appears more than once",
        "partial": "its markers are unpaired",
    }
    if state.kind != "exact":
        return False, f"left the PATH block in {where}: {reason[state.kind]}"
    info = real.stat()
    if info.st_nlink > 1:
        return False, f"left the PATH block in {where}: the file has {info.st_nlink} hard links"
    lines = real.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = state.span[0] - 1, state.span[1]
    # A block appended after a blank separator leaves that blank behind.
    if start and not lines[start - 1].strip():
        start -= 1
    remaining = "".join(lines[:start] + lines[end:])
    atomic_write_text(real, remaining, mode=stat.S_IMODE(info.st_mode))
    return True, f"removed the agent-box PATH block from {real}"


def manual_instructions(bin_dir: Path) -> list[str]:
    """What to run by hand, for anyone who declines either half."""
    lines = [f"  ln -s {shim_path()} {bin_dir / COMMAND}"]
    if not any(entry == bin_dir for entry in path_entries()):
        target = rc_file()
        where = str(target) if target else "your shell startup file"
        lines.append(f"  # then put {bin_dir} on PATH in {where}:")
        lines.extend(f"  {line}" for line in rc_block(bin_dir).splitlines())
    return lines
