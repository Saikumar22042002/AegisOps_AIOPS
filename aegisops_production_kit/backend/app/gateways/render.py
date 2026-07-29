"""GW: outbound composition — what is allowed to leave the platform over a chat channel.

Every string sent to a channel goes through `outbound()`. It applies, in order:

1. **Redaction** (`security.redaction.redact`) — the same masking the SSE console, the context
   graph, Langfuse and the message-persistence path use. A chat transcript lives forever on
   someone's phone and in a third-party datacentre; this is the last gate before it does.
2. **Withholding** — a High-confidentiality answer is NOT sent. The channel gets a short notice
   plus a web deep link. Credential reveal (S1) and step-up re-auth are unreachable from a
   gateway by construction: there is no gateway code path that calls them.
3. **Truncation** — long output is cut at the channel's limit with an explicit marker and a deep
   link to the full artifact, never silently clipped.

`how_to_link` is the ONLY thing an unbound sender ever receives: no run, no data, no
acknowledgement of whether the org or the bot's other users exist.
"""

from __future__ import annotations

from ..security.redaction import redact
from ..settings import Settings

#: Appended when an answer is withheld for confidentiality.
WITHHELD_NOTICE = (
    "🔒 This answer contains sensitive material, so I'm not sending it over chat. "
    "Open it in AegisOps:"
)

#: Appended when output is cut to fit the channel's message limit.
TRUNCATED_NOTICE = "…\n\n✂️ Truncated for chat. Full output:"

STREAM_CURSOR = "▌"


def web_run_link(settings: Settings, run_id: str, *, tab: str | None = None) -> str:
    base = f"{settings.web_base_url}/?run={run_id}"
    return f"{base}&tab={tab}" if tab else base


def web_session_link(settings: Settings, session_id: str) -> str:
    return f"{settings.web_base_url}/?session={session_id}"


def outbound(text: str, *, limit: int, level: str | None = None,
             deep_link: str | None = None) -> str:
    """Compose a channel-safe message. `level` is the confidentiality level of the content."""
    body = redact(text or "")
    if (level or "").lower() == "high":
        return f"{WITHHELD_NOTICE}\n{deep_link}" if deep_link else WITHHELD_NOTICE
    if not body.strip():
        return "(no output)"
    if len(body) <= limit:
        return body
    tail = f"{TRUNCATED_NOTICE}\n{deep_link}" if deep_link else TRUNCATED_NOTICE
    # Cut on a whitespace boundary when one is nearby, so we don't split a word or a token.
    budget = max(0, limit - len(tail) - 1)
    cut = body[:budget]
    space = cut.rfind("\n")
    if space < budget - 200:
        space = cut.rfind(" ")
    if space > budget * 0.5:
        cut = cut[:space]
    return f"{cut}{tail}"


def how_to_link(settings: Settings) -> str:
    """The complete reply to an unbound sender. Says how to link and nothing else.

    No org names, no usernames, no confirmation that any account exists — an unbound sender
    learns only that linking is the way in.
    """
    return (
        "👋 This is an **AegisOps** bot. I only answer linked accounts.\n\n"
        "To link your account:\n"
        f"1. Open AegisOps → **Settings** → **Connected accounts** ({settings.web_base_url})\n"
        "2. Under **Telegram**, press **Generate code**\n"
        "3. Send me `/link ABCD-EFGH` with that code (it expires in a few minutes)\n\n"
        "Your AegisOps permissions, organization and approval rules then apply here exactly as "
        "they do in the web app."
    )


def linked_greeting(username: str, org: str) -> str:
    return (
        f"✅ Linked to **{username}**"
        + (f" ({org})" if org else "")
        + ".\n\nSend me a request and it runs through the same agents, policies and approval "
          "gate as the web app. `/new` starts a fresh conversation · `/status` shows the link · "
          "`/help` lists commands."
    )


def help_text() -> str:
    return (
        "**AegisOps — Telegram**\n"
        "`/new` — start a fresh conversation\n"
        "`/status` — show which account this chat is linked to\n"
        "`/unlink` — cut the link from this chat\n"
        "`/help` — this message\n\n"
        "Anything else is a request: it runs through the same router, policy checks and "
        "human-approval gate as the web app. Infrastructure changes always ask for approval "
        "first, and sensitive output stays in the web UI."
    )


def refusal(detail: str) -> str:
    return f"🚫 {redact(detail)}"


def plan_summary_line(plan: dict | None) -> str:
    """`+3 ~1 -0` from a plan summary — a count, never the plan contents."""
    summary = ((plan or {}).get("summary") or {}) if isinstance(plan, dict) else {}
    add = summary.get("add", 0) or 0
    change = summary.get("change", 0) or 0
    destroy = summary.get("destroy", 0) or 0
    return f"+{add} ~{change} -{destroy}"


def approval_card(payload: dict, *, settings: Settings, run_id: str,
                  initiator: str | None = None) -> str:
    """The approval request as shown in a chat: identity of the change and its SHAPE only.

    Deliberately does not include the diff, inputs, outputs or policy detail — those are
    reviewable in the web UI, which is also where the audit trail of "what did the approver
    actually see" belongs. What a chat approver gets is enough to decide whether to open it.
    """
    plan = (payload or {}).get("plan") or {}
    workflow = (payload or {}).get("workflow") or "change"
    mode = (payload or {}).get("mode") or "apply"
    steps = plan.get("steps") if isinstance(plan, dict) else None
    checks = (payload or {}).get("policyChecks") or []
    failed = [c for c in checks
              if isinstance(c, dict) and c.get("evaluated") is not False and c.get("passed") is False]
    lines = [
        "🛡️ **Approval required**",
        f"**{workflow}** · mode `{mode}` · plan `{plan_summary_line(plan)}`",
    ]
    if isinstance(steps, list) and steps:
        lines.append(f"{len(steps)} step(s) in one governed plan")
    if (payload or {}).get("reason") == "deviation":
        lines.append("⚠️ This is a **deviation** from what was already approved.")
    if failed:
        lines.append(f"❗ {len(failed)} policy check(s) failing")
    if initiator:
        lines.append(f"Initiated by **{redact(initiator)}**")
    lines += [
        "",
        f"Review the full plan, diff and policy checks: {web_run_link(settings, run_id, tab='terraform')}",
    ]
    return "\n".join(lines)
