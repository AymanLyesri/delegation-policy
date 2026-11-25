# delegation-policy

A standalone Hermes plugin that keeps a delegation policy permanently in the
main agent's instructions, so the cheap orchestrator model routes work instead
of doing it: planning, review and heavy thinking always go to planner one-shot
subagents, task execution to worker subagents. Which model plays each role is a
setting in `config.yaml` (`delegation.roles`), not text in this repo.

"Planning" and "review" have exact tool signatures, so a hook can enforce them.
"Heavy thinking" - the questions with no mechanical oracle, where a cheap model
produces a confident wrong answer - has none, so that lane is voluntary and
policed after the fact by rework: a worker fix that got reworked escalates to
the planner. The one-glance rule for the lane: if a worker could falsify its own
answer by running a command, it is execution, not thinking.

Since v2.0.0 the policy is also ENFORCED by a `pre_tool_call` hook. Prose in
a prompt was not enough: the orchestrator kept reading the plan/review skills
and writing plans itself. See "## The gate" below.

The plugin does ONLY this. It is not an orchestration engine: Hermes' native
`delegate_task` already exists, and `config.yaml` already selects every model.
Nothing here SETS a model - it renders the configured names into the prompt and
the gate's command, once per new session. Tools and core code are untouched.

## Architecture

```
delegation-policy/
+-- plugin.yaml     manifest (name, version, description, provides_hooks)
+-- __init__.py     register(ctx): one system-prompt-section + the pre_tool_call gate
+-- policy.md       the policy text - the part you edit
+-- README.md       this file
```

## The gate

The hook is the part that actually bites. Blocked, on a gated session:

| tool | condition |
|---|---|
| `skill_view` | name is `plan`, `writing-plans` or `requesting-code-review` (any namespace prefix) |
| `read_file` | path is `<...>/skills/<that skill>/SKILL.md` - without this, the documented "read the SKILL.md directly" fallback routes around the block |
| `write_file`, `patch` | path contains `.hermes/plans/` |
| `terminal` | command contains `.hermes/plans/` AND a write verb (redirect, tee, cp, mv, sed -i, rm) |

Heavy thinking is deliberately NOT in this table: it has no tool signature, so
the hook cannot see it. That lane is audited after the fact instead - a worker
fix reworked because its diagnosis was wrong escalates the re-diagnosis to the
planner. Any planner pass unlocks the gate, including a heavy-thinking pass, so
a thinking pass still owes its own planning brief.

The block message names the exact command to run, built from the configured
planner model. Unlock rule: a row in `$HERMES_HOME/state.db` with
`sessions.source='oneshot'`, a model starting with the configured planner or
reviewer model, `started_at` AFTER this session's last block for the same act,
and newer than 15 minutes. Evidence is the recorded run, never the command string, so `echo`
or a failed spawn unlocks nothing, and planning and review buy separate
passes. A first gated call in a session always blocks once (it arms the
anchor).

Scoping: `pre_tool_call` hooks get no `platform`, so the gate reads
`sessions.source` itself. `subagent` (delegate_task workers), `oneshot` (the
GLM pass and any `hermes chat -q`) and `cron` are exempt; everything else is
gated. A missing row or a failing lookup fails OPEN and writes an error line,
so a broken store can never wedge a turn.

Ledger: every decision appends one line to
`$HERMES_HOME/logs/delegation-policy-gate.jsonl` (`block`, `allow` with the
evidence timestamp, or `error`). Nothing else is logged.

Known holes, by design or by limit of the mechanism:

- Prose. A plan written into the chat is invisible to a hook.
- `execute_code` and any tool that writes files without a `path` arg.
- A DeepSeek `delegate_task` child is exempt and can write a plan.
- Evidence is global, not per session: a planner pass in another window counts.
- A gateway restart clears the anchors; every session then blocks once more
  (the safe direction).

Proof, in order, from a live session: `block` in the ledger for the local
session -> a oneshot row for the configured planner appears -> the retried call
logs `allow`
with `evidence` equal to that row's `started_at` -> a review-skill call still
blocks -> no `error` lines anywhere between.

## Which model plays which role (settings, not hardcoded)

Four roles, four owners in `config.yaml`. Change a model with `hermes config
set` and start a new session; nothing in this plugin needs editing.

