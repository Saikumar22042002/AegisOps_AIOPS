# AegisOps Operations Runbook

Local sandbox operations for the AegisOps production kit. All commands run from
`aegisops_production_kit/` (this directory). Three distinct flows — pick the right one:

| Situation | What to do | Rebuild images? | Touch volumes? |
|---|---|---|---|
| Sandbox credentials expired (~hourly) | **Flow A** — refresh creds, recreate app containers | **No** | No |
| Source code changed (backend/frontend) | **Flow B** — rebuild images, recreate app containers | **Yes** | No |
| Full teardown explicitly requested | **Flow C** — destructive cleanup | — | Only if explicitly told |

Never run `docker compose down -v`, `docker volume prune`, or `docker system prune`
as part of Flows A/B. The database (`pgdata`), Redis, Neo4j, Grafana, Prometheus and
Terraform state volumes must survive every routine refresh.

---

## Flow A — Credential refresh only (the ~hourly sandbox ritual)

Credentials live in exactly two places, both gitignored:

- `.env` — AWS (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`,
  `AWS_DEFAULT_REGION`), Azure (`AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID`,
  `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`), GCP project (`GOOGLE_CLOUD_PROJECT`),
  model key (`GEMINI_API_KEY` or another provider key).
- `infra/secrets/gcp-sa.json` — the GCP service-account key, mounted read-only into
  the api containers at `/secrets/gcp-sa.json`.

Gotchas learned the hard way:

- **AWS sandbox keys are STS session creds** (`ASIA…` prefix) — `AWS_SESSION_TOKEN`
  is mandatory, not optional. All three values rotate together.
- **`AZURE_CLIENT_ID` must be the App registration's Application (client) ID — a
  36-char GUID**, same shape as the tenant/subscription IDs. If you paste a 40-char
  secret-looking string there, auth fails with `AADSTS700016`.
- **`GOOGLE_CLOUD_PROJECT` must match the new SA's project** — sandbox projects
  change every session. Keep `GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp-sa.json`
  (container path) — never a host path.

### Steps

1. Replace the AWS and Azure values in `.env` (and the model key if it rotated).
2. Replace `infra/secrets/gcp-sa.json` with the new SA key; update
   `GOOGLE_CLOUD_PROJECT` in `.env` to the new project id.
3. Verify presence **without printing values** (step 1 of the command block below).
4. Recreate ONLY the app containers so the new env is loaded. `.env` is injected via
   `env_file` at container **creation**, so a restart is not enough and a rebuild is
   pointless — recreate is exactly right.
5. Run health/readiness checks.
6. Run the safe identity validations (read-only, identity output only, no secrets).

### Copy/paste block

```bash
cd "C:/Users/Sai kumar/Documents/AegisOps_AIOPS/aegisops_production_kit"

# 1) presence check — prints PRESENT/EMPTY, never values
for k in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_DEFAULT_REGION \
         AZURE_SUBSCRIPTION_ID AZURE_TENANT_ID AZURE_CLIENT_ID AZURE_CLIENT_SECRET \
         GOOGLE_CLOUD_PROJECT GEMINI_API_KEY; do
  v=$(grep -E "^${k}=" .env | head -1 | cut -d= -f2-)
  [ -n "$v" ] && echo "$k=PRESENT" || echo "$k=EMPTY"
done
ls -l infra/secrets/gcp-sa.json   # confirm the file timestamp is fresh

# 2) recreate app containers only (NO build, NO volume changes)
docker compose --profile full up -d --force-recreate api api-b frontend

# 3) health / readiness
curl -s -o /dev/null -w 'healthz %{http_code}\n' http://localhost:8000/healthz
curl -s http://localhost:8000/readyz
curl -s -o /dev/null -w 'frontend %{http_code}\n' http://localhost:3000

# 4) identity validation — read-only, identity output only
docker exec aegisops_production_kit-api-1 python -c "import boto3; i=boto3.client('sts', region_name='us-east-1').get_caller_identity(); print('AWS OK', i['Account'], i['Arn'])"
docker exec aegisops_production_kit-api-1 python -c "
import os
from azure.identity import ClientSecretCredential
c = ClientSecretCredential(os.environ['AZURE_TENANT_ID'], os.environ['AZURE_CLIENT_ID'], os.environ['AZURE_CLIENT_SECRET'])
c.get_token('https://management.azure.com/.default'); print('AZURE OK')"
docker exec aegisops_production_kit-api-1 python -c "
from google.oauth2 import service_account
from google.auth.transport.requests import Request
c = service_account.Credentials.from_service_account_file('/secrets/gcp-sa.json', scopes=['https://www.googleapis.com/auth/cloud-platform'])
c.refresh(Request()); print('GCP OK', c.service_account_email)"
```

If all four print OK, you are back in business. Total time: ~1 minute.

---

## Flow B — Source code changed (rebuild + recreate)

App containers run **baked image code** — there are no source bind-mounts
(removed by CLN-1 in `docker-compose.override.yml`). An image rebuild is the only
way running code changes.

```bash
cd "C:/Users/Sai kumar/Documents/AegisOps_AIOPS/aegisops_production_kit"

# rebuild only the two app images from current source
docker compose --profile full build api frontend

# recreate the three app containers (api-b reuses the api image)
docker compose --profile full up -d api api-b frontend

# verify the running backend actually carries the new code
docker exec aegisops_production_kit-api-1 sh -c \
  'ls /app/app/harness | wc -l; ls /app/app/engine | wc -l; ls /app/alembic/versions | tail -3'

# health as in Flow A
curl -s http://localhost:8000/readyz
```

Notes:
- Alembic migrations run against the persistent `pgdata` volume; a rebuild does not
  reset the DB. Check head: `docker exec aegisops_production_kit-postgres-1 psql -U aegisops -d aegisops -t -c "select version_num from alembic_version;"`
- Backing services (postgres, redis, neo4j, keycloak, langfuse, otel, prometheus,
  grafana) are pinned third-party images — never rebuild or recreate them in this flow.

---

## Flow C — Full destructive cleanup (explicit request ONLY)

Never run by default. This deletes runtime state. Data volumes are deleted **only**
with the `-v` flag — leave it off unless the explicit goal is to wipe the database.

```bash
# stop + remove all containers and the compose network (volumes SURVIVE):
docker compose --profile full down

# ⚠ DESTROYS pgdata, redisdata, neo4jdata, tfstate, … — only on explicit request:
# docker compose --profile full down -v
```

---

## Reference

- Ports: api `8000`, api-b `8001`, frontend `3000`, postgres `5433`, langfuse `3001`,
  grafana `3002` (see `docker-compose.yml` for the rest).
- Health: `GET /healthz` (liveness), `GET /readyz` (dependencies + P5 preflight).
- Secrets hygiene: `.env`, `frontend/.env.local`, `infra/secrets/gcp-sa.json` are
  gitignored — verify with `git check-ignore` after any tooling change; never
  `git add` them; never echo their values into a terminal or chat.
- Feature flags: `AEGISOPS_CAPABILITY_PACKS` (P4 packs), `AEGISOPS_CREDENTIAL_BROKER`
  (P5 broker) — both read from `.env` at container creation, so flag flips follow
  Flow A (recreate, no rebuild).
