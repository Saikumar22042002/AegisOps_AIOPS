# TF_FORCE_UNLOCK — clearing a stuck Terraform state lock (PR-2b)

A terraform stage killed at its timeout (PR-2b: SIGTERM → grace → SIGKILL on the process
group) can leave a state lock behind. The run is classified honestly ("terraform apply
exceeded 45m … the state may still hold a lock") and the reconciler/orphan sweep reconciles
the RUN; the state LOCK itself may need a manual clear before the next apply on that
resource.

## First: let the automation try

The reconciler re-drives a resumable run against its saved plan; terraform's own
stale-plan protection refuses an out-of-date apply. Wait one reconcile interval and check
whether the resource is usable again before intervening.

## Local backend (dev default)

A local state lock is a `.terraform.tfstate.lock.info` file in the per-resource workspace.
If no process holds it:

```bash
cd infra/terraform-workspaces/<module>
TF_WORKSPACE=<state_workspace> terraform force-unlock <LOCK_ID>
```

The `<LOCK_ID>` is printed in the lock error. Confirm no live apply is running for that
run first (`GET /api/runs/<id>` is terminal).

## Remote backend (A3: S3 + DynamoDB)

The lock is a DynamoDB item. Same command with the backend configured:

```bash
terraform force-unlock <LOCK_ID>
```

Only force-unlock when you have CONFIRMED the run is terminal (crashed/killed), never while
an apply might still be in flight — a wrongful unlock permits a concurrent apply on the
same state.
