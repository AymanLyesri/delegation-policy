"""delegation-policy - a persistent orchestration policy for the main agent.

The policy text lives in policy.md next to this file. Delivery is one
ctx.register_system_prompt_section() call: the text is rendered ONCE per new
session into the volatile tier of the system prompt (after memory, before the
profile/timestamp lines), frozen on the agent for the whole session, persisted
with the prompt verbatim, and restored from the persisted bytes on resume.
No hook runs, no per-turn re-injection, nothing duplicated.

Sessions whose platform is a delegated worker or a bounded cron run are
excluded: a worker told to "delegate substantial work" conflicts with its own
brief, and a cron run that starts delegating is killed by the 3-minute hard
interrupt mid-child. The policy targets the interactive orchestrator agent.

Failure contract: if policy.md cannot be served at register() time the plugin
load fails visibly (hermes plugins list shows the error; the session starts
normally without the section). If the file becomes unreadable later, the
section renders a short notice line instead - the failure is visible in the
prompt itself, not only in the logs.

Model routing is NOT touched here: delegation.model / delegation.provider in
config.yaml stay the single owner of which model workers run on.
"""

from __future__ import annotations

import logging
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

# Rendered instead of the policy when policy.md is unreadable at render time.
_LOAD_FAILURE_NOTICE = (
    "[delegation-policy] policy.md failed to load; the reason is in the Hermes "
    "logs. Delegation policy is OFF until the file is fixed and a new session "
    "starts."
)


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
    logger.info(
        "delegation-policy: section '%s' registered (%d chars)", SECTION_ID, len(text)
    )


def _self_test() -> int:
    """Every branch, against the shipped policy and throwaway policy files."""
    import tempfile

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
    #    exactly one section with the right id and position.
    class _Ctx:
        def __init__(self) -> None:
            self.sections = []

        def register_system_prompt_section(
            self, id, content, *, position, max_chars
        ):
            self.sections.append((id, content, position, max_chars))

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

    for f in failures:
        print("FAIL", f)
    print("ok" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
