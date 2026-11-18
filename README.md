# delegation-policy

A standalone Hermes plugin that keeps a delegation policy permanently in the
main agent's instructions, so the cheap orchestrator model (DeepSeek Flash)
routes work instead of doing it: planning and review always go to GLM
one-shot subagents, task execution to DeepSeek subagents.

The plugin does ONLY this. It is not an orchestration engine: Hermes' native
`delegate_task` already exists, and `delegation.model` / `delegation.provider`
in `config.yaml` already select the worker model. Nothing here touches model
routing, tools, or core code.

## Architecture

```
delegation-policy/
+-- plugin.yaml     manifest (name, version, description)
+-- __init__.py     register(ctx): one system-prompt-section registration
+-- policy.md       the policy text - the part you edit
+-- README.md       this file
```

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
  There is no per-turn hook, no repeated injection, nothing that runs during
  the conversation.
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

- Policy text: 2257 chars (2257 bytes on disk).
- Total prompt bytes added: 2257 + 80 section frame + 74 container markers,
  ONCE per session, not per turn.
- Roughly 450 tokens per session at ~4 chars/token for English prose -
  the fixed cost of the policy, paid once when the session's prompt is
  built and then covered by prompt caching.
- Aggregate budget: Hermes allows up to 8000 chars across all plugin
  sections; this plugin uses 1795 of it.

## Troubleshooting

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
