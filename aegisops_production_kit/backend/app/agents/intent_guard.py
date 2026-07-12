"""Deterministic intent-safety guards (Phase 7 / BUG-01).

Regex-only — no LLM in the loop — so these protections hold even when classification misfires.

Root cause this closes (screenshots 19/20): a stale pending parameter-collection record in Redis
swallowed EVERY later message in its session — the router's continuation short-circuit never
re-classified — so a read-only question like "How many s3 buckets are running in aws?" was
treated as the next answer for a pending `destroy_vpc` collection and re-asked for a VPC name.

Two independent layers:

1. `message_shape()` / `is_new_request()` — decides whether a message is plausibly an ANSWER to
   a pending parameter request ("ok, ubuntu", "name web-01, t3.small, key my-key") or a NEW
   question/request ("how many…", "are any…", "create a VM in gcp") that must be re-classified.
   The router consults this before continuing a pending collection.

2. `guard_classification()` — post-LLM hard guard on every cloudops classification: a
   status/inventory question can never carry a side-effecting action, and `action=destroy` is
   only allowed when the user's own words contain an explicit destructive verb. A destroy plan
   is therefore structurally unreachable from a read-shaped message, whatever the LLM returns.
"""

from __future__ import annotations

import re

# Leading filler tokens stripped before shape detection ("Earlier, did I…" → "did i…").
_FILLERS = re.compile(
    r"^(?:(?:earlier|so|ok|okay|also|and|but|btw|hey|hi|hello|please|now|then|well|hmm|umm|oh|"
    r"right|thanks|thank\s+you)[\s,.!:;-]+)+",
    re.IGNORECASE,
)

# Polite-request modal prefix ("can you …", "could you please …", "please …").
_MODAL = re.compile(
    r"^(?:(?:can|could|would|will|shall|may)\s+(?:you|we|u)\s+(?:please\s+)?|please\s+)",
    re.IGNORECASE,
)

# Cancel/abandon words — always a new request (drop the pending collection).
_CANCEL = re.compile(r"^(?:cancel|abort|nevermind|never\s*mind|forget\s+(?:it|that|this))\b", re.IGNORECASE)

# Interrogative / status starters: the message ASKS about state, it never instructs a change.
_QUESTION_START = re.compile(
    r"^(?:how|what|whats|what's|which|where|when|who|whose|why|"
    r"is|are|am|was|were|do|does|did|have|has|had|any)\b",
    re.IGNORECASE,
)

# Imperative read verbs — read-only even in imperative form ("list my vms", "show the vpc id").
_READ_VERB_START = re.compile(
    r"^(?:list|show|count|describe|check|tell|give|get|fetch|display|status)\b", re.IGNORECASE
)

# Explicit destructive verbs — REQUIRED (anywhere in the message) for a destroy action.
_DESTRUCTIVE = re.compile(
    r"\b(?:destroy|delete|remove|terminate|tear\s*down|teardown|deprovision|decommission|"
    r"wipe|drop|kill|undo|revert)\b",
    re.IGNORECASE,
)