| role | config key | what it does |
|---|---|---|
| orchestrator | `model.default` + `model.provider` | routes, relays, talks to the user. Read from the live session, so the prompt names the model that is actually answering. |
| planner | `delegation.roles.planner.model` + `.provider` | one-shot planning and heavy-thinking passes (`hermes chat -q ... -m <model> --provider <provider>`) |
| reviewer | `delegation.roles.reviewer.*` | the review pass after the work; falls back to the planner pair when unset |
| worker | `delegation.model` + `delegation.provider` | `delegate_task` children: implementation, research, debugging, testing. Falls back to the orchestrator model, which is what Hermes does when the key is unset |

Absent keys fall back to the historical pair (`glm-5.3` on `opencode-go`), so an
empty config reproduces the old behaviour exactly. The gate's unlock evidence
follows the planner and reviewer models, so moving the planner off GLM keeps the
gate working instead of wedging it:

```bash
hermes config set delegation.roles.planner.model glm-5.3 --force
hermes config set delegation.roles.planner.provider opencode-go --force
hermes config set delegation.roles.reviewer.model glm-5.3 --force
hermes config set delegation.roles.reviewer.provider opencode-go --force
hermes config set delegation.model deepseek-v4.1-flash --force
hermes config set delegation.provider opencode-go --force
```

`--force` silences core's "not a recognized config key" notice: the keys are
read by this plugin, not by core. Leaving a key out is safe - it falls back.

## Installation

The plugin is a user plugin: this directory under `~/.hermes/plugins/` is the
whole install.

```bash
ls ~/.hermes/plugins/delegation-policy/
# plugin.yaml  __init__.py  policy.md  README.md
```

Optionally verify the module's own contract checks before enabling:

```bash
"$(readlink -f /proc/$(systemctl --user show hermes-gateway.service -p MainPID --value)/exe)" \
    ~/.hermes/plugins/delegation-policy/__init__.py
# -> ok
```

## Editing __init__.py vs policy.md

`policy.md` is read on every new session, so editing it needs nothing more than
a fresh session. `__init__.py` is not: a running process keeps the module it
loaded, so after editing the code the plugin must be reloaded before the change
is live anywhere that process serves:

```bash
hermes plugins disable delegation-policy && hermes plugins enable delegation-policy
```

Skip that and the symptom is subtle: new sessions still render the policy (the
file is re-read) but with the previous code's behaviour - measured once as a
section that printed `unset` for two roles because the loaded build read config
with PyYAML, which is not importable in the render process.

## Enabling / disabling

Native plugin management - no config hand-editing:

```bash
hermes plugins enable delegation-policy     # enabled + gateway plugin reload
hermes plugins disable delegation-policy    # removed + gateway plugin reload
```

- Enabling takes effect in the NEXT session prompt (the CLI says so:
  "deferred: system prompt (next session)"). Sessions already running keep
  the prompt they started with; on resume, sections are restored from the
  persisted prompt bytes.
- No Hermes restart is required for enable/disable on this build; the
  gateway reloads plugins in place.

## Policy customization

Edit `policy.md` and start a new session - that is the whole procedure.
The file is re-read on every NEW session render, so edits do not need a
gateway restart, only a fresh session. Limits enforced by the plugin and by
Hermes core:

- Max 4000 chars after strip AND after the model names are substituted (Hermes'
  per-section ceiling; the plugin refuses to load with a visible error if the
  RENDERED text exceeds it, and the error names the length).
- Keep the six model tokens (`{{ORCH}}`, `{{ORCH_P}}`, `{{PLAN}}`, `{{PLAN_P}}`,
  `{{WORK}}`, `{{WORK_P}}`) in the file: the plugin refuses to load without them,
  so a model name can never quietly become prose again.
- Do not add the reserved marker text `hermes-plugin-section` anywhere in the
  policy - core would silently skip the section every session, so the plugin
  refuses to load instead, with the reason in the error.
- Keep it ASCII-only (house convention on this install).

## How injection works

`__init__.py` registers ONE system prompt section:

```python
ctx.register_system_prompt_section(
    "delegation-policy", _section, position="after_memory", max_chars=4000)
```

