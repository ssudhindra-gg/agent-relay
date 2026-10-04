# Agent Relay architecture

This document explains what Agent Relay does and how it is built, mostly in
diagrams. The normative protocol is [`SPEC.md`](../SPEC.md); the diagrams
below describe the implementation in this repository.

- [What it does](#what-it-does)
- [Components](#components)
- [A task end to end](#a-task-end-to-end)
- [Task lifecycle](#task-lifecycle)
- [Leases, crashes and redelivery](#leases-crashes-and-redelivery)
- [Concurrent claims](#concurrent-claims)
- [Data model](#data-model)
- [Security model](#security-model)
- [Deployments](#deployments)
- [CI/CD pipeline](#cicd-pipeline)

## What it does

Agent Relay is a **mailbox for software agents**. An agent registers to get an
identity and an inbox. Other agents send tasks to that inbox. Whatever process
serves the recipient picks tasks up, does the work **on its own machine**, and
posts back a result for the sender to read. The relay stores and delivers
work; it never executes it.

```mermaid
flowchart LR
    S["🤖 Sender agent<br/>alice"]
    subgraph relay["Agent Relay"]
        direction TB
        Q[("📥 inbox of uppercase<br/>queued tasks")]
        RES[("📤 results")]
    end
    W["⚙️ Worker process<br/>serving uppercase"]
    S -- "1 · send 'hello'" --> Q
    Q -- "2 · claim (leased)" --> W
    W -- "3 · complete 'HELLO'" --> RES
    RES -- "4 · read result" --> S
```

Key properties:

| Property | What it means |
|---|---|
| **Inbox per identity** | Registration creates an identity, not a process. Tasks wait while the recipient is offline. |
| **One task, one result** | V1 has one recipient and one final output (or error) per task. |
| **At-least-once delivery** | Each claim is a time-limited lease. If a worker dies, the task is redelivered (up to 5 attempts). |
| **Execution stays with the agent** | The relay never runs task content; workers treat it as untrusted input. |
| **Scales horizontally** | Any number of API replicas and workers coordinate through PostgreSQL row locks. |

## Components

```mermaid
flowchart LR
    subgraph clients["Clients"]
        direction TB
        sender["Sender agents<br/>send tasks, read results"]
        workers["Worker processes<br/>python main.py worker"]
        browser["Dashboard in a browser<br/>token in sessionStorage"]
    end

    subgraph api["Relay API: FastAPI + uvicorn"]
        direction TB
        routes["main.py<br/>routes, auth, long-poll claim"]
        storage["storage.py<br/>register, send, claim,<br/>heartbeat, complete/fail"]
        db["database.py<br/>models, transactions,<br/>lease recovery"]
        loop["Recovery loop<br/>every 5 s"]
        dash["GET /<br/>dashboard.html"]
        routes --> storage --> db
        loop --> db
    end

    subgraph pg["PostgreSQL"]
        direction TB
        agents[("agents")]
        tasks[("tasks")]
        attempts[("attempts")]
    end

    sender -- "HTTPS + Bearer token" --> routes
    workers -- "HTTPS + Bearer token" --> routes
    browser -- "same API" --> routes
    browser -. "loads page" .-> dash
    db -- "SQLAlchemy / psycopg 3" --> pg
```

- **Relay API** is stateless; all state is in PostgreSQL, so replicas can be
  added or restarted freely.
- **Workers** are plain HTTP clients. They never touch the database and can run
  anywhere that can reach the API.
- **The dashboard** is a static page that calls the same authorized endpoints
  as any agent, with the token the user pastes in.

## A task end to end

SPEC acceptance scenario 1: register two agents; one sends a task; the other
claims and completes it; the sender reads the result.

```mermaid
sequenceDiagram
    autonumber
    actor A as Sender (alice)
    participant R as Relay API
    participant DB as PostgreSQL
    actor W as Worker (uppercase)

    A->>R: POST /agents {"name":"alice"}
    R->>DB: insert agent (token stored as SHA-256 hash)
    R-->>A: 201 {agent_id, token}  (token shown once)
    W->>R: POST /agents {"name":"uppercase"}
    R-->>W: 201 {agent_id, token}

    A->>R: POST /tasks {to, input:"hello"}  + Idempotency-Key
    R->>DB: insert task (status queued)
    R-->>A: 201 {task_id, status:"queued"}

    W->>R: POST /tasks/claim {worker_id, wait_seconds:30}
    Note over R: long poll: re-check every 0.5 s<br/>until a task arrives or 30 s pass
    R->>DB: SELECT ... FOR UPDATE SKIP LOCKED<br/>task → processing, new attempt
    R-->>W: 200 {task_id, input, attempt:1, claim_token, lease_expires_at}

    Note over W: executes locally: "hello".upper()
    loop while working (every ≤20 s)
        W->>R: POST /tasks/{id}/heartbeat {claim_token}
        R-->>W: 200 {lease_expires_at: now + 60 s}
    end

    W->>R: POST /tasks/{id}/complete {claim_token, output:"HELLO"}
    R->>DB: store output, close attempt, task → completed
    R-->>W: 200 {task_id, status:"completed"}

    A->>R: GET /tasks/{id}
    R-->>A: 200 {status:"completed", output:"HELLO", attempt_count:1}
```

Every call after registration carries `Authorization: Bearer <token>`; the
token, not any ID in the request, decides who the caller is.

## Task lifecycle

```mermaid
stateDiagram-v2
    direction LR
    [*] --> queued: POST /tasks
    queued --> processing: claimed (new attempt + lease)
    processing --> completed: complete with valid claim token
    processing --> failed: fail with valid claim token
    processing --> queued: lease expired, attempts remain
    processing --> failed: lease expired, 5th attempt used
    completed --> [*]
    failed --> [*]
```

While a task is `processing`, heartbeats extend its lease to 60 s from the
current server time. An expired lease cannot be revived, even if no other
worker has claimed the task yet. Explicit failure is terminal; it is not
retried automatically.

Each delivery attempt has its own outcome: `processing`, `completed`,
`failed`, or `expired`. `GET /tasks/{id}/attempts` shows this history, never
the claim tokens.

## Leases, crashes and redelivery

A claim is a 60 second lease. If the worker crashes or stalls and stops
heartbeating, the recovery loop expires the attempt and requeues the task,
and the next claim gets a **new claim token and attempt number**. A worker
that comes back late is rejected with its old token, even before recovery
has run.

```mermaid
sequenceDiagram
    participant W1 as Worker 1
    participant R as Relay API
    participant L as Recovery loop (every 5 s)
    participant W2 as Worker 2

    W1->>R: claim
    R-->>W1: attempt 1, claim_token A, lease 60 s
    Note over W1: stalls, e.g. network partition<br/>(no heartbeats reach the relay)
    Note over R: lease passes its deadline
    W1->>R: complete with token A (too late)
    R-->>W1: 409 stale_claim
    L->>R: expire attempt 1, task → queued
    W2->>R: claim
    R-->>W2: attempt 2, claim_token B, new lease
    W2->>R: complete with token B
    R-->>W2: 200 completed
```

Because a worker can crash after doing the work but before reporting it,
execution is **at least once**. Agents with external side effects should use
the task ID as an idempotency key. Retrying the exact same completion with
the same claim token is safe: it returns the original response.

## Concurrent claims

Several workers (and API replicas) can poll the same inbox at once. Each
claim runs in one transaction that locks the oldest queued task with
`FOR UPDATE SKIP LOCKED`. A row locked by another transaction is **skipped,
not waited on**, so racing workers get different tasks immediately.

```mermaid
sequenceDiagram
    participant W1 as Worker 1
    participant W2 as Worker 2
    participant DB as PostgreSQL

    par
        W1->>DB: BEGIN, then SELECT oldest queued task<br/>FOR UPDATE SKIP LOCKED
        DB-->>W1: task 1 (row now locked)
    and
        W2->>DB: BEGIN, then SELECT oldest queued task<br/>FOR UPDATE SKIP LOCKED
        DB-->>W2: task 2 (task 1 is locked, so skipped)
    end
    W1->>DB: task 1 → processing, insert attempt, then COMMIT
    W2->>DB: task 2 → processing, insert attempt, then COMMIT
```

Lock order is always **task row first, then its attempts**, for claims,
heartbeats, completions and recovery alike, which avoids deadlocks. Advisory
locks additionally serialize idempotent task creation (per sender and key)
and schema creation when several replicas start at once.

## Data model

```mermaid
erDiagram
    AGENTS ||--o{ TASKS : "sends (sender_id)"
    AGENTS ||--o{ TASKS : "receives (recipient_id)"
    TASKS ||--o{ ATTEMPTS : "delivered as"

    AGENTS {
        string id PK "agent_ + 32 hex chars"
        string name "display label, not unique"
        string description "optional"
        string token_hash "SHA-256 of the bearer token"
        timestamptz created_at
        timestamptz last_seen_at "last authenticated request"
    }
    TASKS {
        string id PK "task_ + 32 hex chars"
        string sender_id FK
        string recipient_id FK
        text input
        string status "queued, processing, completed, failed"
        text output
        text error
        int attempt_count
        string idempotency_key "unique per sender"
        timestamptz created_at
        timestamptz finished_at
    }
    ATTEMPTS {
        int id PK
        string task_id FK
        int attempt_number "unique per task"
        string worker_id "diagnostic label"
        string claim_token_hash UK "SHA-256 of the claim token"
        timestamptz claimed_at
        timestamptz lease_expires_at
        timestamptz finished_at
        string outcome "processing, completed, failed, expired"
        string terminal_action "complete or fail"
        string terminal_payload_hash "makes terminal retries idempotent"
    }
```

## Security model

- **Two kinds of secret.** The *agent token* (`agt_…`) identifies the caller
  on every request; the per-claim *claim token* (`clm_…`) proves the caller
  holds the current lease. Both are returned exactly once and stored only as
  SHA-256 hashes, so a database dump does not reveal them.
- **Identity comes from the token**, never from IDs in the request body, so a
  client cannot send as someone else or read another agent's inbox.
- **Only the sender and recipient** can read a task, its result, or its
  attempt history. Everyone else gets `404`, which does not reveal whether
  the task exists.
- **Registration** is open locally. Shared installations set
  `RELAY_ENROLLMENT_SECRET`, which callers send as `X-Enrollment-Secret`.
- **The dashboard** keeps the token in `sessionStorage` and renders task
  content as text.

## Deployments

The same image runs in every environment. Only how PostgreSQL is provided
and how the API is reached changes.

### Docker Compose

```mermaid
flowchart LR
    user["Browser / agents / workers"]
    subgraph host["Docker host"]
        subgraph compose["docker compose project"]
            relay["relay<br/>agent-relay:local<br/>health check: /ready"]
            postgres["postgres<br/>postgres:17-alpine<br/>health check: pg_isready"]
        end
        vol[("volume<br/>postgres-data")]
    end

    user -- ":8000" --> relay
    relay -- "postgres:5432" --> postgres
    postgres --- vol
    relay -. "depends_on: service_healthy" .-> postgres
```

### Kubernetes (kind)

```mermaid
flowchart TB
    user["Browser / agents / workers"]
    subgraph ns["namespace: agent-relay"]
        svc_relay["Service relay<br/>ClusterIP :8000"]
        subgraph dep["Deployment relay (2 replicas)"]
            direction LR
            r1["relay pod<br/>init: wait-for-postgres<br/>readiness /ready<br/>liveness /health"]
            r2["relay pod<br/>init: wait-for-postgres<br/>readiness /ready<br/>liveness /health"]
        end
        svc_pg["Service postgres<br/>headless :5432"]
        subgraph sts["StatefulSet postgres"]
            pg0["postgres-0<br/>readiness/liveness pg_isready"]
        end
        pvc[("PVC data-postgres-0<br/>1 Gi")]
        secret[/"Secret postgres-credentials<br/>from k8s/postgres.env"/]
    end

    user -- "kubectl port-forward" --> svc_relay
    svc_relay --> r1 & r2
    r1 & r2 --> svc_pg --> pg0
    pg0 --- pvc
    secret -. "POSTGRES_PASSWORD" .-> dep
    secret -.-> sts
```

- Readiness uses `/ready`, which queries the real tables, so a replica that
  loses the database leaves the Service without being restarted. Liveness
  uses `/health` only, so a database outage does not restart every replica.
- Postgres applies its password only when the volume is first initialized.
  The CI deploy job therefore reuses an existing deployment's password.

## CI/CD pipeline

`.github/workflows/ci.yml` tests every change against PostgreSQL in three
deployment shapes, and deploys only if every test passes.

```mermaid
flowchart LR
    trigger["push to main<br/>pull request<br/>manual dispatch"]

    subgraph test["job: test"]
        direction TB
        unit["Unit / protocol tests<br/>throwaway PostgreSQL"]
        subgraph s1["Acceptance scenario 1 over HTTP"]
            direction TB
            t1["process<br/>uvicorn subprocess"]
            t2["compose<br/>full stack, random ports"]
            t3["kubernetes<br/>k8s/ in a throwaway namespace"]
        end
        unit --> s1
    end

    subgraph deploy["job: deploy (needs: test)"]
        direction TB
        tag["Tag image<br/>commit + hash of build inputs"]
        build["docker build"]
        load["kind load docker-image"]
        apply["kubectl apply -k<br/>with the new tag"]
        wait["Wait for rollouts"]
        smoke["scripts/smoke.py<br/>/ready, /health,<br/>dashboard heading, 401"]
        tag --> build --> load --> apply --> wait --> smoke
    end

    trigger --> test
    test -- "all passed" --> deploy
```

| Where it runs | kind cluster used |
|---|---|
| GitHub-hosted runners | a throwaway cluster created per job |
| Locally with [`act`](https://nektosact.com/) | your existing cluster, via the mounted Docker socket |

Because the tag includes a hash of the build inputs, every code version, even
an uncommitted local change under `act`, produces a new tag and therefore a
real rollout.
