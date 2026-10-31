# 0005: Run queue and leases

Status: proposed in PR 1.2 (week 1); accepted when the owner merges it. PR 1.3 adds the worker's side.

## Context

design.md specifies the mechanism but not the details:

- the API or the scheduler tick inserts a run row;
- a worker leases it with `SELECT ... FOR UPDATE SKIP LOCKED`;
- the worker extends the lease while it works;
- an expired lease lets another worker resume from the last checkpoint;
- there is one active run per thread, and a second resume waits.

The data model lists a run's fields as `id, workflow, version, thread_id, status, lease_until, attempts`, changing "status only".

This ADR fills in what design.md leaves open: the extra columns, the statuses, the claim query, the fencing token and the limits.

## Decision

**Table `runs`** (migration 0003). The design's fields plus the following:

| Column | Why |
|---|---|
| `kind` (`start` or `resume`) | Whether `payload` is the graph's input or the resume value for an interrupt |
| `payload` (jsonb) | The input or the resume value |
| `lease_owner` (uuid) | The fencing token: a fresh uuid per claim |
| `error` | Why a run failed, cut to 2,000 characters |
| `created_at`, `updated_at` | Ordering and diagnosis |

**Statuses:**

| Status | Meaning |
|---|---|
| `queued` | Waiting for a worker |
| `running` | Leased by a worker |
| `interrupted` | The graph stopped at an interrupt and waits for a resume run |
| `succeeded` | The graph finished |
| `failed` | An error, a workflow or version that isn't registered (PR 1.3), or too many expired leases |

Rows are never deleted.

**Claiming** (`jhunt.platform.runtime.queue.claim`). One transaction, three statements:

1. **Fail exhausted runs.** A run that is `running`, has an expired lease and has `attempts >= MAX_ATTEMPTS` becomes `failed`, with "gave up after N attempts: the lease expired each time". Without this step, such a run stays the oldest open run on its thread and blocks every later run on it forever.
2. **Pick a run.** The run must be `queued`, or `running` with an expired lease. It must also be the oldest open run (`queued` or `running`) on its thread. The pick uses `ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1`.
3. **Take the lease.** Set `status = 'running'`, a new `lease_owner`, `lease_until = now() + lease` and `attempts + 1`.

Rules that go with it:

- **One active run per thread** (hard rule 11) holds twice over:
  - the oldest-open-run condition in step 2 means a busy thread's later runs wait;
  - a partial unique index, `runs (thread_id) WHERE status = 'running'`, makes the database refuse a second running run if the query is ever wrong. A claim that hits that index rolls back and returns nothing.
- **Indexes for the claim query:** `runs (status, id)` and `runs (thread_id, status)`.
- **One clock:** all lease times use Postgres `now()`, never a worker's clock.

**Fencing.** `extend`, `finish` and `fail` all update `WHERE id = :id AND lease_owner = :owner AND status = 'running'`, and return whether exactly one row changed. A worker whose lease expired and was taken over gets `False` back.

From PR 1.3, a worker that gets `False` from `extend` exits immediately (`os._exit`). It doesn't close the run or finish its step, and the new owner carries on.

**Numbers:**

| What | Value |
|---|---|
| Lease | 30 s |
| Heartbeat (`extend`) | every 10 s, from PR 1.3 |
| Maximum attempts | 5. An attempt is counted when the run is claimed. A graph that raises an error fails the run at once; it is not retried here. Retries come with the tool gateway and Scout in weeks 3 to 4 |

Tests pass shorter leases as arguments.

## Known limit

Fencing stops a stale worker from writing to the `runs` row. It doesn't stop that worker's graph code. Consider a worker that is stalled but not dead (a long GC pause, a frozen VM, a network partition). It can lose its lease while a step is in progress. A new worker then takes over, while the old one may still finish that step and write a checkpoint before its next heartbeat notices.

The self-fence in PR 1.3 makes that window as small as one heartbeat, but doesn't close it. Side effects in that window are protected by the tool gateway's idempotency keys (weeks 3 to 4), not by the queue.

## Open questions

- **True fencing for checkpoint writes.** Wrap the LangGraph checkpointer so that each checkpoint write checks, in the same Postgres transaction, that the run's `lease_owner` is still this worker. A stale worker's write would then fail instead of landing. This needs the checkpointer to write inside a transaction we control, and its writes span several tables. It could be a useful upstream contribution to `langgraph-checkpoint-postgres`, as a hook for a "write precondition".
- **Graph errors.** Whether to retry graph errors (as opposed to crashes) inside the queue, with backoff, or leave it to each workflow. To be decided with Scout's retries in weeks 3 to 4.
