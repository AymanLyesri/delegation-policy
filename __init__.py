"""delegation-policy - a persistent orchestration policy for the main agent.

Two parts, both in this file:

1. POLICY TEXT. policy.md is rendered ONCE per new session into the volatile
   tier of the system prompt (after memory, before the profile/timestamp
   lines), frozen on the agent for the whole session, persisted with the
   prompt verbatim, and restored from the persisted bytes on resume. No
   per-turn re-injection.

2. THE GATE (pre_tool_call hook). Prose in a prompt was not enough: the
   orchestrator kept doing planning and review inline. The hook blocks the
   mechanical entry points for those two activities on the orchestrator
   session and hands back the exact command to spawn a GLM pass:

   - skill_view of a planning or review skill (plan, writing-plans,
     requesting-code-review), and read_file of that skill's SKILL.md
     (blocking only skill_view just reroutes through the documented
     "read the SKILL.md directly" fallback).
   - write_file / patch of an artifact under .hermes/plans/.

   Unlock: a session gets through only once a real GLM pass exists in
   state.db - a sessions row with source='oneshot', model starting 'glm',
   started_at newer than this session's last block and newer than
   UNLOCK_TTL_SECONDS. Evidence is the recorded run, never the command
   string, so an echo or a failed spawn does not unlock anything.

   Scope: any session whose sessions.source is not in EXEMPT_SOURCES.
   'subagent' (delegate_task workers) and 'oneshot' (the GLM pass itself,
   and hermes chat -q runs) and 'cron' are left alone. A missing row means
   fail open: the gate never breaks a turn.

   A hook cannot see prose. A plan written into the chat instead of a file
   is invisible to it; that limit is documented in README.md.

Failure contract: if policy.md cannot be served at register() time the plugin
load fails visibly (hermes plugins list shows the error; the session starts
normally without the section). If the file becomes unreadable later, the
section renders a short notice line instead - the failure is visible in the
prompt itself, not only in the logs.

Model routing is NOT touched here: delegation.model / delegation.provider in
config.yaml stay the single owner of which model workers run on.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
POLICY = ROOT / "policy.md"
SECTION_ID = "delegation-policy"

# Core's hard per-section ceiling (hermes_cli.plugins_dispatch.
# MAX_SYSTEM_PROMPT_SECTION_CHARS). Tracked at import so a future build that
# lowers it fails this plugin's load visibly instead of silently skipping the
# section on every session.
try:
    from hermes_cli.plugins_dispatch import MAX_SYSTEM_PROMPT_SECTION_CHARS as _CEILING
except Exception:  # older/isolated host: the documented v1 ceiling
    _CEILING = 4000

# Platforms that must NOT receive the orchestration policy:
# - "subagent": delegate_task children are the workers this policy governs.
# - "cron": scheduled runs are bounded by a 3-minute hard interrupt; a cron
#   agent that starts delegating substantial work is killed mid-child.
# Remove "cron" from this tuple if scheduled runs should orchestrate too.
EXCLUDED_PLATFORMS = ("subagent", "cron")

# --- gate configuration ---------------------------------------------------

# Skills whose use IS the planning or review act. Normalised by stripping a
# "superpowers:" / plugin namespace prefix. "brainstorming" is deliberately
# absent: it is interactive dialogue with the user, which a one-shot GLM
# child cannot hold. "receiving-code-review" is absent too: it is guidance
# for handling feedback already in hand, not a review pass.
GATED_SKILLS = ("plan", "writing-plans", "requesting-code-review")

# Artifact home of the `plan` skill. A write here without a GLM pass is the
# orchestrator planning inline, in a file.
PLANS_MARKER = ".hermes/plans/"

# sessions.source values this gate ignores: delegated workers, one-shot
# children (including the GLM pass itself), and bounded cron runs.
EXEMPT_SOURCES = frozenset({"subagent", "cron", "oneshot"})

# A GLM pass is evidence for this long; then the gate asks for a fresh one.
UNLOCK_TTL_SECONDS = 15 * 60

# Model family that counts as the planning/review pass.
UNLOCK_MODEL_PREFIX = "glm"

# Where the gate tells the orchestrator to write its brief. Must NOT be under
# PLANS_MARKER, or the agent gets blocked while writing the thing it was just
# told to write.
BRIEF_PATH = "~/.hermes/cache/scratch/glm-brief.md"

# Shell verbs that turn a mention of the plans directory into a write. Reads,
# listings and greps stay free; `echo x > plan.md`, tee, cp, mv, sed -i don't.
_PLAN_WRITE_RE = re.compile(r"(>>?|\btee\b|\bcp\b|\bmv\b|\bsed\s+-i\b|\brm\b)")

_ARM_LOCK = threading.Lock()
# The self-test drives the gate with stubbed lookups; it must not write fake
# block/allow rows into the real ledger that proof reads.
_GATE_EVENTS = True
# session_id -> wall clock of that session's most recent block (the anchor
# evidence must postdate). Fresh per process: after a restart every session
# arms again and has to produce a new GLM pass. Safe direction, no state file.
_ARMED: dict[tuple[str, str], float] = {}
# session_id -> source, cached; absent rows are never cached (a row can land
# a moment after the first tool call, and a cached '' would exempt forever).
_SOURCE_CACHE: dict[str, str] = {}


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else Path.home() / ".hermes"


def _state_db() -> Path:
    return _hermes_home() / "state.db"


def _query(sql: str, params: tuple = ()) -> list[tuple]:
    """Read-only query against the live session store; [] on any trouble."""
    db = _state_db()
    if not db.is_file():
        return []
    con = None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        return con.execute(sql, params).fetchall()
    finally:
        if con is not None:
            con.close()


def _session_source(session_id: str) -> str:
    """sessions.source for this session; '' when unknown (-> fail open)."""
    if not session_id:
        return ""
    if session_id in _SOURCE_CACHE:
        return _SOURCE_CACHE[session_id]
    try:
        rows = _query("select source from sessions where id = ?", (session_id,))
        source = str(rows[0][0] or "").strip().lower() if rows else ""
    except Exception as exc:
        logger.debug("delegation-policy: source lookup failed: %s", exc)
        return ""
    if source:
        # Never cache an absence: the row may land a moment after this call,
        # and a cached '' would exempt the session for the process lifetime.
        _SOURCE_CACHE[session_id] = source
    return source


def _glm_pass_after(anchor: float, now: float) -> float | None:
    """started_at of the newest GLM one-shot pass newer than anchor, or None.

    This is the unlock evidence: a recorded run, not a command string. A
    broken lookup returns ``now`` (fail open, logged as an error event): a
    gate that blocks because the session store hiccuped would spend the
    user's money on GLM passes that can never unlock it.
    """
    try:
        rows = _query(
            "select started_at from sessions "
            "where source = 'oneshot' and model like ? and started_at > ? "
            "order by started_at desc limit 1",
            (UNLOCK_MODEL_PREFIX + "%", anchor),
        )
        if not rows:
            return None
        started = float(rows[0][0])
    except Exception as exc:
        _gate_event("error", stage="evidence", error=str(exc)[:200])
        return now
    if started <= now - UNLOCK_TTL_SECONDS:
        return None
    return started


def _gate_event(name: str, **fields) -> None:
    """Append one line to the gate ledger; never raises."""
    if not _GATE_EVENTS:
        return
    try:
        path = _hermes_home() / "logs" / "delegation-policy-gate.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": round(time.time(), 3), "event": name, **fields}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=True) + "\n")
    except Exception:
        pass


def _norm_skill(name) -> str:
    text = str(name or "").strip().lower()
    return text.rsplit(":", 1)[-1].rsplit("/", 1)[-1]


def _gated_skill_path(path) -> bool:
    """True for an absolute/relative SKILL.md of a gated skill."""
    raw = str(path or "").strip()
    if not raw:
        return False
    try:
        resolved = Path(os.path.expanduser(raw)).as_posix()
    except Exception:
        resolved = raw.replace("\\", "/")
    for skill in GATED_SKILLS:
        if f"/skills/{skill}/SKILL.md" in resolved:
            return True
    return False


def _gated_action(tool_name: str, args: dict) -> str | None:
    """The label of the planning/review act this call would perform, if any."""
    args = args if isinstance(args, dict) else {}
    if tool_name == "skill_view":
        skill = _norm_skill(args.get("name"))
        if skill in GATED_SKILLS:
            return "Review" if "review" in skill else "Planning"
        return None
    if tool_name == "read_file":
        if _gated_skill_path(args.get("path")):
            return "Reading the review/planning skill"
        return None
    if tool_name in ("write_file", "patch"):
        path = str(args.get("path") or "")
        if PLANS_MARKER in path.replace("\\", "/"):
            return "Writing a plan artifact"
        return None
    if tool_name == "terminal":
        # The same write through a shell. Narrow on purpose: a command that
        # only reads or lists the plans directory stays free.
        command = str(args.get("command") or "")
        if PLANS_MARKER in command.replace("\\", "/") and _PLAN_WRITE_RE.search(command):
            return "Writing a plan artifact"
    return None


def _act_key(action: str) -> str:
    """The two acts buy their own GLM pass; they never share an unlock."""
    return "review" if "review" in action.lower() else "planning"


def _gate_message(action: str) -> str:
    """Prescribe the spawn; never say 'denied' and never list what is open."""
    job = "review pass" if "review" in action.lower() else "planning pass"
    return (
        f"DELEGATION GATE: {action.lower()} is a GLM {job}, not work for this model.\n"
        f"1) write the brief (facts, the exact question, the output shape) to {BRIEF_PATH}\n"
        f'2) hermes chat -q "$(cat {BRIEF_PATH})" -m glm-5.3 --provider opencode-go\n'
        "The GLM reply is that command's stdout. Retry this call once it returns."
    )


def _is_delegated_child() -> bool:
    try:
        from agent.delegation_context import is_delegated_child_context

        return bool(is_delegated_child_context())
    except Exception:
        return False


def _pre_tool_call(tool_name=None, args=None, session_id="", **_kwargs):
    """Veto planning/review tool calls on the orchestrator; fail open always."""
    sid = str(session_id or "")
    try:
        action = _gated_action(str(tool_name or ""), args or {})
        if action is None:
            return None
        if _is_delegated_child():
            return None
        source = _session_source(sid)
        if source == "" or source in EXEMPT_SOURCES:
            return None
        now = time.time()
        arm_key = (sid, _act_key(action))
        with _ARM_LOCK:
            anchor = _ARMED.get(arm_key, now)
        evidence = _glm_pass_after(anchor, now)
        if evidence is not None:
            _gate_event(
                "allow", session=sid, tool=str(tool_name), action=action,
                act=arm_key[1], evidence=round(evidence, 3),
            )
            return None
        with _ARM_LOCK:
            _ARMED[arm_key] = now
        _gate_event(
            "block", session=sid, tool=str(tool_name), action=action,
            act=arm_key[1], source=source,
        )
        return {"action": "block", "message": _gate_message(action)}
    except Exception as exc:  # a gate must never break a turn
        logger.debug("delegation-policy: gate error, allowing call: %s", exc)
        _gate_event("error", stage="gate", session=sid, error=str(exc)[:200])
        return None


def _policy_text() -> str:
    """Read and validate the policy; raise with a clear reason on failure."""
    text = POLICY.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("policy.md is empty")
    if "hermes-plugin-section" in text:
        # Reserved persistence markers make core silently skip the section on
        # every session; refuse at load time where the reason is visible.
        raise ValueError("policy.md contains a reserved hermes-plugin-section marker")
    if len(text) > _CEILING:
        raise ValueError(
            f"policy.md is {len(text)} chars, over the {_CEILING}-char "
            "system-prompt-section ceiling; trim it"
        )
    return text


def _section(info) -> str:
    """Render the policy for one new session; '' skips the section entirely."""
    platform = str((info or {}).get("platform") or "").lower()
    if platform in EXCLUDED_PLATFORMS:
        return ""
    try:
        return _policy_text()
    except Exception as exc:
        logger.warning("delegation-policy: policy.md unreadable at render: %s", exc)
        return _LOAD_FAILURE_NOTICE


def register(ctx) -> None:
    # Fail the plugin load (visibly, safely) when the policy cannot be served;
    # the loader records the error and the session starts without the section.
    text = _policy_text()
    ctx.register_system_prompt_section(
        SECTION_ID, _section, position="after_memory", max_chars=_CEILING
    )
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    logger.info(
        "delegation-policy: section '%s' registered (%d chars), gate armed on %d skills",
        SECTION_ID, len(text), len(GATED_SKILLS),
    )


# Rendered instead of the policy when policy.md is unreadable at render time.
_LOAD_FAILURE_NOTICE = (
    "[delegation-policy] policy.md failed to load; the reason is in the Hermes "
    "logs. Delegation policy is OFF until the file is fixed and a new session "
    "starts."
)


def _self_test() -> int:
    """Every branch, against the shipped policy and throwaway policy files."""
    import tempfile

    global _session_source, _glm_pass_after, _GATE_EVENTS

    failures: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # 1. The shipped policy is servable right now.
    text = _policy_text()
    check("policy is non-empty", bool(text), True)
    check("policy within ceiling", len(text) <= _CEILING, True)
    check("policy is ascii-only", text.isascii(), True)
    check("policy starts with its heading", text.startswith("# Delegation Policy"), True)
    check("policy names the gate", "gate" in text.lower(), True)

    # 2. Platform gate: workers and cron are excluded, main surfaces served.
    check("subagent excluded", _section({"platform": "subagent"}), "")
    check("cron excluded", _section({"platform": "cron"}), "")
    check("cli served", _section({"platform": "cli"}), text)
    check("telegram served", _section({"platform": "telegram"}), text)
    check("no platform info served", _section({}), text)
    check("parent_session id irrelevant", _section({"platform": "desktop"}), text)

    # 3. Render-time failures surface as the notice line, never as an raise.
    global POLICY
    with tempfile.TemporaryDirectory() as tmp:
        saved = POLICY
        try:
            POLICY = Path(tmp) / "missing.md"
            check(
                "missing file -> notice",
                _section({"platform": "cli"}),
                _LOAD_FAILURE_NOTICE,
            )
            empty = Path(tmp) / "empty.md"
            empty.write_text("   \n", encoding="utf-8")
            POLICY = empty
            check(
                "empty file -> notice",
                _section({"platform": "cli"}),
                _LOAD_FAILURE_NOTICE,
            )
            marker = Path(tmp) / "marker.md"
            marker.write_text(
                "<!-- hermes-plugin-sections:start -->", encoding="utf-8"
            )
            POLICY = marker
            check(
                "reserved marker -> notice",
                _section({"platform": "cli"}),
                _LOAD_FAILURE_NOTICE,
            )
            oversized = Path(tmp) / "oversized.md"
            oversized.write_text("x" * (_CEILING + 1), encoding="utf-8")
            POLICY = oversized
            check(
                "oversized file -> notice",
                _section({"platform": "cli"}),
                _LOAD_FAILURE_NOTICE,
            )
        finally:
            POLICY = saved

    # 4. register(): missing policy fails the load; present policy registers
    #    exactly one section with the right id and position, plus the hook.
    class _Ctx:
        def __init__(self) -> None:
            self.sections = []
            self.hooks = []

        def register_system_prompt_section(
            self, id, content, *, position, max_chars
        ):
            self.sections.append((id, content, position, max_chars))

        def register_hook(self, name, callback):
            self.hooks.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        saved = POLICY
        try:
            POLICY = Path(tmp) / "missing.md"
            try:
                register(_Ctx())
                failures.append("register with missing policy did not raise")
            except (OSError, ValueError):
                pass
        finally:
            POLICY = saved

    ctx = _Ctx()
    register(ctx)
    check("one section registered", len(ctx.sections), 1)
    if ctx.sections:
        sid, content, position, max_chars = ctx.sections[0]
        check("section id", sid, SECTION_ID)
        check("section position", position, "after_memory")
        check("section content is callable", callable(content), True)
        check("section max_chars matches ceiling", max_chars, _CEILING)
    check("hook registered", ctx.hooks, ["pre_tool_call"])

    # 5. Trigger classifier: what the gate sees and what it must not.
    check("plan skill gated", _gated_action("skill_view", {"name": "plan"}) is not None, True)
    check(
        "namespaced plan skill gated",
        _gated_action("skill_view", {"name": "superpowers:writing-plans"}) is not None,
        True,
    )
    check(
        "review skill gated",
        _gated_action("skill_view", {"name": "superpowers:requesting-code-review"})
        is not None,
        True,
    )
    check(
        "brainstorming not gated",
        _gated_action("skill_view", {"name": "superpowers:brainstorming"}),
        None,
    )
    check(
        "receiving-code-review not gated",
        _gated_action("skill_view", {"name": "superpowers:receiving-code-review"}),
        None,
    )
    check(
        "skill read_file gated",
        _gated_action("read_file", {"path": "/home/ayman/.hermes/skills/plan/SKILL.md"})
        is not None,
        True,
    )
    check(
        "superpowers plan SKILL.md gated",
        _gated_action(
            "read_file",
            {"path": "~/.hermes/plugins/superpowers/skills/writing-plans/SKILL.md"},
        )
        is not None,
        True,
    )
    check(
        "unrelated read_file free",
        _gated_action("read_file", {"path": "/home/ayman/notes.md"}),
        None,
    )
    check(
        "plans write gated",
        _gated_action("write_file", {"path": "/home/ayman/.hermes/plans/x.md"})
        is not None,
        True,
    )
    check(
        "patch of a plans file gated",
        _gated_action("patch", {"path": "~/.hermes/plans/x.md"}) is not None,
        True,
    )
    check(
        "normal code write free",
        _gated_action("write_file", {"path": "/home/ayman/.hermes/plugins/x.py"}),
        None,
    )
    check("terminal free", _gated_action("terminal", {"command": "ls"}), None)
    check(
        "shell write into plans gated",
        _gated_action(
            "terminal", {"command": "echo hi > ~/.hermes/plans/x.md"}
        )
        is not None,
        True,
    )
    check(
        "shell read of plans free",
        _gated_action("terminal", {"command": "cat ~/.hermes/plans/x.md"}),
        None,
    )
    check(
        "shell listing of plans free",
        _gated_action("terminal", {"command": "ls -la ~/.hermes/plans/"}),
        None,
    )

    # 6. Gate outcome: block / allow / exempt, with the lookups stubbed.
    saved_source, saved_evidence = _session_source, _glm_pass_after
    try:
        _ARMED.clear()
        _SOURCE_CACHE.clear()
        _GATE_EVENTS = False

        _session_source = lambda session_id: "local"
        _glm_pass_after = lambda anchor, now: None
        blocked = _pre_tool_call(
            tool_name="skill_view", args={"name": "plan"}, session_id="s1"
        )
        check("orchestrator blocked", isinstance(blocked, dict), True)
        check("block action", (blocked or {}).get("action"), "block")
        check(
            "block message prescribes the command",
            "-m glm-5.3 --provider opencode-go"
            in str((blocked or {}).get("message", "")),
            True,
        )
        check(
            "block message path is out of the plans dir",
            PLANS_MARKER not in str((blocked or {}).get("message", "")),
            True,
        )
        check("arm recorded", ("s1", "planning") in _ARMED, True)

        # Evidence after the block opens the gate for the same session. The
        # stub models the real query: one pass, consumed by the next attempt.
        evidence = {"pass": False}

        def _stub_evidence(anchor, now):
            if evidence["pass"]:
                evidence["pass"] = False
                return now
            return None

        _glm_pass_after = _stub_evidence
        evidence["pass"] = True
        check(
            "fresh glm pass allows",
            _pre_tool_call(tool_name="skill_view", args={"name": "plan"}, session_id="s1"),
            None,
        )
        # The review act buys its own pass: a planning unlock does not cover it.
        check(
            "review demands its own pass",
            isinstance(
                _pre_tool_call(
                    tool_name="skill_view",
                    args={"name": "superpowers:requesting-code-review"},
                    session_id="s1",
                ),
                dict,
            ),
            True,
        )
        evidence["pass"] = True
        check(
            "fresh glm pass allows review too",
            _pre_tool_call(
                tool_name="skill_view",
                args={"name": "superpowers:requesting-code-review"},
                session_id="s1",
            ),
            None,
        )
        # ... but a stale one does not (the TTL branch inside _glm_pass_after).
        check(
            "stale glm pass blocks again",
            isinstance(
                _pre_tool_call(
                    tool_name="write_file",
                    args={"path": "/home/ayman/.hermes/plans/x.md"},
                    session_id="s1",
                ),
                dict,
            ),
            True,
        )

        # Error paths: a broken session store fails OPEN and says so in the
        # ledger, under stage=evidence (real lookup) or stage=gate (anything
        # else in the verdict path).
        real_evidence = saved_evidence
        saved_events = _GATE_EVENTS
        _GATE_EVENTS = True
        import tempfile as _tf

        with _tf.TemporaryDirectory() as _tmp:
            saved_home = _hermes_home
            _ARMED.clear()
            globals()["_hermes_home"] = lambda: Path(_tmp)
            try:
                (Path(_tmp) / "state.db").write_text("not a database", encoding="utf-8")
                _glm_pass_after = real_evidence
                check(
                    "broken state.db fails open",
                    _pre_tool_call(
                        tool_name="skill_view", args={"name": "plan"}, session_id="s4"
                    ),
                    None,
                )

                def _boom(anchor, now):
                    raise RuntimeError("db gone")

                _glm_pass_after = _boom
                _ARMED.clear()
                check(
                    "raising lookup fails open",
                    _pre_tool_call(
                        tool_name="skill_view", args={"name": "plan"}, session_id="s5"
                    ),
                    None,
                )
                ledger = Path(_tmp) / "logs" / "delegation-policy-gate.jsonl"
                body = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
                check("evidence error logged", '"stage": "evidence"' in body, True)
                check("gate error logged", '"stage": "gate"' in body, True)
            finally:
                globals()["_hermes_home"] = saved_home
                _GATE_EVENTS = saved_events

        for source in ("subagent", "oneshot", "cron", ""):
            _session_source = lambda session_id, s=source: s
            check(
                f"source {source!r} exempt",
                _pre_tool_call(
                    tool_name="skill_view", args={"name": "plan"}, session_id="s2"
                ),
                None,
            )
        _session_source = lambda session_id: "local"
        check(
            "ungated tool passes",
            _pre_tool_call(
                tool_name="terminal", args={"command": "ls"}, session_id="s3"
            ),
            None,
        )
        check(
            "malformed args fail open",
            _pre_tool_call(tool_name="skill_view", args=None, session_id="s3"),
            None,
        )
    finally:
        _session_source, _glm_pass_after = saved_source, saved_evidence
        _GATE_EVENTS = True
        _ARMED.clear()
        _SOURCE_CACHE.clear()

    for f in failures:
        print("FAIL", f)
    print("ok" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