- The section renders ONCE per new session into the system prompt's volatile
  tier (after memory/user profile, before the profile and timestamp lines),
  framed as `## Plugin Context: delegation-policy` inside an HTML-comment
  container. It is then FROZEN on the agent for the whole session, persisted
  with the prompt verbatim, and restored from the persisted bytes on resume.
  The text is never re-injected; the hook (below) is what runs per tool call.
- Verification trail: each render logs one line, once per session -
  `grep "Session plugin prompt section" ~/.hermes/logs/agent.log`.
- Scoped delivery: the render callback checks the session's platform. Workers
  are excluded so a delegated child never receives orchestration instructions
  that conflict with its own brief. Cron runs are also excluded: they are
  bounded by a 3-minute hard interrupt, and a cron agent that starts
  delegating substantial work is killed mid-child. Edit `EXCLUDED_PLATFORMS`
  in `__init__.py` if you want scheduled runs to orchestrate too.
- Failure contract: if `policy.md` is missing/empty/oversized at plugin
  load, the load fails VISIBLY (`hermes plugins list` shows the error; Hermes
  itself starts normally). If the file becomes unreadable later, every new
  session renders a short notice line instead of the policy, so the failure
  is visible in the prompt itself, not only in logs.
- SOUL.md is untouched: the section is additive prose in its own container;
  identity slots of the prompt are not modified.

## Token overhead

Measured on the installed build (framing computed with Hermes' own
`format_system_prompt_sections`):

- Policy text: 3926 chars rendered with the current config (3902 bytes on disk
  as the template, measured 2026-11-25), 74 under the 4000-char hard ceiling.
  The rendered length moves by the difference in model-name lengths, and the
  plugin refuses to load (naming the length) if a longer name pushes it over.
- Total prompt bytes added: 3926 + 80 section frame + 74 container markers,
  ONCE per session, not per turn.
- Roughly 1000 tokens per session at ~4 chars/token; nearer 1200-1300 if the
  paths and flags in it tokenize tighter than prose -
  the fixed cost of the policy, paid once when the session's prompt is
  built and then covered by prompt caching.
- The gate adds nothing to the prompt. A block message (~4 lines) enters the
  transcript only on a blocked call, and the retried call replaces nothing.
- Aggregate budget: Hermes allows up to 8000 chars across all plugin
  sections content; this section is 3926 of it (4080 bytes on the wire).

## Troubleshooting

- A planning/review call blocked in a session that should be exempt: check
  `sessions.source` for that session id in `state.db`. Only `subagent`,
  `oneshot` and `cron` are exempt; a row written by some other surface is
  gated on purpose.
- The gate never blocks anything: `grep '"event": "block"'`
  `$HERMES_HOME/logs/delegation-policy-gate.jsonl`. No lines at all means the
  hook did not load (check `hermes plugins list` for an error) or every call
  was exempt.
- The gate blocks after a planner pass: the pass must be NEWER than the block
  (`evidence` in the `allow` line) and its session row must read
  `source='oneshot'` with a model starting with `delegation.roles.planner.model`
  (or the reviewer's). A `hermes chat` run on another model is not evidence -
  including a pass launched before you changed the setting.
- `error` lines in the ledger: the gate failed open. The reason is in the
  line; the turn was not blocked.

- "Not enabled" in `hermes plugins list` after enabling: run
  `hermes plugins list` again; if it shows an error, the load failed - the
  message names the reason (missing/empty/oversized policy.md, or a reserved
  marker inside it).
- Policy absent in a session: it only enters NEW sessions. Also check
  `EXCLUDED_PLATFORMS` - sessions on the `subagent` and `cron` platforms are
  excluded by design.
- A "[delegation-policy] policy.md failed to load" line in a session prompt:
  the file was unreadable at render time; the reason is in the logs. Fix the
  file, then start a new session.
- `hermes prompt-size` shows the section but a session does not: treat
  sessions as authoritative; `prompt-size` runs its own discovery and can
  render content live sessions never see.
- Verify end to end, like the tests that shipped this plugin:
  `hermes chat -q "Do not use tools. Does your system prompt contain
  '## Plugin Context: delegation-policy'? Quote the two lines after
  '### Objective'."` then read the reply in state.db.
