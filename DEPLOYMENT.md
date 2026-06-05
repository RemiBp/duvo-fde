# Deployment — Korral StoreLink MCP

How Korral IT runs this server in **isolated GCP**. Runnable artifact: **`Dockerfile`** at repo root.

---

## Korral IT constraints (non-negotiable)

| Constraint | How we comply |
|------------|---------------|
| **StoreLink is not on the public internet** | MCP container runs in Korral VPC only. StoreLink egress via internal DNS (e.g. `storelink.internal`). No ingress, no `EXPOSE`, no public load balancer. |
| **No customer data leaves Korral GCP** | Image built and stored in **Korral Artifact Registry** (same tenancy). Logs → **Cloud Logging** in-project. Secrets in **Secret Manager** — never in the image or a public registry. |
| **Frequent post-go-live updates** | Tag every release; worker pulls `:latest` or a pinned digest from Korral registry. Rolling pod restart — no customer egress, no StoreLink exposure. |

---

## Architecture

```
Duvo worker (GKE, Korral VPC)
    │  stdin/stdout  MCP JSON-RPC
    ▼
duvo-korral-mcp container          private network only
    │  internal HTTP (future live mode)
    ▼
StoreLink
```

The worker **spawns** the container (`docker run -i` or K8s `stdin: true`). **stdout** = MCP protocol. **stderr** = `[FDE-DEBUG]` logs + `[BUYER AUDIT]` JSON → Cloud Logging.

---

## Build & deploy (Korral tenancy only)

```bash
# Build (local CI or Cloud Build inside Korral project)
docker build -t duvo-korral-mcp:latest .

# Push to Korral Artifact Registry — NOT Docker Hub
export PROJECT_ID=<korral-gcp-project>
export REGION=europe-west1
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/duvo/duvo-korral-mcp:latest"
docker tag duvo-korral-mcp:latest "$IMAGE"
docker push "$IMAGE"
```

**Image contract** (`Dockerfile`): `python:3.11-slim`, non-root `duvouser`/`duvogroup`, `PYTHONUNBUFFERED=1`, `ENTRYPOINT ["python", "server.py"]`.

**Smoke test:**

```bash
docker run --rm --entrypoint id duvo-korral-mcp:latest   # duvouser
docker run -i --rm duvo-korral-mcp:latest                # stdio MCP
```

---

## Secrets

Per-store keys live in **Secret Manager**, mounted read-only into the worker. The agent passes `auth_key` on each tool call. Weekly rotation → `API_KEY_ROTATED`; unknown store → `CRITICAL_AUTH_FAILURE`. No rebuild needed for key rotation.

---

## Release cadence (frequent updates)

1. Merge change → **Cloud Build** (or CI) in Korral project builds and pushes `:latest` + `:sha-<git>` tag.
2. Korral IT rolls worker pods (or restarts MCP sidecar) to pick up the new digest.
3. Run `python3 test_harness.py 2>&1` in CI before push; spot-check buyer task in staging VPC.

All artifacts and data stay inside Korral GCP. StoreLink remains private.

---

## Go-live checklist

- [ ] Image in Korral Artifact Registry (not public)
- [ ] Worker spawns MCP with stdio only
- [ ] Secret Manager keys mounted
- [ ] stderr → Cloud Logging
- [ ] Harness green; butter SKU task verified in VPC

Tool design: `README.md`.
