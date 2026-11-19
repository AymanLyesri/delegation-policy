# delegation-policy

A standalone Hermes plugin that keeps a delegation policy permanently in the
main agent's instructions, so the cheap orchestrator model (DeepSeek Flash)
routes work instead of doing it: planning and review always go to GLM
one-shot subagents, task execution to DeepSeek subagents.

Since v2.0.0 the policy is also ENFORCED by a `pre_tool_call` hook. Prose in
a prompt was not enough: the orchestrator kept reading the plan/review skills
and writing plans itself. See "## The gate" below.

The plugin does ONLY this. It is not an orchestration engine: Hermes' native
`delegate_task` already exists, and `delegation.model` / `delegation.provider`
in `config.yaml` already select the worker model. Nothing here touches model
routing, tools, or core code.

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

The block message names the exact command to run. Unlock rule: a row in
`$HERMES_HOME/state.db` with `sessions.source='oneshot'`, `model like 'glm%'`,
`started_at` AFTER this session's last block for the same act, and newer than
15 minutes. Evidence is the recorded run, never the command string, so `echo`
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
- Evidence is global, not per session: a GLM pass in another window counts.
- A gateway restart clears the anchors; every session then blocks once more
  (the safe direction).

Proof, in order, from a live session: `block` in the ledger for the local
session -> a `glm-5.3` oneshot row appears -> the retried call logs `allow`
with `evidence` equal to that row's `started_at` -> a review-skill call still
blocks -> no `error` lines anywhere between.

The intended model split (already configured in `config.yaml`, not by this
plugin):

- Main agent (DeepSeek Flash, `model.default`): routing, relaying context,
  user conversation.
- GLM planner/reviewer: one-shot spawns
  (`hermes chat -q ... -m glm-5.3 --provider opencode-go`) for the planning
  pass before substantial work and the review pass after it.
- Workers (DeepSeek Flash via `delegation.model`): implementation, research,
  debugging, testing, routine execution.

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

- Max 4000 chars after strip (Hermes' per-section ceiling; the plugin refuses
  to load with a visible error if you exceed it).
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

- Policy text: 2844 chars (2844 bytes on disk, measured 2026-11-19).
- Total prompt bytes added: 2844 + 80 section frame + 74 container markers,
  ONCE per session, not per turn.
- Roughly 710 tokens per session at ~4 chars/token for English prose -
  the fixed cost of the policy, paid once when the session's prompt is
  built and then covered by prompt caching.
- The gate adds nothing to the prompt. A block message (~4 lines) enters the
  transcript only on a blocked call, and the retried call replaces nothing.
- Aggregate budget: Hermes allows up to 8000 chars across all plugin
  sections; this plugin uses 1795 of it.

## Troubleshooting

- A planning/review call blocked in a session that should be exempt: check
  `sessions.source` for that session id in `state.db`. Only `subagent`,
  `oneshot` and `cron` are exempt; a row written by some other surface is
  gated on purpose.
- The gate never blocks anything: `grep '"event": "block"'`
  `$HERMES_HOME/logs/delegation-policy-gate.jsonl`. No lines at all means the
  hook did not load (check `hermes plugins list` for an error) or every call
  was exempt.
- The gate blocks after a GLM pass: the pass must be NEWER than the block
  (`evidence` in the `allow` line) and its session row must read
  `source='oneshot'`, `model like 'glm%'`. A `hermes chat` run on another
  model is not evidence.
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
