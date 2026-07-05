<#
  AegisOps task runner for Windows (PowerShell) — mirrors the Makefile targets for
  machines without GNU make.  Usage:  ./make.ps1 <target>
  Examples:  ./make.ps1 up   ./make.ps1 migrate   ./make.ps1 dev
#>
param(
  [Parameter(Position = 0)]
  [string]$Target = "help"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Invoke-Compose { param([string[]]$ComposeArgs) & docker compose @ComposeArgs }

switch ($Target.ToLower()) {
  "help" {
    Write-Host "AegisOps targets:" -ForegroundColor Cyan
    @(
      "up         Start backing services",
      "up-full    Start everything incl. containerized api + frontend",
      "down       Stop services (keep data)",
      "logs       Tail service logs",
      "ps         Show service status",
      "install    Install backend + frontend deps",
      "build      Build api + frontend images",
      "migrate    Run Alembic migrations",
      "seed       Load real initial data",
      "dev        Run backend + frontend together",
      "dev-api    Run backend only (uvicorn :8000)",
      "dev-web    Run frontend only (next :3000)",
      "test       Backend pytest (test container) + frontend vitest",
      "test-backend  Backend pytest only (containerized, real datastores)",
      "test-frontend Frontend vitest + RTL only",
      "lint       Lint backend (ruff) + frontend (eslint)",
      "fmt        Format/auto-fix backend",
      "e2e        Playwright E2E (app must be running)",
      "reset      Stop and DELETE all volumes (destructive)"
    ) | ForEach-Object { Write-Host ("  " + $_) }
  }
  "up"       { Invoke-Compose @("up", "-d"); Invoke-Compose @("ps") }
  "up-full"  { Invoke-Compose @("--profile", "full", "up", "-d", "--build"); Invoke-Compose @("ps") }
  "down"     { Invoke-Compose @("down") }
  "logs"     { Invoke-Compose @("logs", "-f") }
  "ps"       { Invoke-Compose @("ps") }
  "install"  {
    Push-Location backend; pip install -e ".[dev]"; Pop-Location
    Push-Location frontend; npm install; Pop-Location
  }
  "build"    { Invoke-Compose @("--profile", "full", "build") }
  "migrate"  { Push-Location backend; alembic upgrade head; python -m app.graph_db.schema; Pop-Location }
  "seed"     { Push-Location backend; python -m seed.seed; Pop-Location }
  "dev" {
    Write-Host "Starting API (:8000) and frontend (:3000)…" -ForegroundColor Cyan
    $api = Start-Process -FilePath "uvicorn" -ArgumentList "app.main:app", "--reload", "--host", "0.0.0.0", "--port", "8000" -WorkingDirectory (Join-Path $Root "backend") -NoNewWindow -PassThru
    $web = Start-Process -FilePath "npm" -ArgumentList "run", "dev" -WorkingDirectory (Join-Path $Root "frontend") -NoNewWindow -PassThru
    Write-Host "API pid $($api.Id) · web pid $($web.Id). Ctrl-C to stop." -ForegroundColor Green
    Wait-Process -Id $api.Id, $web.Id
  }
  "dev-api"  { Push-Location backend; uvicorn app.main:app --reload --host 0.0.0.0 --port 8000; Pop-Location }
  "dev-web"  { Push-Location frontend; npm run dev; Pop-Location }
  "test" {
    # Backend tests run INSIDE the containerized environment (real PG/Redis/Neo4j), because the
    # slim prod image has no pytest — the `api-test` service adds the test tools at runtime.
    Invoke-Compose @("--profile", "test", "run", "--rm", "api-test")
    Push-Location frontend; npm test; Pop-Location
  }
  "test-backend"  { Invoke-Compose @("--profile", "test", "run", "--rm", "api-test") }
  "test-frontend" { Push-Location frontend; npm test; Pop-Location }
  "lint" {
    Push-Location backend; ruff check .; Pop-Location
    Push-Location frontend; npm run lint; Pop-Location
  }
  "fmt"      { Push-Location backend; ruff format .; ruff check --fix .; Pop-Location }
  "e2e"      { Push-Location frontend; npx playwright test; Pop-Location }
  "reset" {
    Invoke-Compose @("down", "-v")
    Write-Host "All AegisOps volumes removed." -ForegroundColor Yellow
  }
  default {
    Write-Host "Unknown target '$Target'. Run ./make.ps1 help" -ForegroundColor Red
    exit 1
  }
}
