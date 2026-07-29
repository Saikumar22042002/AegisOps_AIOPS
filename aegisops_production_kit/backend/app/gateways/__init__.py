"""GW: messaging gateways — channel-agnostic core + per-channel adapters.

A gateway only moves text. Everything that matters (routing, planning, policy, the approval
interrupt, Terraform) happens in the same graph the web UI drives, through the same
`POST /chat` driver semantics — there is no gateway-only code path to a mutation.

    app/gateways/
      identity.py    channel account  ⇄  platform user  (link codes, binding, audit)
      transport.py   the abstract send/edit/callback surface an adapter must implement
      render.py      outbound composition: redaction, withholding, truncation, deep links
      stream.py      progressive answer streaming over an edit-capable transport
      driver.py      inbound → the shared run driver (commands, RBAC, session, streaming)
      telegram/      the first adapter: Bot API client + update mapping + long-poll loop

Design rules that are not negotiable, and where they are enforced:

* **Identity is a binding, never an allowlist.** An unbound sender has no platform identity and
  gets only the how-to-link reply (`driver.handle_inbound`). We deliberately do NOT adopt
  waku's `TELEGRAM_ALLOWED_USER` pattern: a chat id is not an authenticated principal, and
  RBAC/tenancy/four-eyes cannot be evaluated against one.
* **No new mutation path.** `driver` calls `api.chat.prepare_run` / `build_drive` —
  the exact functions `POST /chat` calls — and approvals go through
  `api.chat.resolve_approval_core`, the exact function `POST /approvals/{run_id}` calls.
* **Nothing sensitive leaves over a chat channel.** `render.outbound` redacts every outbound
  message and withholds High-confidentiality answers behind a web deep link. Credential
  reveal and step-up re-auth are simply not reachable from a gateway.
"""
