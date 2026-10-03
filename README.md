# Agent Relay

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. PostgreSQL persists the queue and
attempts, while workers execute tasks on their own machines. The included
worker deterministically returns `input.upper()`.

The protocol is specified in [`SPEC.md`](SPEC.md). This repository runs it
three ways: Docker Compose, the API on the host, or Kubernetes on a local
[kind](https://kind.sigs.k8s.io/) cluster. A GitHub Actions workflow tests
every change and deploys it to kind.

## Requirements

- Docker (Compose, the test suite and kind all use it)
- [uv](https://docs.astral.sh/uv/) **0.12.22 or newer**. Older uv versions
  rewrite `uv.lock` in a format CI rejects, so run `uv self update` first.
- For Kubernetes: `kind` and `kubectl`

## Run it

The quickest way is Docker Compose, which runs the relay and its `postgres`
service together:

```bash
docker compose up -d --build
```

The API and dashboard are at <http://127.0.0.1:8000/>. Data lives in the
`postgres-data` volume, so it survives restarts; `docker compose down -v`
wipes it. Postgres is also published on `127.0.0.1:5432` (user, password and
database `relay`) for `psql`. See the header of `docker-compose.yaml` for the
overridable settings, and set `POSTGRES_PASSWORD` for anything shared.

To run the API on the host instead, start only the database and point the app
at it:

```bash
docker compose up -d postgres
uv sync
uv run uvicorn main:app --reload
```

`RELAY_DATABASE_URL` (or `DATABASE_URL`) selects the database and defaults to
`postgresql+psycopg://relay:relay@localhost:5432/relay`; plain
`postgresql://` URLs are accepted too. `GET /health` is a liveness check and
`GET /ready` verifies database connectivity and schema (it queries the real
tables, so a wiped volume reports not-ready instead of passing with zero
tables).

Register two identities and send a task:

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

The response contains each agent's secret `token` once. Keep it outside source
control. Use `Authorization: Bearer <token>` for all subsequent API calls;
registration is the only unauthenticated endpoint. For a shared installation,
set `RELAY_ENROLLMENT_SECRET` and send it as `X-Enrollment-Secret` when
registering.

## Dashboard

Open the relay's root URL (`/`), paste an agent token into **Agent token**,
and click **Use token**. The dashboard shows the agent directory and the tasks
that agent sent or received, using the same authorized API as any client;
it never shows other agents' tasks. The token is kept in `sessionStorage`
only, so it is forgotten when the tab closes (or on **Clear**).

Tokens are shown only once, at registration, and only their hashes are
stored, so a lost token cannot be recovered: register a new agent instead.

## Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a new
token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

An existing credential can also be supplied explicitly (the token is not
written to disk):

```bash
uv run python main.py worker --agent-id agent_123 --token agt_… --worker-id laptop-2
```

The worker is a pure HTTP client: it never connects to the database, so it
can run on any machine that can reach the relay.

## Deploy to Kubernetes (kind)

`k8s/` holds kustomize manifests for an `agent-relay` namespace:

- **`postgres`**: a StatefulSet with a 1 Gi PersistentVolumeClaim (the
  cluster's default StorageClass) and `pg_isready` readiness/liveness probes,
  behind a headless Service.
- **`relay`**: a 2-replica Deployment behind a ClusterIP Service on port 8000.
  Readiness uses `/ready`, so a replica that loses the database stops
  receiving traffic. Liveness uses `/health`, so a database outage does not
  restart every replica. An init container waits for Postgres. Several
  replicas are safe because claims coordinate through row locks.

```bash
kind create cluster

# Postgres password (gitignored); applied only when the volume is first created.
python -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_hex(24))" > k8s/postgres.env

docker build -t agent-relay:local .
kind load docker-image agent-relay:local   # kind cannot see local images otherwise
kubectl apply -k k8s/
kubectl -n agent-relay rollout status statefulset/postgres
kubectl -n agent-relay rollout status deployment/relay

kubectl -n agent-relay port-forward svc/relay 8080:8000   # then open http://127.0.0.1:8080/
```

The relay pods use `imagePullPolicy: Never`, so after rebuilding with the same
tag you must `kind load` again and `kubectl -n agent-relay rollout restart
deployment/relay`. The CI deploy job avoids this by giving every version its
own tag (see below). Changing `k8s/postgres.env` later does not change the
password of an existing database.

## Storage and delivery behavior

`database.py` contains the PostgreSQL engine, SQLAlchemy models, and lease
recovery. `storage.py` contains task/claim/terminal operations; routes and
request models are kept in `main.py` and `schemas.py`. Each operation is one
PostgreSQL transaction. Claims and recovery select work with
`FOR UPDATE SKIP LOCKED`, so concurrent workers (in any number of API
processes) never receive the same task and never queue up behind each other.
Heartbeats and terminal submissions lock the one task they name; every writer
locks the task row before its attempts, which keeps lock order consistent.
Advisory locks serialize idempotent task creation (per sender and key) and
schema creation, so several replicas can start against an empty database.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Tests

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
uv run pytest -v
```

`conftest.py` starts a disposable PostgreSQL container for the session and
runs every test against freshly created databases on it, so tests never touch
your dev database, a running Compose stack, or your cluster's `agent-relay`
namespace. They ignore `RELAY_DATABASE_URL` entirely.

`test_integration_http.py` runs SPEC acceptance scenario 1 (register two
agents, send, claim, complete, read the result) over real HTTP against three
targets, then checks the persisted rows directly in PostgreSQL, including that
tokens are stored only as hashes:

| Target | What runs | Isolation |
|---|---|---|
| `process` | `uvicorn` subprocess | its own database |
| `compose` | the full `docker-compose.yaml` stack, rebuilt from the working tree | throwaway project name, random ports, own volume |
| `kubernetes` | the `k8s/` manifests on a kind cluster | throwaway namespace, unique image tag, own password |

A target whose tools are missing is skipped (e.g. no kind cluster). Every
target cleans up after itself; the kind target removes only its own image tag
from the node.

| Variable | Purpose |
|---|---|
| `RELAY_TEST_POSTGRES_URL` | Use an existing PostgreSQL server (an admin URL such as `postgresql://postgres:secret@localhost:5432/postgres`) instead of starting a container |
| `RELAY_TEST_KIND_CLUSTER` | kind cluster for the `kubernetes` target (default `kind`; kubectl context `kind-<name>`) |
| `RELAY_TEST_DOCKER_HOST` | Address of Docker-published ports, default `127.0.0.1`. Set `host.docker.internal` when the tests themselves run in a container on Docker Desktop (as under act) |
| `RELAY_TEST_REQUIRE_TARGETS` | `1` turns a skipped target into a failure (set in CI) |

## CI

`.github/workflows/ci.yml` runs on pushes to `main`, on pull requests, and on
manual dispatch:

1. **test**: the whole test suite on PostgreSQL, with all three integration
   targets required.
2. **deploy** (only if **test** passes): builds an image tagged
   `agent-relay:<commit>-<hash of build inputs>`, so every code version gets a
   new tag. It then loads the image into kind, applies `k8s/` with that tag,
   waits for the rollouts, and runs `scripts/smoke.py`. The smoke test checks
   `/ready`, `/health`, that the served dashboard heading matches
   `dashboard.html`, and that unauthenticated API calls get `401`. It writes
   nothing. If an existing deployment is found, its database password is
   reused.

On GitHub-hosted runners each job creates a throwaway kind cluster
(`.github/actions/kind-access`). uv is pinned to 0.12.22 in the workflow and
the Dockerfile.

### Run CI locally with act

[act](https://nektosact.com/) runs the same workflow on your machine, and its
deploy job targets **your existing kind cluster** instead of creating one:

```bash
docker pull catthehacker/ubuntu:act-latest   # once; .actrc disables act's own pulls
act workflow_dispatch
```

`.actrc` selects the runner image and mounts the Docker socket into the job
containers. Through it the jobs build images, run the Compose target, and
reach the kind API. Notes:

- act copies your **working tree**, including uncommitted changes, and runs
  those. The image tag includes a hash of the build inputs, so local edits
  still get a new tag.
- Each deploy leaves its image tag on the kind node. To remove an old one:
  `docker exec kind-control-plane ctr -n k8s.io images rm docker.io/library/agent-relay:<tag>`.
- On Windows, act loses executable bits on the files of shell-script actions,
  so under act the workflow downloads pinned `kind`/`kubectl` binaries
  instead of using `helm/kind-action`.

## Project layout

| Path | Contents |
|---|---|
| `main.py`, `schemas.py`, `errors.py` | FastAPI routes, request models, error envelope |
| `database.py`, `storage.py` | PostgreSQL models, transactions, claim/recovery logic |
| `worker.py` | the deterministic uppercase worker (`python main.py worker`) |
| `dashboard.py`, `dashboard.html` | the token-based dashboard |
| `Dockerfile`, `docker-compose.yaml` | container image and the Compose stack |
| `k8s/` | kustomize manifests for Kubernetes |
| `conftest.py`, `test_*.py` | test suite and integration targets |
| `scripts/smoke.py` | read-only post-deploy smoke test |
| `.github/`, `.actrc` | CI workflow, kind access action, act configuration |
