---
name: ab-config
description: Configure agent-box for this project with the user through guided questions, explained recommendations and checked writes.
---

# ab-config

Speak the user's language, and relay command output in it. Configure agent-box together with the user; never assume consent.

1. Learn the current state first: run `agent-box config describe` (or `agent-box config describe PATH` for one section) and read the repository: languages, layout, tests, CI, and sensitive areas such as auth, migrations, infrastructure and secret handling. In the JSON, `source` says where each effective value comes from, `host-only` means the value is unknown in this session, and `project_allowed` says whether `.agent-box.json` may override the setting.
2. Ask briefly about goals and constraints (review strictness, cost and speed, delivery route, tooling, browser use), one question at a time, with 2-4 concrete options and a recommendation.
3. Cover only sections with a meaningful decision, for example review triggers (`workflow.review.policy`, `sensitive_paths`, `ignored_paths`, `max_files`, `max_lines`), Plan sync (`workflow.sync.policy`, `events`, `scope`, `references`, `paths`) and scout, worker, reviewer and sync candidate lists (`workflow.ROLE.models`, ordered `{"provider", "model", "effort"}` objects; another CLI's model runs managed; worker, reviewer and sync `tiers.N` apply only with `workflow.use_tiers`), delivery (`workflow.merge_mode`, `workflow.auto_push`) image tool groups (`box.tools.GROUP`), the isolated browser (`box.browser.enabled`) and the per-project k3s cluster (`box.k3s.*`, see docs/integrations/k3s.md). For each proposal explain its purpose, consequences and alternatives, and say whether it is a project override or a global setting. Prefer project overrides; propose a global change only when the user wants it for every project.
4. Suggest models from `suggestions` or an ID the user gives; never claim a model is available to their account.
5. Never ask for, display or write credentials, tokens, API keys or environment values; send the user to `agent-box config` for those. Never edit `config.json` or `.agent-box.json` by hand.
6. Before writing, show one summary of all changes and get explicit consent. Preview with `--dry-run`, then write:
   - project: `agent-box config set --project PATH=JSON... [--reset PATH]...`
   - global: `agent-box config set --global PATH=JSON... [--reset PATH]...` (saves a proposal only)
   Values are JSON: `true`, `5`, `"text"`, `["a"]`. `--reset` removes a project override or restores a global built-in value. If a command refuses, explain why and adjust.
7. Finish with a summary of what changed. Global proposals take effect only after the user confirms them in their own host terminal with `agent-box config proposal apply`; `agent-box config ai` offers this when the session ends. Changes to `box.tools` also need `agent-box build`. The isolated browser uses a separate image and needs no workspace rebuild; first use downloads it through local rootless Podman.
