# syntax=docker/dockerfile:1
# AegisOps API image — FastAPI + LangGraph, with Terraform, Ansible, and kubectl on PATH.
# PR-7: the base image is pinned by DIGEST (not just the 3.11-slim tag) so a rebuild is
# reproducible and a moved tag can't slip a different base in. Rebuild cadence: refresh the
# digest monthly (or on a base CVE) — `docker manifest inspect python:3.11-slim` for the new
# digest, update here, rebuild, re-run the supply-chain CI job.
FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3 AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TERRAFORM_VERSION=1.9.8 \
    KUBECTL_VERSION=v1.31.3 \
    TFSEC_VERSION=v1.28.14

# ── OS packages + Terraform + kubectl + Ansible (all real CLIs the agents shell out to) ──
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        curl unzip git ca-certificates gnupg openssh-client \
        ansible \
        build-essential libpq-dev; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) tfarch=amd64; karch=amd64;; arm64) tfarch=arm64; karch=arm64;; *) echo "unsupported arch $arch"; exit 1;; esac; \
    curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${tfarch}.zip" -o /tmp/terraform.zip; \
    unzip /tmp/terraform.zip -d /usr/local/bin; rm /tmp/terraform.zip; \
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${karch}/kubectl" -o /usr/local/bin/kubectl; \
    chmod +x /usr/local/bin/kubectl; \
    terraform version; kubectl version --client=true; ansible --version; \
    apt-get purge -y --auto-remove build-essential; \
    rm -rf /var/lib/apt/lists/*

# ── Security scanners (MPP promotion gate is fail-closed without one; checkov is
#    preferred by the seam, tfsec is the fallback). checkov lives in its own venv so
#    its dependency tree can never conflict with the app's. ──
RUN set -eux; \
    python -m venv /opt/checkov; \
    /opt/checkov/bin/pip install --no-cache-dir "checkov==3.3.8"; \
    ln -s /opt/checkov/bin/checkov /usr/local/bin/checkov; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) tsarch=amd64;; arm64) tsarch=arm64;; *) echo "unsupported arch $arch"; exit 1;; esac; \
    curl -fsSL "https://github.com/aquasecurity/tfsec/releases/download/${TFSEC_VERSION}/tfsec-linux-${tsarch}" -o /usr/local/bin/tfsec; \
    chmod +x /usr/local/bin/tfsec; \
    checkov --version; tfsec --version

WORKDIR /app

# ── Python deps (layer-cached on pyproject changes) ──
COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel && pip install .

# ── App source ──
COPY . .

RUN useradd -m -u 10001 aegis && mkdir -p /app/.terraform-state && chown -R aegis:aegis /app
USER aegis

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=10 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
