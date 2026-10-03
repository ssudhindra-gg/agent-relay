# Agent Relay

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. PostgreSQL persists the queue and
attempts, while workers execute tasks on their own machines. The included
worker deterministically returns `input.upper()`.

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

## Storage and delivery behavior

`database.py` contains the PostgreSQL engine, SQLAlchemy models, and lease
recovery. `storage.py` contains task/claim/terminal operations; routes and
request models are kept in `main.py` and `schemas.py`. Each operation is one
PostgreSQL transaction. Claims and recovery select work with
`FOR UPDATE SKIP LOCKED`, so concurrent workers (in any number of API
processes) never receive the same task and never queue up behind each other.
Heartbeats and terminal submissions lock the one task they name; every writer
locks the task row before its attempts, which keeps lock order consistent.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
uv run pytest -q
```

Tests need Docker. `conftest.py` starts a disposable PostgreSQL container for
the session and runs every test against freshly created databases on it, so
they never touch your dev database or a running Compose stack (they ignore
`RELAY_DATABASE_URL`). To use an existing server instead, set
`RELAY_TEST_POSTGRES_URL` to an admin URL such as
`postgresql://postgres:secret@localhost:5432/postgres`; tests create and drop
their own databases there.

`test_integration_http.py` runs acceptance scenario 1 over real HTTP twice:
against a uvicorn subprocess and against the full `docker-compose.yaml` stack
(rebuilt from the working tree under a throwaway project name and random
ports), then checks the persisted rows directly in PostgreSQL.

This project intentionally does not include Kubernetes, CI, external brokers,
or an LLM. Those are deployment concerns rather than part of the relay
protocol.
