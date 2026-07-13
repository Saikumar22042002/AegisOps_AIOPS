"""AegisOps admin CLI — operational commands that run outside the request path.

    python -m app.admin rebuild-world-model    # PR-5: rebuild Neo4j from Postgres inventory
    python -m app.admin retention-sweep         # PR-4: run one retention pass now
    python -m app.admin mark-unreachable <name> --reason "<why>" [--undo] [--org <org_id>]
                                                # inventory honesty: a row whose real resource
                                                # lives in an account we can no longer reach is
                                                # flipped active→unreachable (reversible)

PR-5 uses this to PROVE Neo4j is a derived mirror: after a Postgres restore, the world
model is reconstructed from inventory alone — no cloud read, no Neo4j backup needed.
"""

from __future__ import annotations

import asyncio
import json
import sys


async def _rebuild_world_model(args: list[str]) -> int:
    from .graph_db import world_model
    out = await world_model.rebuild_from_inventory()
    print(f"world model rebuilt from inventory: {out['resources']} resources "
          f"across {out['orgs']} org(s)")
    return 0


async def _retention_sweep(args: list[str]) -> int:
    from .agents.retention import sweep_retention
    out = await sweep_retention()
    print("retention sweep:", out)
    return 0


def _flag(args: list[str], name: str) -> str | None:
    """Value of `--name value`, or None."""
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


async def _first_org_id() -> str:
    from sqlalchemy import select

    from .db.models import Organization
    from .db.session import session_scope
    async with session_scope() as s:
        org = (await s.execute(select(Organization).limit(1))).scalars().first()
        if org is None:
            raise SystemExit("no organization exists")
        return str(org.id)


async def _mark_unreachable(args: list[str]) -> int:
    """Inventory honesty (live-acceptance follow-up): sandbox accounts rotate per cred set,
    stranding rows whose real resources we can no longer reach — they must never sit
    silently `active` (offered as DEP parents / day-2 targets, blocking their names).
    Explicit per-name, logged, reversible with --undo. Never a bulk sweep, never a guess."""
    positional, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in ("--reason", "--org"):
            skip = True
        elif not a.startswith("--"):
            positional.append(a)
    if not positional:
        print("usage: mark-unreachable <name> --reason \"<why>\" [--undo] [--org <org_id>]",
              file=sys.stderr)
        return 2
    name = positional[0]
    undo = "--undo" in args
    reason = _flag(args, "--reason") or ""
    if not undo and not reason:
        print("a non-empty --reason is required (it is recorded on the row)", file=sys.stderr)
        return 2
    from .agents.inventory import mark_unreachable
    org_id = _flag(args, "--org") or await _first_org_id()
    out = await mark_unreachable(org_id, name, reason, undo=undo)
    if out is None:
        print(f"no {'unreachable' if undo else 'active'} row named {name!r} in org {org_id}",
              file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0


_COMMANDS = {
    "rebuild-world-model": _rebuild_world_model,
    "retention-sweep": _retention_sweep,
    "mark-unreachable": _mark_unreachable,
}


def _bootstrap() -> None:
    """Initialise the engines the app lifespan normally provides. Found live (2026-07-14):
    the CLI never called init_engine, so any command that actually touched Postgres crashed
    ('Database engine not initialised') — retention-sweep had only survived because the
    dev defaults skip every category before the first query. Engine/driver creation is
    lazy; unused ones cost nothing."""
    from .db.session import init_engine
    from .graph_db.neo4j import init_neo4j
    from .settings import get_settings
    settings = get_settings()
    init_engine(settings)
    init_neo4j(settings)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in _COMMANDS:
        print("usage: python -m app.admin <command>\ncommands: " + ", ".join(_COMMANDS),
              file=sys.stderr)
        return 2
    _bootstrap()
    return asyncio.run(_COMMANDS[argv[0]](argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
