"""GW: push a parked run to the channels of the people who can actually approve it.

Called from `api.chat.build_drive` whenever a run reaches `awaiting_approval`, regardless of
which gateway started it — so a change begun in the browser can be approved from a phone, and
one begun on a phone is equally approvable in the browser.

Who receives a card is a governance decision, not a convenience:

* **linked** — an unlinked user has no channel identity to push to;
* **eligible** — `can_approve` per the bound user's roles (`identity.notifiable_approvers`);
* **org-scoped** — the run's org only (S0);
* **four-eyes aware** — when the flag is on and the run targets Production, the initiator is
  excluded, because they cannot approve it and a card they cannot action is noise that invites
  a confusing refusal.

The card carries the change's SHAPE (workflow, mode, `+a ~c -d`, step count, failing-policy
count) and a web deep link — never the diff, inputs or outputs. A chat transcript is a poor
place for a plan, and the audit record of "what did the approver see before deciding" belongs
in the web UI.

Every function here is best-effort by contract: a push failure must never affect the run.
"""

from __future__ import annotations

import structlog

from ..settings import Settings, get_settings
from . import identity, render
from .transport import Button, TransportError

log = structlog.get_logger(__name__)


def approval_buttons(run_id: str) -> list[Button]:
    """Inline Approve/Reject for a parked run.

    The token is opaque and UNTRUSTED. Pressing it proves only that someone with access to that
    chat pressed a button — so `driver.handle_callback` re-resolves the sender's identity and
    re-runs the full server-side decision path (RBAC, org scope, four-eyes, awaiting-approval
    state, the in-flight lock). Nothing is authorized by the token itself.
    """
    return [Button(label="✅ Approve", token=f"apv:{run_id}:approved"),
            Button(label="🚫 Reject", token=f"apv:{run_id}:rejected")]


def _transport_for(channel: str, settings: Settings):
    """The live transport for a channel, or None when that gateway isn't running."""
    if channel == identity.TELEGRAM:
        from .telegram import poller
        gw = poller.current()
        return gw.client if gw is not None else None
    return None


async def approval_pending(*, run_id: str, org_id: str, env: str | None,
                           initiator_user_id: str | None, initiator_username: str | None,
                           interrupt_payload: dict) -> int:
    """Push the approval card to every linked, eligible approver. Returns how many were notified.

    Never raises: an unreachable channel, an unlinked org or a disabled gateway all mean "0
    pushed", and the run continues to wait at the gate exactly as it would have.
    """
    settings = get_settings()
    transport = _transport_for(identity.TELEGRAM, settings)
    if transport is None:
        return 0

    # A5: exclude the initiator only when four-eyes actually forbids them approving this run.
    exclude = None
    if settings.aegisops_four_eyes_for_production and (env or "").lower() == "production":
        exclude = initiator_user_id

    try:
        targets = await identity.notifiable_approvers(org_id, channel=identity.TELEGRAM,
                                                     exclude_user_id=exclude)
    except Exception as exc:  # noqa: BLE001
        log.warning("gateway.notify_targets_failed", run_id=run_id, error=str(exc))
        return 0
    if not targets:
        return 0

    card = render.approval_card(interrupt_payload, settings=settings, run_id=run_id,
                               initiator=initiator_username)
    text = render.outbound(card, limit=transport.max_text_len,
                           deep_link=render.web_run_link(settings, run_id, tab="terraform"))
    pushed = 0
    for target in targets:
        try:
            await transport.send(target.channel_chat_id, text,
                                 buttons=approval_buttons(run_id))
            pushed += 1
        except TransportError as exc:
            # A blocked bot / deleted chat for ONE approver must not stop the others.
            log.warning("gateway.notify_send_failed", run_id=run_id,
                        approver=target.username, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            log.warning("gateway.notify_failed", run_id=run_id, approver=target.username,
                        error=str(exc))
    if pushed:
        log.info("gateway.approval_pushed", run_id=run_id, channel=identity.TELEGRAM,
                 approvers=pushed)
    return pushed
