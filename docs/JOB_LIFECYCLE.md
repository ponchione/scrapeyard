# Job Cancellation and Deletion

Scrapeyard separates cancellation from deletion. Cancellation stops the current
accepted delivery and keeps its durable job/run history. Deletion is a later,
resumable cleanup operation for cancelled or terminal work.

## State machine

- `queued -> running`: the worker claims the exact `job_id` and `current_run_id`.
- `queued -> cancelled`: cancellation changes the parent only. A queued delivery
  has no `JobRun`, so cancellation does not create one. The cancelled
  `current_run_id` remains on the job as the accepted Redis delivery identity.
- `running -> cancelled`: one jobs.db transaction changes the exact running
  `JobRun` and its matching parent, sets completion/update timestamps, and
  disables scheduling.
- `running -> complete|partial|failed`: normal terminal finalization remains an
  ownership-checked transaction. Cancellation and finalization serialize, so
  exactly one can win.
- `scheduled fire -> failed attempt`: a cron callback that fails before Redis
  acceptance persists a synthetic terminal `JobRun` with a sanitized
  `failure_code` and degrades that schedule's durable health. It may converge
  the parent only when no other queued/running owner exists. A later accepted
  cron run clears the schedule failure state; success by a different schedule
  does not.
- `cancelled|complete|partial|failed -> deleting`: deletion reserves an
  immutable `delete_results` policy after confirming that the job has no
  pending webhook delivery.
- `deleting -> absent`: Redis must be complete or missing; external database and
  filesystem cleanup then precedes one final jobs.db transaction.

`cancelled` and `deleting` never return to `queued` or `running`. Queued
reconciliation, stale-running recovery, scheduler triggers, and terminal
webhook reconciliation all exclude those states. A scheduler callback that
loaded older state still loses its `queue_run` compare-and-set.

## Cancel API

`POST /jobs/{job_id}/cancel` has this contract:

- `204`: queued/running cancellation is durable and the run is quiescent, or the
  job was already cancelled and quiescence was re-verified.
- `404`: the job does not exist.
- `409`: the job is complete, partial, failed, or deleting.
- `503`: the job is durably cancelled, but Redis cancellation/inspection is
  unavailable. Retry the same request when Redis recovers.
- `504`: the job is durably cancelled, but the configured abort grace expired.
  Retry the same request to finish verification.
- `500`: the jobs.db cancellation transition itself could not be persisted.

The transition disables future schedule triggers before local APScheduler
removal and before Redis cancellation. A queued/deferred delivery is located
in the fixed priority queue set and atomically moved to the base execution
queue before `Job.abort()` places the run ID in arq's abort set. The embedded
worker has `allow_abort_jobs=True`, so arq removes the delivery before invoking
the scrape handler. This cancellation admission bypasses priority fairness only
to prove quiescence; it does not execute the handler. For in-progress work, arq
cancels the owned asyncio handler task and waits for its cancellation result.
The bound is
`SCRAPEYARD_WORKERS_CANCELLATION_GRACE_SECONDS` (default 10 seconds).

The worker also checks exact jobs.db run ownership at bounded lifecycle
boundaries: before target starts; around target delays and domain rate-limit
waits; before/after retry backoff; after fetch returns; between pagination
pages; before/after validation retry; before/after error flush; before/after
result persistence; and before terminal finalization. Cancellation propagates
to outstanding target children, exits browser-permit contexts, stops the run
heartbeat, and deletes an unfinalized run artifact. It never creates
`job.failed` or a cancellation webhook.

A `204` means the arq handler can no longer create later errors, results,
terminal state, or webhook intent for that run. Writes that completed before
quiescence may have existed briefly. An unowned result is removed before the
response; already committed error writes are not retroactively erased by
cancellation, but no new writes can occur after the response.

## Delete API

`DELETE /jobs/{job_id}?delete_results=false` has this contract:

- `204`: deletion completed, or the job was already absent.
- `409`: the job is queued/running (cancel it first), a pending/in-flight
  webhook blocks deletion, or an existing deletion reservation has the
  opposite result policy.
