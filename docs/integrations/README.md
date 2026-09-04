# Optional integrations

Everything here is off by default and independent of everything else. agent-box
works as a plain workspace runtime with none of it enabled; each page below
describes one thing you can turn on, what it exposes, and what it costs.

| Integration | Default | What it is |
| --- | --- | --- |
| [Shared skills](skills.md) | links on, superpowers off | One skill tree both agents read, plus the practice skills agent-box ships |
| [Vikunja](vikunja.md) | off | Task tracking both agents can read and update |
| [Flow-Next](flow-next.md) | detection automatic, auto-push off | Review routing, Flowctl resolution, fail-closed auto-push |
| [Agent browser](browser.md) | off | A headless browser inside the workspace box for agents to drive |
| [Telegram](telegram.md) | off | Anthropic's official channel for control, plus one-way notifications |
| [Git and GitHub](git.md) | identity on, credentials off | What of your Git and GitHub setup reaches a session |

All of them are answered in one place:

```bash
agent-box config
```

and applied by that command, or later by `agent-box install`. Starting an agent
applies nothing — see [Configuration](../../README.md#configuration).
