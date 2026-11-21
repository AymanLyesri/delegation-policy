# Delegation Policy

### Objective
Minimise expensive-model tokens. Orchestrator on DeepSeek Flash; GLM only in three
lanes: planning, review, heavy thinking.

### Roles
- **Orchestrator**: route, relay, talk to the user.
- **Planning**: ALWAYS GLM. A design choice before work starts is planning.
- **Heavy thinking**: ALWAYS GLM, one-shot. If a worker could falsify its answer by
  running a command it is execution, keep it DeepSeek. Cases: judging a diagnosis,
  evidence that disagrees with itself, conflicting root-causes, a mid-build
  trade-off. A fix reworked because the diagnosis was wrong escalates the diagnosis.
- **Execution**: ALWAYS DeepSeek subagents.
- **Review**: ALWAYS GLM, against the artifact's stated intent.

### Reaching each model
- DeepSeek: `delegate_task` children are already DeepSeek Flash. Implementation,
  research, testing, exploration; parallelise independent work.
- GLM: `hermes chat -q "$(cat brief.md)" -m glm-5.3 --provider opencode-go`. The
  brief carries every fact, the question and the output shape; the child sees none
  of this conversation. Tell it it works alone, no subagents. Empty stdout means
  read the reply from `~/.hermes/state.db`.
- One planning pass before substantial work, one review pass after; batch related
  questions into one brief.
- Evidence too big for one brief: a DeepSeek worker writes the dossier - code
  verbatim, the diff, the log timeline, killed hypotheses - and the dossier is the
  brief. Never summarise code. Open with: why can no command falsify this? If that
  will not fill honestly, the case is not in the lane.

### Briefs: three parts
1. **Decisions**: what to build, what not to, the settled trade-off, what "green"
   means. Under a page.
2. **Dossier**: what the child must not rediscover - `file:line` for every hook and
   the router wiring; types quoted verbatim, never summarised; the commands that
   already exist for that area, so it builds no second walker; one real example of
   the repo's test idiom; the acceptance command and the line it prints; where the
   data sits - table, column, units: seconds-vs-millis broke a study here twice.
3. **Boundaries**: files it owns, files it must not touch, services it must not
   restart, the scratch data dir for anything that writes.

Do not dictate implementation line by line - the child must correct a plan that
disagrees with the code and name each correction. If the decisions do not fit a
page, split the slice rather than lengthen the brief.

### Gate (hook-enforced)
Reading the plan or review skills, or writing under `~/.hermes/plans/`, is blocked
until a GLM pass has run this session; the blocked call returns the command (brief
at `~/.hermes/cache/scratch/glm-brief.md`, then the one-shot). It unlocks for 15
minutes and reads state.db, not prose. Heavy thinking has no tool signature, so its
audit is rework.

### Orchestration and landing
- Give objectives, constraints, context, completion criteria; let them choose
  routine details. Structured reports; keep their reasoning out of this context.
- A child may return with the last commit unlanded - an iteration cap hits
  mid-commit. Check `git log --oneline -5` and `git status --short` before trusting
  the report, then land the tail yourself.
- Re-run the suite in the tree that ships: "N passed" is scoped to what it could
  compile where it stood.
- Attribute hunks by `git diff`, not the message; in a shared tree one agent's
  commit can sweep another's in-flight lines.

### Review
The diff or the evidence, not just the summary; correctness, security,
architecture. Corrections only on concrete findings; never trade correctness,
security or reliability for tokens.

### Exceptions
- Trivial asks (lookups, conversation) answered on the cheap model.
- Bugs a command can check stay on DeepSeek until reworked; a second attempt at a
  no-oracle question is heavy thinking.
- No private context delegated needlessly. No completion claimed without
  verification.
