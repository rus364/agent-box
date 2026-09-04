# Shared skills

Claude reads skills from `~/.claude/skills`; Codex reads them from
`~/.agents/skills`. Left alone, that means maintaining two copies of the same
`SKILL.md` files and watching them drift.

agent-box treats `~/.agents/skills` as the one real directory and gives Claude
symlinks into it, so both agents consume the same files.

```text
~/.agents/skills/           the tree you and your agents maintain
    vikunja/                installed by agent-box
    systematic-debugging/   installed by agent-box (optional)
    my-own-skill/           yours

~/.claude/skills/           symlinks into the tree above
```

Override the tree's location with `AGENT_BOX_AGENTS_DIR`.

## The links into Claude's directory

On by default, but a setting rather than an assumption: those symlinks live in
your real Claude configuration, which Claude sessions started outside agent-box
also read.

```bash
agent-box config     # "Link shared skills into ~/.claude/skills"
```

Turning it off removes the links agent-box created and leaves everything else
in that directory alone. A name already occupied by a real directory, or by a
symlink pointing somewhere else, is never overwritten — agent-box warns and
keeps what is there.

## Skills agent-box ships

Some integrations need a skill to be usable at all, so agent-box carries its own
copies and installs them rather than waiting for you to produce one.

| Skill | Installed when |
| --- | --- |
| `vikunja` | the [Vikunja integration](vikunja.md) is on |
| `systematic-debugging`, `test-driven-development`, `verification-before-completion` | `skills.superpowers` is on |

An installed directory carries a marker file recording the fingerprint of the
copy that produced it. agent-box replaces it when it ships a newer copy, and
removes it when you turn the integration off. A directory of the same name that
agent-box did **not** create has no marker: it is yours — hand-written, or a
symlink into a checkout you are editing — and is never overwritten or deleted.

Nothing is installed or refreshed by starting an agent. `agent-box config` and
`agent-box config apply` are the only commands that touch this tree.

## The superpowers skills

Three engineering-practice skills, vendored from
[obra/superpowers](https://github.com/obra/superpowers) (MIT): work the bug
before the fix, write the test before the implementation, verify before
claiming done.

```bash
agent-box config     # "Install the superpowers practice skills"
```

Off by default — they change how an agent works, which is a choice, and your
project may already have its own conventions for the same ground.

They are checked into this repository rather than fetched at install time, so
what runs on your machine is what was reviewed, and an install needs no network
and no trust in a third party's current `main`. `agent_box/skills/VENDORED.json`
records the upstream, the release tag and the exact commit each copy came from,
and every copy carries the upstream MIT licence.

### Refreshing them (maintainers)

```bash
agent-box skills vendor superpowers
```

Clones the upstream at its latest stable tag, takes the three skills, points
their cross-references at the vendored copies, checks the result, and writes it
into `agent_box/skills/`. The checks are the point: a skill whose frontmatter
name no longer matches its directory, a restructured tree with nested
`SKILL.md` files, a `SKILL.md` still depending on the upstream plugin, or a
broken relative link all fail the vendoring instead of shipping.

A reference to a skill agent-box does *not* vendor is deliberately left as
written and reported, because rewriting it would produce a link to something
you do not have.

## The whole tree in the container

If `~/.agents` exists, the entire tree is bind-mounted read-only at its own
path, with `skills/` overlaid read-write so agents can create and update their
own skills. Symlinks are followed so they resolve inside the container:
external targets linked from `skills/` are read-write, targets linked only from
elsewhere in the tree stay read-only. A broken symlink is a warning, not a
failure.

The scope is deliberate. Narrowing the mount to `skills/` would not expose less
of the host — the tree is exposed either way — it would only leave the
non-skill half of it quietly broken inside the box.

## Checking it

```bash
agent-box doctor
```

Reports each shared skill and whether Claude's copy resolves to the same file.
With the links turned off it says so instead of reporting every skill as
missing.
