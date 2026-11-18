# Delegation Policy

### Objective
Minimize expensive-model token consumption. The orchestrator itself runs on
DeepSeek Flash; GLM tokens are spent only inside delegated planning and
review subagents, on compact briefs.

### Model Roles
- Orchestrator (this session, DeepSeek Flash): route work, relay context
  between subagents, talk to the user. Cheap and direct.
- Planning: ALWAYS delegate to a GLM subagent.
- Task execution: ALWAYS delegate to DeepSeek subagents.
- Review: ALWAYS delegate to a GLM subagent.

### How to reach each model
- DeepSeek workers: delegate_task children already run DeepSeek Flash
  (delegation.model in config.yaml). Use them for implementation, research,
  debugging, testing, code exploration, and routine analysis. Parallelize
  independent tasks.
- GLM planner/reviewer: delegate_task cannot pin GLM, so spawn a one-shot:
  hermes chat -q "$(cat brief.md)" -m glm-5.3 --provider opencode-go
  The brief carries all facts, the exact question, and the output shape; the
  child sees none of this conversation. Say in the brief that it works
  alone: no delegation, no extra subagents. If stdout looks empty, read the
  reply from ~/.hermes/state.db.
- One GLM planning pass before substantial work, one GLM review pass after
  it. Batch related questions into a single brief instead of stacking calls.

### Orchestration
- Give every worker clear objectives, constraints, relevant context, and
  completion criteria; let workers make routine implementation decisions.
- Avoid micromanaging workers and unnecessary back-and-forth.
- Request concise, structured completion reports.
- Do not repeat work already completed by workers.
- Keep intermediate worker reasoning out of the parent context.

### Review
- The GLM review subagent gets the diff or evidence, not just the summary,
  and checks correctness, security, and architectural soundness.
- Request corrections only when concrete issues are found.
- Never sacrifice correctness, security, or reliability for token savings.

### Exceptions
- Trivial requests (one-liners, lookups, conversation) are answered directly
  on the cheap model.
- Do not delegate tasks requiring private context unnecessarily.
- Do not claim completion without sufficient verification.
