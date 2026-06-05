# Deployment — Korral StoreLink MCP in Isolated GCP

How Korral IT runs the Duvo StoreLink MCP server inside Korral's **private GCP project**. No public endpoints. No inbound TCP. The Duvo agent worker **spawns the container as a child process** and talks to it over **stdin/stdout** (MCP JSON-RPC).

---

## Mental model

```
┌─────────────────────┐   stdin/stdout (JSON-RPC)   ┌──────────────────────┐
│ Duvo agent worker   │ ◄──────────────────────────► │ duvo-korral-mcp       │
│ (GKE / Cloud Run    │   stdout = protocol ONLY    │ (this server)         │
│  job, private VPC)  │   stderr = logs + audit     │                       │
└─────────────────────┘                              └──────────┬───────────┘
                                                                  │ internal
                                                                  ▼
                                                       ┌──────────────────────┐
                                                       │ StoreLink API        │
                                                       │ (private network)    │
                                                       └──────────────────────┘
```

**Key constraint:** MCP is not an HTTP service. Korral IT does **not** expose port 443/8080 for this server. The parent worker owns the pipes.

---

## Three pillars

| Pillar | What Korral IT owns | This repo provides |
|--------|---------------------|-------------------|
| **1. Runtime** | Build image, run non-root, spawn with `-i` stdio | `Dockerfile`, `server.py` entrypoint |
| **2. Secrets** | Per-store API keys in Secret Manager, weekly rotation | `enforce_security_boundary()` contract |
| **3. Observability** | Ship stderr to Cloud Logging | `[FDE-DEBUG]` + `[BUYER AUDIT]` on stderr |

---

## Pillar 1 — Build and ship the container

### Build locally or in CI

```bash
docker build -t duvo-korral-mcp:latest .
```

Image properties (see `Dockerfile`):

- Base: `python:3.11-slim`
- User: `duvouser` / group `duvogroup` (non-root)
- `PYTHONUNBUFFERED=1` — logs flush immediately in Cloud Logging
- `ENTRYPOINT ["python", "server.py"]` — stdio MCP, **no `EXPOSE`**

### Push to Artifact Registry

```bash
# Example — adjust project / region to Korral's isolated GCP project
export PROJECT_ID=korral-duvo-pilot
export REGION=europe-west1
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/duvo/duvo-korral-mcp:latest"

docker tag duvo-korral-mcp:latest "$IMAGE"
docker push "$IMAGE"
```

### Smoke test (stdio)

```bash
docker run -i --rm duvo-korral-mcp:latest
# Worker sends JSON-RPC on stdin; server responds on stdout.
# Ctrl+C to exit.
```

Confirm non-root identity:

```bash
docker run --rm --entrypoint id duvo-korral-mcp:latest
# uid=999(duvouser) gid=999(duvogroup)
```

---

## Pillar 2 — Secrets and rotation

StoreLink uses **per-store** credentials (`X-Korral-Store-Key`), rotated weekly by Korral IT.

### Production layout (recommended)

Store one JSON document in **Secret Manager** (e.g. `storelink-store-keys`):

```json
{
  "47": { "current": "<rotated-key-47>" },
  "102": { "current": "<rotated-key-102>" }
}
```

Mount it read-only into the worker pod at runtime, e.g. `/var/secrets/store-keys.json`. The Duvo worker (or a thin bootstrap wrapper) reads keys and passes the correct `auth_key` on each MCP tool call — keys never need to be baked into the image.

### Failure modes (fail closed)

The server enforces auth **before** any StoreLink logic:

| Event | Server response | Operator action |
|-------|-----------------|-----------------|
| Unknown store | `CRITICAL_AUTH_FAILURE` | Register store in vault |
| Key rotated mid-flight | `API_KEY_ROTATED` | Reload secret mount, retry |
| Wrong token | `UNAUTHORIZED` | Check vault mapping |

**Pilot note:** `server.py` ships with an in-memory vault for the FDE demo (`key_store_47_valid`, `key_store_102_valid`). Replace with Secret Manager–backed lookup before production cutover.

### Rotation runbook

1. Korral IT writes new `current` key to Secret Manager.
2. Worker reloads the mounted secret (or pod restarts).
3. In-flight requests that hit stale keys receive `API_KEY_ROTATED`; the agent retries with the refreshed key.
4. No container rebuild required for rotation.

---

## Pillar 3 — Observability

All operational output goes to **stderr**. **stdout is reserved for MCP JSON-RPC** — never tee stderr into stdout in the worker.

| Stream | Content | Cloud Logging |
|--------|---------|---------------|
| **stdout** | MCP protocol only | Do not ingest (binary-safe pipe) |
| **stderr** | `[FDE-DEBUG]` technical traces | `severity`, `store_id`, message |
| **stderr** | `[BUYER AUDIT]` JSON | Buyer morning review / audit sink |

### GKE example (worker pod)

Configure the Duvo worker container to capture the MCP child stderr:

```yaml
# Pseudocode — worker spawns MCP as subprocess
containers:
  - name: duvo-worker
    image: REGION-docker.pkg.dev/PROJECT/duvo/worker:latest
    env:
      - name: MCP_IMAGE
        value: REGION-docker.pkg.dev/PROJECT/duvo/duvo-korral-mcp:latest
    # Worker runs: docker run -i MCP_IMAGE  (or equivalent container exec API)
```

Route pod logs to Cloud Logging via the standard GKE logging agent. Filter on:

- `[FDE-DEBUG]` — SRE dashboards, error alerting
- `[BUYER AUDIT]` — buyer audit index (JSON `AUDIT_LOG`, `STORE_ID`, `HUMAN_READABLE_SUMMARY`)

`PYTHONUNBUFFERED=1` in the image ensures lines appear in Cloud Logging without delay.

---

## How the Duvo worker runs the server

The worker is the **only** component that starts the MCP server:

1. Pull `duvo-korral-mcp:latest` from Artifact Registry (private VPC).
2. Start container with **interactive stdio**: `docker run -i` or Kubernetes equivalent (`stdin: true`, `tty: false`).
3. Forward MCP JSON-RPC from the agent session to container **stdin**.
4. Read MCP responses from container **stdout**.
5. Capture container **stderr** for Cloud Logging (do not mix into stdout).

There is no load balancer, ingress, or Cloud Run HTTP URL for this service.

### Network

- Worker and MCP container run in Korral's **isolated VPC**.
- Egress to StoreLink only via internal DNS (e.g. `storelink.internal`).
- No `0.0.0.0` listen socket in the MCP container.

---

## Go-live checklist

| Step | Owner | Done when |
|------|-------|-----------|
| Image built and pushed to private Artifact Registry | Korral IT | `docker pull` succeeds from VPC |
| Non-root user verified | Korral IT | `id` shows `duvouser` |
| Secret Manager keys mounted for stores 47, 102 (+ prod stores) | Korral IT | Tool calls authenticate |
| stderr → Cloud Logging pipeline live | Korral IT | `[FDE-DEBUG]` visible in console |
| Buyer audit index configured | Korral IT | `[BUYER AUDIT]` searchable |
| Harness passes in CI | FDE / Duvo | `python3 test_harness.py 2>&1` green |
| Worker stdio spawn tested end-to-end | Duvo | Agent completes butter SKU task |

---

## Local parity (before GCP)

Reproduce the same contract on a developer machine:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 test_harness.py 2>&1          # buyer task + security proof
python3 server.py                     # stdio MCP (Cursor / local worker)
docker build -t duvo-korral-mcp . && docker run -i --rm duvo-korral-mcp
```

Agent design decisions and tool surface are documented in `README.md`.
