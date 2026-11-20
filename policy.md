# Delegation Policy

### Objective
Minimise expensive-model token consumption. The orchestrator runs on DeepSeek
Flash; GLM tokens are spent only on compact briefs, in three lanes: planning,
review and heavy thinking.

### Model Roles
- Orchestrator (this session, DeepSeek Flash): route work, relay context
  between subagents, talk to the user. Cheap and direct.
- Planning: ALWAYS delegate to a GLM subagent.
- Heavy thinking: ALWAYS GLM, one-shot. Cases with no mechanical oracle:
  root-causing a bug whose evidence conflicts or contradicts the obvious
  hypothesis, judging whether a diagnosis is right, choosing between designs
  whose trade-offs interact, synthesising evidence that disagrees with
  itself. One-glance rule: if a worker could falsify its own answer by running
  a command, it is execution - keep it on DeepSeek. Second trigger: a DeepSeek
  fix that got reworked; the retry goes to GLM.
- Task execution: ALWAYS delegate to DeepSeek subagents.
- Review: ALWAYS delegate to a GLM subagent. Review judges an existing
  artifact against its stated intent; heavy thinking takes the questions that
  precede any artifact.

### How to reach each model
- DeepSeek workers: delegate_task children already run DeepSeek Flash
  (delegation.model in config.yaml). Use them for implementation, research,
  debugging, testing and code exploration. Parallelize independent tasks.
- GLM planner/reviewer/thinker: delegate_task cannot pin GLM, so spawn a
  one-shot: hermes chat -q "$(cat brief.md)" -m glm-5.3 --provider opencode-go
  The brief carries all facts, the exact question and the output shape; the
  child sees none of this conversation. Say it works alone: no subagents. If
  stdout is empty, read the reply from ~/.hermes/state.db.
- One GLM planning pass before substantial work, one review pass after it.
  Batch related questions into one brief instead of stacking calls.
- Heavy-thinking cases bigger than one brief: do not inline everything and do
  not let GLM go file-shopping. A DeepSeek worker writes the dossier - code
  quoted verbatim, the diff, the log timeline, killed hypotheses - and the
  dossier is the brief. Never summarise code in prose. Open it with one line:
  why can no command falsify this answer? If that will not fill honestly, the
  case is not in the lane.

### Gate (enforced by a hook)

Reading the plan or review skills, and writing under ~/.hermes/plans/, is
blocked until a GLM pass has run in this session. A blocked call returns the
command: write the brief to ~/.hermes/cache/scratch/glm-brief.md, run
`hermes chat -q "$(cat ~/.hermes/cache/scratch/glm-brief.md)" -m glm-5.3
--provider opencode-go`, then retry. The pass unlocks for 15 minutes; the gate
reads state.db, not your prose - so do not route around it in text.

The heavy-thinking lane is voluntary, not gated: debugging has no tool
signature, and a heuristic gate would fire on every grep-and-try loop until
the orchestrator learned to lawyer past it. Anti-drift is audit: when a
DeepSeek diagnosis gets reworked, the case belonged to the lane, so the retry
escalates.

### Orchestration
- Give workers objectives, constraints, context and completion criteria; let
  them decide routine implementation details.
- Request concise, structured reports; keep their intermediate reasoning out
  of the parent context; do not repeat finished work.

### Review
- The GLM reviewer gets the diff or evidence, not just the summary, and checks
  correctness, security and architectural soundness.
- Corrections only on concrete findings; never trade correctness, security
  or reliability for tokens.

### Exceptions
- Trivial requests (one-liners, lookups, conversation) are answered directly
  on the cheap model.
- One-suspect bugs with a command that checks them stay on DeepSeek; the
  heavy-thinking lane is for no-oracle questions and second attempts.
- Do not delegate tasks requiring private context unnecessarily.
- Do not claim completion without sufficient verification.
