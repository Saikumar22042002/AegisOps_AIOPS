"""AegisOps admin CLI — operational commands that run outside the request path.

    python -m app.admin rebuild-world-model    # PR-5: rebuild Neo4j from Postgres inventory
    python -m app.admin retention-sweep         # PR-4: run one retention pass now

PR-5 uses this to PROVE Neo4j is a derived mirror: after a Postgres restore, the world
model is reconstructed from inventory alone — no cloud read, no Neo4j backup needed.
"""

from __future__ import annotations

import asyncio
import sys


async def _rebuild_world_model() -> int:
    from .graph_db import world_model
    out = await world_model.rebuild_from_inventory()
    print(f"world model rebuilt from inventory: {out['resources']} resources "
          f"across {out['orgs']} org(s)")
    return 0


async def _retention_sweep() -> int:
    from .agents.retention import sweep_retention
    out = await sweep_retention()
    print("retention sweep:", out)
    return 0


_COMMANDS = {
    "rebuild-world-model": _rebuild_world_model,
    "retention-sweep": _retention_sweep,
}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in _COMMANDS:
        print("usage: python -m app.admin <command>\ncommands: " + ", ".join(_COMMANDS),
              file=sys.stderr)
        return 2
    return asyncio.run(_COMMANDS[argv[0]]())


if __name__ == "__main__":
    raise SystemExit(main())