# U7: "undo that" / "revert the last apply" — a gated destroy of the LAST resource applied in
# this conversation. Requires an anchor word so ordinary sentences never trigger it.
_UNDO = re.compile(
    r"\b(?:undo|revert)\b.{0,40}\b(?:that|it|this|last|previous|apply|applied|change|deployment)\b"
    r"|^\s*(?:undo|revert)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def is_undo(message: str) -> bool:
    """U7: does this message ask to undo/revert the last applied change?"""
    return bool(_UNDO.search(message or ""))

# Imperative action verbs that START a new actionable request mid-collection.
_ACTION_VERB_START = re.compile(
    r"^(?:create|provision|launch|deploy|build|make|set\s*up|setup|spin\s*up|add|open|attach|"
    r"enable|disable|configure|modify|update|change|resize|scale|stop|start|restart|reboot|"
    r"destroy|delete|remove|terminate|tear\s*down)\b",
    re.IGNORECASE,
)

# Resource nouns: an action verb ALONE ("create" — a valid key-pair answer) is not a new
# request; an action verb + one of these is.
_RESOURCE_NOUN = re.compile(
    r"\b(?:vms?|instances?|servers?|machines?|buckets?|s3|gcs|blobs?|storage|databases?|dbs?|"
    r"postgres(?:ql)?|mysql|sql|rds|cloud\s*sql|clusters?|k8s|kubernetes|eks|aks|gke|vpcs?|"
    r"networks?|subnets?|resource\s+groups?|rg|ec2|gce|ports?|firewalls?|security\s+groups?|"
    r"resources?)\b",
    re.IGNORECASE,
)

# Intent labels that imply a side effect (used to rewrite a mislabelled read).
_SIDE_EFFECT_INTENT = re.compile(
    r"^(?:destroy|delete|remove|terminate|teardown|create|provision|deploy|launch|modify|"
    r"update|scale)_",
    re.IGNORECASE,
)

# Broad inventory question ("did I create ANY resources…", "list all my resources").
_BROAD_INVENTORY = re.compile(
    r"(?:\b(?:any|all|every|what)\b[^.?!]*\bresources?\b)|(?:\bresources?\b[^.?!]*\b(?:created?|"
    r"provisioned|made|exist)\b)|\beverything\s+(?:i|we)\s+(?:created?|provisioned|made)\b",
    re.IGNORECASE,
)


def message_shape(message: str) -> str:
    """Classify a message's conversational shape: 'question' | 'request' | 'answer'.

    'question' → asks about state (must be read-only);
    'request'  → starts an actionable ask (re-classify, never continue a pending collection);
    'answer'   → plausibly bare values answering a pending parameter request.
    """
    text = _FILLERS.sub("", (message or "").strip())
    if not text:
        return "answer"
    if _CANCEL.match(text):
        return "request"
    bare = _MODAL.sub("", text)
    if _QUESTION_START.match(bare) or _READ_VERB_START.match(bare):
        return "question"
    if _ACTION_VERB_START.match(bare) and _RESOURCE_NOUN.search(bare):
        return "request"
    return "answer"


def is_question(message: str) -> bool:
    return message_shape(message) == "question"


def is_new_request(message: str) -> bool:
    """Mid parameter-collection: True when this message must be re-classified instead of being
    consumed as the pending request's next answer."""
    return message_shape(message) != "answer"


def explicitly_destructive(message: str) -> bool:
    """The user's own words contain a destructive verb (destroy/delete/remove/terminate/…)."""
    return bool(_DESTRUCTIVE.search(message or ""))


def is_broad_inventory_question(message: str) -> bool:
    """A question about everything created ("did I create any resources in aws or azure?")."""
    return is_question(message) and bool(_BROAD_INVENTORY.search(message or ""))


def guard_classification(message: str, cls: dict) -> dict | None:
    """Deterministic overrides for a cloudops classification. Returns None when no change is
    needed, else a dict of state overrides (may include `guard_note` for the timeline/log).

    Invariants enforced:
      • a question-shaped message always ends up `action=read` with a query_* intent;
      • `action=destroy` requires an explicit destructive verb in the message — otherwise the
        run is diverted to a clarification (and the action forced to read) so no destructive
        workflow is reachable.
    """
    if (cls.get("domain") or "").lower() != "cloudops":
        return None
    action = (cls.get("action") or "create").lower()
    intent = cls.get("intent") or ""
    resource = cls.get("resource") or "resources"

    if is_question(message):
        if action != "read" or _SIDE_EFFECT_INTENT.match(intent):
            safe_intent = intent if intent.lower().startswith(
                ("query_", "read_", "check_", "list_", "describe_")) else f"query_{resource}"
            return {
                "action": "read",
                "intent": safe_intent,
                "routing_reason": ((cls.get("routing_reason") or "").strip()
                                   + " [safety guard: status/inventory question → read-only]").strip(),
                "guard_note": f"read-only question (LLM said {intent or action})",
            }
        return None

    if action == "destroy" and not explicitly_destructive(message):
        return {
            "action": "read",
            "needs_clarification": True,
            "clarification": (
                "This was classified as a **destroy** operation, but your message doesn't "
                "explicitly ask to tear anything down, so I stopped — nothing was changed. "
                "If you do want to destroy it, say so explicitly, e.g. "
                "“destroy the VPC named prod-network”."
            ),
            "guard_note": f"destroy without an explicit destructive verb (LLM said {intent or 'destroy'})",
        }

    # Mirror guard (Phase 8 / N-08): an explicitly destructive message misclassified as CREATE
    # must never enter a provisioning flow ("destroy the VM" started provisioning in manual
    # testing). Redirect to the destroy flow when the message is unambiguous, never to create.
    # (Deliberately NOT applied to modify: "remove port 8002 from sai-test" is a legitimate
    # modify that contains a destructive verb.)
    if action == "create" and explicitly_destructive(message):
        return {
            "action": "destroy",
            "intent": intent if intent.lower().startswith(("destroy_", "delete_", "terminate_"))
            else f"destroy_{resource}",
            "routing_reason": ((cls.get("routing_reason") or "").strip()
                               + " [safety guard: destructive wording ⇒ destroy flow, never create]").strip(),
            "guard_note": f"destructive message classified as create (LLM said {intent or 'create'})",
        }
    return None