- `503`: deletion is reserved, but Redis is unavailable or still reports the
  current run as queued, deferred, or in progress. Retry with the same policy.
- `500`: a cross-database/filesystem phase or final jobs.db deletion failed.
  The reservation remains and the same request resumes it.

The jobs.db reservation sets `status=deleting`, disables the schedule, records
`deletion_requested_at`, and persists `delete_results_on_delete`. The parent
job remains authoritative until cleanup is complete. This prevents an already
running scheduler callback or reconciliation pass from recreating work while
errors.db, results_meta.db, and result files are cleaned independently.

Cleanup order is:

1. Confirm the current arq run is `complete` or `missing`.
2. Remove all errors for the job.
3. If `delete_results=true`, remove contained result directories and then their
   metadata. Files are removed first on this explicit path so a filesystem
   fault leaves metadata paths available for retry.
4. In one jobs.db transaction, recheck the reservation and absence of pending
   webhooks, remove delivered/failed/scrubbed webhook rows, remove `JobRun`
   rows, and finally remove the job.

`delete_results=false` retains result metadata and files without retaining job
YAML or other secrets. `GET /results/{job_id}` continues to serve the newest
metadata row after the job is gone; `latest=false&run_id=...` serves an explicit
retained run. `GET /jobs/{job_id}` remains `404`. Preserved results remain
subject to normal age and per-job result retention. Errors have no
post-deletion access path and are always removed.

When the parent job is absent, project-scoped readers are authorized against
the `results_meta.project` owner selected by the same explicit/latest lookup as
the artifact. Cross-project and missing retained IDs both return `404`;
unscoped readers retain access.

Periodic artifact reconciliation also treats every retained `results_meta` row
as authoritative, so absence of the parent job after `delete_results=false`
does not make its run directory an orphan. An unindexed cancellation artifact
can be removed only after the configured orphan grace and only when no exact
queued/running project/job/run ownership exists. Explicit deletion continues
to remove files before metadata for retryability, while normal retention
continues to remove metadata first. Reconciliation never deletes or repairs a
metadata row whose `results.json` is missing, unsafe, unreadable, or corrupt.

Pending includes a delivery whose HTTP attempt is currently in flight because
the durable row remains `pending` until its terminal transition. Deletion waits
rather than racing that request. Delivered, permanently failed, and scrubbed
rows may be removed only in the final transaction with their terminal runs;
until then, item-06 tombstones are the item-05 evidence that prevents terminal
reconciliation from recreating an old logical event.

## Failure and consistency limits

Deletion is idempotent but not one cross-system transaction. A process crash
can leave a `deleting` job after any completed external cleanup phase; an
operator or client must repeat DELETE with the original policy. A crash after
filesystem removal but before metadata deletion leaves a metadata row pointing
to a missing file until retry. A host/storage failure can still exceed the
guarantees of SQLite WAL with `synchronous=NORMAL` and atomic local filesystem
replacement.

Artifact reconciliation narrows but cannot make the filesystem and SQLite
atomic. It rechecks metadata, active ownership, and age before removal, but a
final race remains between the last SQLite query and filesystem mutation. The
grace threshold is the safety barrier for that interval; the deployment's
single-process/local-filesystem assumption remains in force.

Redis abort and inspection do not scan keys and are scoped only to the current
run ID. Inspection checks arq's global result, in-progress, and payload keys
plus the base execution queue and the three fixed priority intake queues in one
bounded transaction. Multiple queue membership is treated as corruption and
fails closed. Redis loss can delay proof of quiescence, so
cancellation/deletion fail closed rather than guessing. The scheduler,
embedded arq worker, priority admission cursor, local
filesystem, SQLite connection manager, and webhook coordinator retain the
documented single-process assumption; this work does not add multi-instance
cancellation leases.

Webhook HTTP remains asynchronous and at-least-once. A request completed before
deletion was requested may already have reached the receiver. A network/process
failure after receiver acceptance can cause a retry with the same deterministic
delivery ID; pending state blocks deletion until the delivery becomes terminal.
