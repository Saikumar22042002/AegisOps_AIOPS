"""Resource-appropriate success cards (Phase 8 / N-06).

Every successful provision posts a concise, useful summary in the CONVERSATION — not just the
Logs tab (screenshot 3: S3 applied silently). Pure function: outputs+inputs in, markdown out.
Secrets never appear here — credentials are delivered via the one-time reveal (N-02)."""

from __future__ import annotations

_VM_TYPES = {"ec2", "vm", "gce", "instance"}
_BUCKET_TYPES = {"s3", "gcs", "storage"}
_DB_TYPES = {"rds", "postgres", "cloudsql"}
_K8S_TYPES = {"eks", "aks", "gke"}


def _line(label: str, value) -> str | None:
    return f"- **{label}:** `{value}`" if value not in (None, "", [], {}) else None


def success_card(resource_type: str, outputs: dict, inputs: dict | None = None) -> str | None:
    """Markdown summary for a successful apply, keyed by resource type. None only when there
    is genuinely nothing to say (no outputs at all)."""
    outputs = outputs or {}
    inputs = inputs or {}
    rt = (resource_type or "").lower()

    if rt in _VM_TYPES:
        host = outputs.get("public_dns") or outputs.get("public_ip")
        user = outputs.get("login_user") or "admin"
        port = outputs.get("admin_port")
        cidr = outputs.get("allowed_cidr") or inputs.get("allowed_cidr")
        windows = (outputs.get("os_kind") == "windows") or (inputs.get("os") == "windows-2022")
        lines = ["### ✅ Instance ready"]
        for entry in (_line("Instance", outputs.get("instance_id") or outputs.get("vm_id")),
                      _line("Public address", host), _line("Private IP", outputs.get("private_ip")),
                      _line("Login user", user)):
            if entry:
                lines.append(entry)
        if host and cidr:
            if windows:
                lines.append(f"- **Connect (RDP):** `mstsc /v:{host}` — port {port or 3389} is open "
                             f"to `{cidr}` only")
                lines.append("- **Password:** generated — reveal it once with the **Reveal credential** "
                             "button below (never logged).")
            else:
                lines.append(f"- **Connect:** `ssh -i <your-key.pem> {user}@{host}` — port {port or 22} "
                             f"is open to `{cidr}` only")
                if outputs.get("key_name"):
                    lines.append(f"- **Key pair:** `{outputs['key_name']}` — download the private key "
                                 "once with the **Reveal credential** button below (never logged).")
        elif host:
            lines.append("- **Remote access:** closed by request (`allowed_cidr = none`). Ask me to "
                         "“open SSH for <your-ip>” when you need in.")
        return "\n".join(lines)

    if rt in _BUCKET_TYPES:
        name = outputs.get("bucket_name") or inputs.get("bucket_name") or inputs.get("account_name")
        region = outputs.get("region") or inputs.get("region") or inputs.get("location")
        lines = ["### ✅ Bucket ready"]
        for entry in (_line("Name", name), _line("ARN", outputs.get("bucket_arn")),
                      _line("Region", region)):
            if entry:
                lines.append(entry)
        if name and rt == "s3":
            lines.append(f"- **Console:** https://s3.console.aws.amazon.com/s3/buckets/{name}")
            lines.append(f"- **Try it:** `aws s3 cp <file> s3://{name}/`")
        elif outputs.get("self_link"):
            lines.append(f"- **Console:** {outputs['self_link']}")
        return "\n".join(lines)

    if rt == "vpc":
        lines = ["### ✅ Network ready"]
        for entry in (_line("VPC", outputs.get("vpc_id")),
                      _line("CIDR", outputs.get("cidr_block") or inputs.get("cidr_block")),
                      _line("Public subnets", ", ".join(map(str, outputs.get("public_subnet_ids") or [])) or None),
                      _line("Private subnets", ", ".join(map(str, outputs.get("private_subnet_ids") or [])) or None)):
            if entry:
                lines.append(entry)
        return "\n".join(lines)

    if rt in _DB_TYPES:
        lines = ["### ✅ Database ready"]
        for entry in (_line("Endpoint", outputs.get("endpoint") or outputs.get("fqdn")),
                      _line("Engine", outputs.get("engine") or inputs.get("engine")
                            or inputs.get("database_version") or inputs.get("pg_version"))):
            if entry:
                lines.append(entry)
        lines.append("- **Credentials:** managed by the provider (Secrets Manager / generated admin "
                     "password) — never shown in chat; use the **Reveal credential** button if a "
                     "generated password exists.")
        return "\n".join(lines)

    if rt in _K8S_TYPES:
        lines = ["### ✅ Cluster ready"]
        for entry in (_line("Cluster", outputs.get("cluster_name") or inputs.get("name")),
                      _line("Endpoint", outputs.get("endpoint")),
                      _line("Version", outputs.get("version"))):
            if entry:
                lines.append(entry)
        return "\n".join(lines)

    # Generic: surface whatever real identifiers the module emitted.
    interesting = {k: v for k, v in outputs.items()
                   if v not in (None, "", [], {}) and k not in ("ingress_ports",)}
    if not interesting:
        return None
    lines = ["### ✅ Resource ready"]
    lines += [e for k, v in list(interesting.items())[:8] if (e := _line(k.replace("_", " "), v))]
    return "\n".join(lines)
