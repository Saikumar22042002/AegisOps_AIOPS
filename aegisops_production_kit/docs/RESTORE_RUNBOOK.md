# RESTORE_RUNBOOK — AegisOps disaster recovery (PR-5)

**Postgres is the only store that must be backed up.** Everything else is rebuildable:

| Store | Role | Recovery |
|---|---|---|
| **PostgreSQL** | System of record — sessions, messages, runs, approvals, audit_log, inventory, memory | **Restore from `pg_dump` (this runbook).** |
| Redis | Ephemeral coordination — heartbeats, event channels, idempotency claims, cancel flags | Rebuildable; start empty. In-flight runs reconcile via the B3 sweep. |
| Neo4j | Derived mirror — world-model resource graph + DEPENDS_ON edges | Rebuild from inventory: `python -m app.admin rebuild-world-model` (no cloud read). |
| Terraform state | Per-resource state | Local: on the TF volume. Remote (A3): S3 bucket **versioning** — verify it is enabled in the backend-config docs. |

`audit_log` and `approvals` are **never** auto-deleted (PR-4 retention excludes them) — they are the compliance record and must be in every backup.

## Backup (scheduled)

`infra/backup/pg_backup.sh` runs a custom-format `pg_dump` and keeps the last `RETENTION` dumps. Schedule it (cron / systemd timer / k8s CronJob) on the DB host or a sidecar:

```bash
DATABASE_URL='postgresql://aegisops:...@postgres:5432/aegisops' \
  BACKUP_DIR=/var/backups/aegisops RETENTION=14 infra/backup/pg_backup.sh
```

## Restore drill (run this — an untested backup is not a backup)

1. **Stop the API/workers** (no writers during restore):
   `docker compose stop api api-b`
2. **Create a fresh, empty database** (or a throwaway target to rehearse):
   `createdb -h <host> -U <user> aegisops_restore`
3. **Restore the dump** (custom format → `pg_restore`):
   `pg_restore --dbname='postgresql://user:pass@host:5432/aegisops_restore' --no-owner --clean --if-exists /var/backups/aegisops/aegisops-<stamp>.dump`
4. **Point the app at the restored DB** (`DATABASE_URL`) and **boot**:
   `docker compose up -d api` → `GET /healthz` returns 200.
5. **Verify data integrity:**
   - sessions/runs/inventory rows are present (`GET /api/sessions`, the Infrastructure tab);
   - `audit_log` + `approvals` intact (spot-check counts against the pre-backup total).
6. **Rebuild Neo4j from the restored inventory** (proves the derived-mirror claim):
   `docker compose exec api python -m app.admin rebuild-world-model`
   → prints `world model rebuilt from inventory: N resources across M org(s)`.
7. **Day-2 on a pre-backup resource** (the real proof): send e.g. "add inbound port 8080 to \<an existing resource\>" → the plan resolves against the restored inventory + rebuilt world model, gated as normal.
8. **Redis** starts empty; any run that was mid-flight at backup time is reconciled to a terminal state by the B3 reconciler sweep on boot — confirm none are stuck `running`.

## TF state-lock recovery (ties to PR-2b)

A subprocess killed at its timeout (process-group SIGKILL) can leave a state lock. The reconciler/orphan sweep reconciles the run; to clear a stuck lock manually see `docs/TF_FORCE_UNLOCK.md`.

## Recorded drill evidence

Record each executed drill in `PROGRESS.md` (DEFERRED LIVE VERIFICATION → DLV-35): date, dump used, and the day-2 result. The drill is DEFERRED for live execution against real backups with the owner (it needs a Postgres instance + a restore target) — never marked done from a dry read.
