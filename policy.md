# Delegation Policy

### Objective
Minimise expensive-model tokens: the orchestrator routes, the planner thinks. Model
names are settings (`delegation.roles`); read the line below, never memory.

### Roles
- **Orchestrator**: route, relay, talk to the user.
- **Planning and heavy thinking**: ALWAYS a planner pass, one-shot; a design choice
  before work starts is planning. If a worker could falsify the answer by running a
  command it is execution (judging evidence, a mid-build trade-off).
- **Execution**: ALWAYS the worker model, through `delegate_task`; parallelise
  independent work.
- **Review**: ALWAYS a reviewer pass, against the artifact's stated intent.

Models (settings, never constants): orchestrator {{ORCH}} / {{ORCH_P}}; planner and reviewer {{PLAN}} / {{PLAN_P}}; worker {{WORK}} / {{WORK_P}}.

### Reaching each model
- Planner: `hermes chat -q "$(cat brief.md)" -m {{PLAN}} --provider {{PLAN_P}}`; the
  brief carries the facts, the exact question, the output shape, and says it works
  alone. Empty stdout: read `~/.hermes/state.db`.
- One planning pass before substantial work, one review after; batch questions.
- Evidence too big for one brief: a worker writes the dossier (code verbatim, diff,
  log, killed hypotheses); that dossier is the brief.

### Briefs
1. **Decisions**: what to build, what not to, the trade-off, what green means.
2. **Dossier**: `file:line` for every hook; types quoted verbatim; commands that
   exist; the test idiom; the acceptance command and its output; table, column, units.
3. **Boundaries**: files it owns and must not touch, services it must not restart,
   the scratch dir for writes. Live data is READ-only, never copied.

Correct a plan that disagrees with the code, naming each correction; split the slice
if the decisions exceed a page.

### Brief integrity
A brief is a snapshot; the artefact is the truth. Mark a fact **measured at a commit**
or **recalled**: recalled ones go stale and the child cannot tell. Where they disagree
the thing wins; wrong facts are the parent's. A number must be re-runnable, or the
brief names its source.

### Ownership and depth
Stated ownership, never overlapping: two children in one file is a design error.
Depth only for independent slices; otherwise it buys latency and merge cost. The
parent lands: a child returns a branch and evidence, never a merge.

### Files a child never writes
Instruction files, this policy, personality and soul documents are the owner's. A
child that disagrees says so in its report; a silent blocked write is a failed report.

### Gate (hook-enforced)
Reading the plan or review skills, or writing under `~/.hermes/plans/`, is blocked
until a planner pass runs; the block returns that command. Unlocks 15 minutes, reads
state.db not prose.

### Orchestration
- Objectives, constraints, context, completion criteria; they choose details.
  Structured reports; their reasoning stays out of this context.
- **Completion is an artefact, not a claim**: no output from the acceptance command,
  no completion; "written" without a handle is unevidenced.
- **Time budget**: past twice it with nothing committed, a child reports the blocker.
- **One scarce resource, serialised**: name the bottleneck; children queue on it.
- Check `git log --oneline -5` and `git status --short` before trusting a report; an
  iteration cap lands mid-commit, so land the tail yourself.
- Re-run the suite in the tree that ships: "N passed" is scoped to what it could
  compile there.
- Attribute hunks by `git diff`, not the message.

### Review
The diff or the evidence, not the summary: correctness, security, architecture.
Corrections on concrete findings only.

### Exceptions
- Trivial asks stay on the cheap model.
- Bugs a command can check stay on workers; a second no-oracle attempt is heavy
  thinking.
- No private context delegated; no completion claimed without verification.
