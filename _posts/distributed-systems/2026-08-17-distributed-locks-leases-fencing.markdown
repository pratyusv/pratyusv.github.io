---
layout: single
comments: true
title: "Distributed Locks: Leases, Fencing Tokens, Sessions, and Failure Safety"
date: 2026-08-17 04:00:00+0100
description: "A connected worker-handoff story explaining distributed locks, leases, fencing tokens, sessions, ZooKeeper and etcd recipes, Redis trade-offs, and failure-safe resource access."
tags: [distributed-locks, leases, fencing-tokens, zookeeper, etcd, redis, leader-election, distributed-systems]
categories: ['Distributed Systems Components']
---

# 1. One Report, Two Workers

At 09:00, worker `W1` acquires ownership of monthly report `report/2026-07`.
It will read several files, calculate the result, and replace the published
report:

~~~text
coordination key   /locks/report/2026-07
protected resource reports/2026-07.csv
worker             W1
~~~

Halfway through, `W1` stops for a long garbage-collection pause. It is alive,
but it sends no heartbeats and executes no application code. The lock service
eventually concludes that `W1` is gone and gives the work to `W2`.

`W2` finishes and publishes the new report. Then `W1` resumes. Its memory still
says "I acquired the lock," so it publishes an older result over `W2`'s file.

![A paused worker resumes after ownership moved to another worker](/assets/img/distributed-locks/story-overview.svg)

The lock service behaved as designed. It restored availability after the old
holder stopped responding. The protected resource was still corrupted.

This story exposes the central problem:

> A distributed lock can decide who should own a resource now. It cannot erase
> old code, delayed packets, or operations already in flight.

We will keep the same report job throughout the article. First we will build a
lock, then discover why it needs an expiry policy, and finally make the file
store reject stale owners with a fencing token.

---

# 2. A Distributed Lock Is Not a Remote Mutex

A process-local mutex lives in one failure domain. The operating system knows
which thread owns it, and a stopped process loses access to the protected
memory. Unlock and protected mutation are ordered by the same machine.

A distributed lock spans independent processes and systems:

~~~text
worker W1       coordination service       object store
worker W2       replicated state           report file
~~~

The coordination service cannot directly observe whether a worker is paused,
partitioned, overloaded, or dead. The object store may accept a request long
after the lock service has transferred ownership. Messages can be delayed and
replies can be lost independently.

![A local mutex and a distributed lock have different failure boundaries](/assets/img/distributed-locks/local-vs-distributed.svg)

A useful distributed-lock contract separates three roles:

| Role | Responsibility |
|---|---|
| Coordination service | Order acquisition attempts and publish current ownership |
| Lock client | Acquire, renew, stop on loss, and release only its own claim |
| Protected resource | Atomically reject commands from stale ownership generations |

Leaving out the third role is the most common design error.

---

# 3. State the Properties Before Choosing a Tool

The phrase "distributed lock" hides several different requirements.

## Safety

For report publication, the critical invariant is:

~~~text
The protected store never accepts a mutation from an ownership generation
older than the greatest generation it has already accepted.
~~~

This is stronger and more useful than "only one worker believes it holds the
lock." Beliefs can overlap during pauses. Accepted mutations must not move
backward.

## Liveness

If a holder crashes, another healthy worker should eventually acquire the
resource. Liveness normally needs timeouts, failure detectors, or explicit
administrative recovery.

## Additional Policy

A system may also require:

- bounded acquisition time;
- first-come-first-served admission;
- re-entrant locking by the same session;
- read and write modes;
- automatic cleanup after client failure;
- a bounded interval before takeover;
- auditability of every ownership transition.

![Safety, liveness, and policy are separate lock properties](/assets/img/distributed-locks/safety-liveness.svg)

Safety and liveness pull in opposite directions. Waiting forever for `W1`
avoids overlapping owners but prevents recovery. Taking over quickly restores
progress but creates a larger chance that `W1` is merely slow. Fencing lets the
system regain liveness without trusting the old holder to stay silent.

---

# 4. The First Lock Has No Failure Recovery

Suppose a transactional database stores one lock row:

~~~sql
CREATE TABLE resource_lock (
    resource_id TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL
);
~~~

Acquisition inserts the row only if it is absent:

~~~sql
INSERT INTO resource_lock(resource_id, owner_id)
VALUES ('report/2026-07', 'W1')
ON CONFLICT DO NOTHING;
~~~

Only one insert wins, so concurrent acquisition is serialized. Release must
also verify ownership:

~~~sql
DELETE FROM resource_lock
WHERE resource_id = 'report/2026-07'
  AND owner_id = 'W1';
~~~

Deleting by resource name alone is unsafe. A delayed cleanup from `W1` could
delete a newer claim owned by `W2`.

This design has mutual exclusion while the database is reachable, but `W1`
can crash permanently after acquisition. Its row never disappears and the
report never runs again.

---

# 5. A Lease Makes Ownership Temporary

A **lease** is ownership valid for a bounded interval according to the
authority that grants it. Add an expiry and an ownership generation:

~~~text
resource       report/2026-07
owner          W1
lease_id       L-81
fence          41
expires_at     authority time 09:00:30
~~~

The client renews before expiry. If renewal stops, the authority may grant a
new lease after the old one expires.

![Grant, renewal, expiry, and takeover in a lease lifecycle](/assets/img/distributed-locks/lease-lifecycle.svg)

The lease converts an unbounded crash wait into a bounded one. It does not prove
that the client stopped at the boundary. Expiry is a statement made by the
authority:

~~~text
lock service: L-81 is no longer current
~~~

It is not a remote interrupt delivered to every thread, device, database, and
packet that `W1` may have touched.

## TTL Selection

Let:

~~~text
T = lease time to live
R = renewal interval
D = high-percentile renewal round-trip delay
P = plausible client scheduling pause
E = authority expiry and failover delay
~~~

A client needs enough margin that ordinary renewal completes before expiry:

~~~text
R + D + P << T
~~~

This is an operational budget, not a proof of safety. No finite TTL can rule
out an unexpectedly long pause. A shorter TTL improves takeover time but makes
false expiry more likely; a longer TTL tolerates disruption but delays recovery.

---

# 6. The Paused-Client Failure

Now replay the opening incident precisely:

~~~text
09:00:00  W1 receives lease L-81, fence 41
09:00:05  W1 reads the old report
09:00:08  W1 pauses for 40 seconds
09:00:30  the authority expires L-81
09:00:31  W2 receives lease L-82, fence 42
09:00:38  W2 publishes the new report
09:00:48  W1 resumes and sends its old result
~~~

![Lease expiry allows a new owner while the old owner is paused](/assets/img/distributed-locks/pause-split-brain.svg)

Checking the lease immediately before the write is insufficient:

~~~text
W1 checks lease -> long pause -> W2 takes over -> W1 writes
~~~

There is always a gap between a client-side check and an external side effect.
Renewing in a background thread does not solve this either. The renewal thread
and work thread can observe different timing, and a request already sent to the
resource may arrive late.

The design must make the final resource operation conditional on current
ownership.

---

# 7. Fencing Tokens Turn Ownership Into an Order

Every successful acquisition receives a token greater than every token issued
for the same protected domain:

~~~text
W1 acquires -> fence 41
W2 acquires -> fence 42
W3 acquires -> fence 43
~~~

Each mutation carries its token. The protected resource stores the greatest
accepted token and rejects lower ones.

![A resource accepts token 42 and rejects delayed token 41](/assets/img/distributed-locks/fencing-timeline.svg)

When `W2` publishes with token 42, the store advances its watermark to 42.
`W1`'s delayed token 41 is then harmless even if its process still believes it
owns the lease.

The invariant is:

~~~text
accepted_token >= greatest_token_previously_accepted
~~~

Tokens need not be wall-clock timestamps. They need a total order consistent
with ownership grants. Consensus log indexes, database sequences, ZooKeeper
transaction IDs, and etcd revisions can provide such an order.

## Where the Check Must Happen

The comparison and protected mutation must share one atomic boundary:

~~~sql
UPDATE reports
SET body = :body,
    greatest_fence = :token
WHERE report_id = :id
  AND greatest_fence < :token;
~~~

If zero rows change, the request is stale. A separate `SELECT` followed by an
unconditional `UPDATE` recreates a race.

![The token comparison and mutation occur in one resource transaction](/assets/img/distributed-locks/resource-check.svg)

For an object store, the equivalent may be a conditional write against an
object generation or metadata value. For a device controller, the gateway may
persist the greatest epoch. If the final resource has no conditional-update or
token-validation mechanism, the lock cannot fully protect it from a stale
holder.

The strict `<` predicate suits this story because each lease publishes once.
If one ownership term sends several commands, the resource normally uses a
two-level rule:

~~~text
token < greatest_token   -> reject stale generation
token > greatest_token   -> establish new generation, then validate command
token = greatest_token   -> validate per-command sequence/version/idempotency
~~~

The new holder should establish its generation at the resource before assuming
old work is fenced. If token 41 reaches an untouched resource before token 42,
the resource can legally accept 41; once it accepts or establishes 42, it must
never accept 41 again. Fencing orders effects—it does not synchronize the
resource's clock with lease expiry.

## Token Scope

A token must be monotonic over every pair of owners that can conflict at the
same resource. A global consensus revision is sufficient but may expose more
ordering than required. A per-resource epoch reduces coupling but must never be
reset or reused while an old operation may still arrive.

Fencing protects mutations. It does not make a stale read fresh, revoke a
credential, or undo an external effect that already committed.

---

# 8. A Small Fenced Resource Interface

The application code should make the token impossible to forget:

~~~cpp
struct Lease {
    std::string resource;
    std::string owner;
    std::uint64_t fencing_token;
};

enum class WriteResult { applied, stale_owner, conflict };

class ReportStore {
public:
    virtual WriteResult publish(
        const Lease& lease,
        std::string_view report,
        std::string_view expected_version) = 0;
};
~~~

This interface carries two independent controls:

- `fencing_token` rejects an earlier ownership generation;
- `expected_version` detects a concurrent content change within the current
  business workflow.

Fencing is not a replacement for optimistic concurrency, idempotency, or a
transaction. It prevents stale ownership from being honored; the resource may
still need its own application invariants.

---

# 9. Acquisition, Renewal, Work, and Release

A robust client follows an explicit state machine:

~~~text
IDLE -> ACQUIRING -> HELD -> SUSPECT -> LOST
                    |                |
                    +----> RELEASING-+
~~~

![The client stops new work before declaring a lease lost](/assets/img/distributed-locks/client-state-machine.svg)

## Acquire

Acquisition returns one indivisible result:

~~~text
lease identity + ownership token + observed TTL
~~~

The caller must not fabricate a token in a second, unrelated request. Otherwise
two clients can disagree about which token belongs to which acquisition.

## Renew

Renew only the exact lease identity returned by acquisition. Schedule renewal
well before expiry and track the last acknowledgement from the authority.

The client should enter `SUSPECT` when it no longer has enough remaining time
to safely begin another unit of work. In `SUSPECT`, stop admitting work while
trying to resolve the lease. On definite loss, enter `LOST` and cancel what can
be cancelled.

![Renewal margin separates work admission from final expiry](/assets/img/distributed-locks/renewal-window.svg)

## Release

Release must compare the lease identity, not just the resource name. It is an
optimization for fast handoff, not the sole cleanup mechanism. If the release
reply is lost, the client should not assume either success or failure; expiry
will eventually remove the claim.

## Work

Every externally visible operation includes the fence. Stopping on loss reduces
conflicts and wasted work. Resource-side validation provides the final safety
boundary for work already running.

---

# 10. Every RPC Can Have an Uncertain Outcome

Consider acquisition:

~~~text
W1 -> acquire request -> authority commits L-81
W1 <- response is lost
~~~

Did `W1` acquire the lock? The authority says yes; the caller does not know.
Blindly retrying with a new identity can create multiple claims for one client.

![A lost acquisition reply creates an uncertain client outcome](/assets/img/distributed-locks/uncertain-acquire.svg)

Use a stable request or session identity so a retry can discover the original
result. ZooKeeper lock recipes commonly include a GUID in the sequential znode
name for exactly this create-succeeded/reply-lost case. A custom service can
deduplicate `Acquire(resource, request_id)` and return the same lease record.

Renewal and release are uncertain too. Operations should be idempotent:

~~~text
Renew(L-81)   changes only L-81
Release(L-81) deletes only L-81
Release(L-81) repeated after deletion is harmless
~~~

Never let an old release delete a newer lease.

---

# 11. The Coordination Service Needs Consensus

If two lock-service replicas can independently grant the same resource during a
partition, the client protocol cannot restore one global order. Correctness-
critical acquisition therefore needs a linearizable authority—typically a
consensus-backed service or one transactional database with an unambiguous
primary.

![Consensus orders grants while fencing protects the external resource](/assets/img/distributed-locks/control-data-resource.svg)

Separate the planes:

| Plane | Typical operations |
|---|---|
| Coordination plane | acquire, queue, renew, release, session expiry |
| Work plane | read input, compute, write report, call device or service |

The coordination plane should remain small. Do not stream report data through
the lock service. It orders ownership; it is not the data path.

Consensus alone does not fence the object store. It makes ownership grants and
tokens ordered. The object store must still enforce that order.

---

# 12. ZooKeeper: Ephemeral Sequential Contenders

For lock root `/locks/report-2026-07`, each contender creates an **ephemeral
sequential** child:

~~~text
/locks/report-2026-07/req-a7-0000000041   W1
/locks/report-2026-07/req-b2-0000000042   W2
/locks/report-2026-07/req-c9-0000000043   W3
~~~

The smallest sequence owns the lock. Every waiter watches only its immediate
predecessor. When `W1` releases or its session expires, only `W2` wakes, lists
the contenders again, and proves it is smallest.

![ZooKeeper contenders form an ordered queue and watch predecessors](/assets/img/distributed-locks/zookeeper-queue.svg)

Watching the owner from every waiter causes a herd when it disappears. Watching
the predecessor distributes wake-ups and preserves queue order.

Ephemeral nodes bind cleanup to ZooKeeper session expiry. A temporary network
disconnect does not immediately end a session; the client must treat connection
state carefully and stop work when ownership is uncertain. A session-expired
event is definitive: the old ephemeral claim is gone and cannot be recovered.

The sequential suffix orders contenders, but production code should derive a
fencing token from a ZooKeeper ordering value with the required scope, such as
the creation transaction ID recorded in node metadata. The external resource
must enforce it. The complete [ZooKeeper internals guide]({% post_url distributed-systems/2022-01-09-zookeeper %})
develops sessions, watches, Zab, and the election recipe in more detail.

---

# 13. etcd: Lease-Bound Keys and Transactions

etcd combines several useful primitives:

- a lease with server-managed TTL and keepalive;
- keys attached to that lease and deleted on expiry or revocation;
- linearizable key-value operations by default;
- atomic compare/then/else transactions;
- a revision on committed key-space changes;
- lock and election APIs built above these primitives.

The etcd lock API returns an ownership key that exists while the caller owns the
lock. That key can guard mutations **inside etcd**:

~~~text
IF create_revision(ownership_key) == acquired_revision
THEN update configuration key
ELSE reject as no longer owner
~~~

![An etcd lease owns a lock key whose revision guards a transaction](/assets/img/distributed-locks/etcd-lease-txn.svg)

Because the validation and update execute in one etcd transaction, a stale
holder cannot mutate protected etcd keys. For a resource outside etcd, carry an
ordered acquisition revision or application epoch to that resource and validate
it there.

An etcd lease ID is an identity, not automatically a suitable monotonically
increasing fence. Use a value whose ordering contract is explicit for the
protected scope.

---

# 14. A Transactional Database Can Host a Lease

When all contenders already depend on one strongly consistent database, a
separate coordination service may be unnecessary. One possible row is:

~~~sql
CREATE TABLE resource_lease (
    resource_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    lease_id UUID NOT NULL,
    fence BIGINT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
~~~

Acquisition or takeover runs in a transaction, locks the row, checks expiry
using database time, increments `fence`, and replaces the lease identity.

![A database transaction atomically checks expiry and increments the fence](/assets/img/distributed-locks/database-lease.svg)

Important details:

- use the database's clock for stored expiry comparisons;
- allocate the fence in the same transaction as ownership;
- renew with `WHERE lease_id = :mine`;
- release with `WHERE lease_id = :mine`;
- enforce the fence in the protected update transaction;
- index and shard the table according to expected lock-key contention.

Database failover semantics now become lock semantics. If the database can
promote a replica that loses a committed grant, the lock can move backward.
The required durability must match the consequence of overlapping work.

For queue workers, `SELECT ... FOR UPDATE SKIP LOCKED` may be simpler than a
named distributed lock: the database atomically assigns rows while naturally
integrating the work-state update.

---

# 15. Redis: Compare Identity on Release and Renewal

A common single-Redis lease acquires a key only when absent and attaches a TTL:

~~~text
SET lock:report/2026-07 7f32... NX PX 30000
~~~

The random value is the lease identity. Release must atomically delete only if
the value still matches. Renewal must atomically extend only the matching value.
A separate `GET` followed by `DEL` or `PEXPIRE` has a race.

![Redis release compares the unique lease value before deletion](/assets/img/distributed-locks/redis-compare-delete.svg)

This pattern is useful when an occasional duplicate is an efficiency cost—for
example, two workers regenerate the same disposable cache object. It is not by
itself resource fencing. A random identity prevents one client from deleting
another client's lease, but it does not tell an external resource that lease
`7f32...` is older than lease `a910...`.

Asynchronous primary-replica failover can also lose a recently acquired key:

~~~text
primary accepts W1 lock -> crashes before replication
replica is promoted      -> W2 acquires the same lock
~~~

Choose this design only when its failure consequence is acceptable, or combine
coordination with a stronger resource-side concurrency mechanism.

---

# 16. What the Redlock Debate Is Really About

Redlock attempts to acquire the same named lease with a unique value from a
majority of independent Redis masters within the lease-validity window. Its
safety argument depends on bounded clock drift and operations completing fast
enough relative to the TTL.

![A client acquires a majority while elapsed time reduces lease validity](/assets/img/distributed-locks/redlock-majority.svg)

The well-known debate is useful because it forces two questions:

1. **What failure model does the algorithm assume?** Clock behavior, process
   pauses, network delay, persistence, and restart policy matter.
2. **What happens at the protected resource after ownership changes?** A lease
   algorithm that produces no monotonic fence cannot by itself make a delayed
   external write stale.

Redis documentation presents Redlock and now recommends implementing fencing
tokens when correctness requires them. Martin Kleppmann's critique argues that
timing assumptions and the lack of an ordered fence make it unsuitable as the
sole correctness boundary under an asynchronous failure model.

The practical decision is not "Redis good" or "Redis bad":

| Consequence of overlap | Appropriate direction |
|---|---|
| Duplicate cache fill or redundant cleanup | A best-effort Redis lease may be enough |
| Duplicate but idempotent job | Lease plus durable idempotency key |
| Corrupted database, conflicting device command, financial error | Linearizable ownership plus resource-enforced fence or a single resource transaction |

Document the failure model and consequence instead of choosing from the name
of the algorithm alone.

---

# 17. Leader Election Is a Long-Lived Lease

Leader election and distributed locking use the same ownership mechanics but
serve different shapes of work.

~~~text
lock:    own resource R for one critical operation
leader:  own role G and repeatedly originate operations for a term
~~~

![A leadership term fences every command produced during that term](/assets/img/distributed-locks/leader-term.svg)

Each leadership term should have an ordered epoch. Every command produced by
that leader carries the epoch, and downstream components reject older epochs.
Raft terms and log positions, storage-primary generations, scheduler epochs,
and partition-assignment generations are examples of the same pattern.

Election is not enough when the old leader can still reach a database or
device. "Step down on disconnect" is good behavior; downstream fencing is the
safety mechanism for a pause or delayed request.

---

# 18. Sometimes the Lock Is the Wrong Abstraction

Before introducing a lock service, ask whether the invariant can move into the
resource that owns the data.

## Prefer a Unique Constraint for Deduplication

~~~sql
INSERT INTO report_runs(report_id, period, status)
VALUES ('sales', '2026-07', 'STARTED')
ON CONFLICT (report_id, period) DO NOTHING;
~~~

The unique key directly encodes "one logical run." It survives retries without
depending on a lease lasting for the whole computation.

## Prefer Compare-and-Swap for State Transitions

~~~sql
UPDATE jobs
SET state = 'RUNNING', owner = :worker
WHERE id = :id AND state = 'READY';
~~~

The row is both the work item and arbitration point. There is no gap between
acquiring a separate lock and changing the resource.

## Prefer Partition Ownership for Stream Work

A broker consumer group can assign each partition to one member and attach a
generation to commits. This is a specialized leased-ownership protocol rather
than a lock around every message.

## Prefer Idempotency for Repeatable Effects

If operation `publish(report_id, content_hash)` is idempotent, duplicate workers
may waste computation without corrupting state. A best-effort lock can then be
an optimization rather than the correctness boundary.

![Constraints, CAS, partition ownership, and idempotency can replace locks](/assets/img/distributed-locks/alternatives.svg)

The simplest safe lock is often no lock at all: express the invariant where the
data changes.

---

# 19. Granularity, Contention, and Fairness

One global lock is easy to reason about and terrible for concurrency. One lock
per record can overwhelm the coordination service.

~~~text
global          /locks/reports
tenant          /locks/reports/acme
report          /locks/reports/acme/monthly-sales
subresource     /locks/reports/acme/monthly-sales/2026-07
~~~

![Coarser locks simplify ordering while finer locks increase concurrency](/assets/img/distributed-locks/granularity.svg)

Choose the largest scope that still permits required parallelism. If an
operation needs multiple locks, define one global acquisition order. Otherwise
two clients can deadlock:

~~~text
W1 holds A, waits for B
W2 holds B, waits for A
~~~

Fair queues avoid starvation but add coordination and can suffer convoying when
the next contender is slow. Wake one successor rather than every waiter where
the service supports ordered predecessor watches. Add randomized backoff to
polling designs to avoid a thundering herd.

Long queues are usually a capacity signal, not a reason to increase TTL. Measure
acquisition wait, hold duration, renewal latency, timeout rate, and queue depth
by resource class.

---

# 20. Failure Matrix

The system is easier to review when each failure has an explicit response.

![Failure modes and the mechanisms that contain them](/assets/img/distributed-locks/failure-matrix.svg)

| Failure | Risk | Required response |
|---|---|---|
| Holder crashes | Lock remains forever | Session or lease expiry |
| Holder pauses past TTL | Old work resumes | Resource-enforced fencing |
| Acquire reply is lost | Client does not know ownership | Stable request identity and lookup |
| Release is delayed | Old release removes new lock | Compare exact lease identity |
| Renewal reply is lost | Client's validity is uncertain | Stop admission, resolve, then lose safely |
| Authority loses quorum | No safe new order | Fail acquisition closed; existing clients become suspect |
| Resource is partitioned | Mutations arrive after handoff | Persist and check fence at resource |
| Lock-service state rolls back | Two generations may appear current | Consensus durability matching the safety requirement |
| Fence counter resets | Old token can look new | Durable, scoped monotonic generation |
| All waiters poll together | Coordination overload | Predecessor watches, backoff, bounded queues |

Notice that many rows are not solved by "set a longer TTL."

---

# 21. Operational Design

## Metrics

Track at least:

- acquisition attempts, successes, failures, and latency;
- queue length and oldest waiter age;
- lease hold duration and renewal round-trip latency;
- remaining TTL when renewals are acknowledged;
- transitions into `SUSPECT` and `LOST`;
- expired leases and explicit releases;
- rejected stale fencing tokens;
- work cancelled after lease loss;
- per-key contention and top lock holders;
- coordination quorum health and clock anomalies.

A rising count of rejected stale tokens is evidence that fencing is working,
but also that pauses, network delays, or client shutdown behavior need
investigation.

## Timeouts

Bound acquisition waits and work admission. A request that cannot acquire a
lock before its caller's deadline should leave the queue cleanly. Renewal
timeouts must be shorter than the remaining safe-work margin.

## Overload

Do not allow unlimited waiters, watches, or retry loops. A hot resource can turn
one application bottleneck into a coordination-service outage. Apply per-key
limits, admission control, and backoff.

## Security

Authorize clients by lock namespace and protected operation. Treat lease IDs as
identifiers, not bearer authorization. Audit forced unlocks and epoch resets;
they can bypass normal ownership history.

---

# 22. Test the Uncomfortable Interleavings

Happy-path tests prove very little. A useful test harness can pause processes
and delay individual messages.

Test these sequences:

1. pause `W1` after reading but before writing;
2. let its lease expire and allow `W2` to publish;
3. resume `W1` and verify the store rejects its smaller fence;
4. drop the successful acquisition reply and retry with the same request ID;
5. delay an old release until after reacquisition;
6. lose several renewal replies without stopping the work thread;
7. partition the client from the authority but not the resource;
8. partition the resource, queue commands, then deliver them out of order;
9. fail the coordination leader around grant and renewal commits;
10. restart the authority and verify the next fence cannot move backward;
11. create thousands of waiters on one key and observe wake-up behavior;
12. force clock adjustments where the chosen lease algorithm permits them.

Verify the protected-resource history, not merely the ownership records. A test
that ends after "W2 acquired" has not tested fencing.

---

# 23. The Complete Report Handoff

Return to `report/2026-07` with the full design:

1. `W1` calls `Acquire(resource, request-81)`.
2. The consensus-backed authority grants lease `L-81` with fence 41.
3. `W1` reads the report and starts computation.
4. `W1` pauses; its renewal acknowledgements stop.
5. The authority expires `L-81` and removes its ownership claim.
6. `W2` acquires lease `L-82` with fence 42.
7. `W2` publishes using an atomic `fence < 42` condition.
8. The store persists report version `v12` and greatest fence 42.
9. `W1` resumes and attempts publication with fence 41.
10. The store rejects it as `stale_owner` without changing the report.
11. `W1` observes lease loss, discards its result, and emits a diagnostic event.
12. `W2` releases `L-82` by exact lease identity.

![The end-to-end handoff remains safe when the old worker resumes](/assets/img/distributed-locks/end-to-end.svg)

The authority restored liveness. The resource preserved safety. The client
reduced unnecessary conflict by stopping when it learned of lease loss. All
three behaviors were necessary.

---

# 24. Decision Guide

| Situation | Default mechanism |
|---|---|
| Prevent the same request from creating two rows | Unique constraint or idempotency key |
| Claim database jobs | Transactional state change or `SKIP LOCKED` |
| Avoid duplicate disposable computation | Best-effort TTL lock may suffice |
| Coordinate writes within etcd | etcd lock/lease plus transaction revision check |
| Order ZooKeeper contenders | Ephemeral sequential recipe plus predecessor watch |
| Protect an external correctness-critical resource | Linearizable acquisition plus resource-enforced fencing |
| Elect a controller | Session/lease-backed election plus command epoch |
| Resource cannot validate a fence | Redesign around its transaction, version, or idempotency boundary |

When evaluating any implementation, ask:

1. What exact invariant requires serialization?
2. Is the lock for correctness or only efficiency?
3. What creates one order of acquisitions?
4. What ends ownership after a crash?
5. What happens during a long process pause?
6. Which value uniquely identifies this lease?
7. Which value monotonically orders ownership generations?
8. Where is that value checked atomically with the side effect?
9. Can an old release or renewal affect a new holder?
10. How are uncertain RPC outcomes resolved?
11. Can a constraint, transaction, CAS, or idempotency key remove the lock?
12. What history will the failure tests inspect?

---

# 25. Final Mental Model

The mechanisms now fit together:

~~~text
consensus or transaction -> one order of ownership grants
session or TTL           -> eventual cleanup and takeover
lease identity           -> renew/release only this claim
fencing token            -> ordered ownership generations
resource validation      -> stale work cannot mutate state
idempotency/versioning   -> duplicate and concurrent effects remain controlled
~~~

A lease is a liveness mechanism with timing semantics. A fencing token is an
ordering mechanism. A lock client is a cooperative participant. The protected
resource is the final authority over whether a side effect is accepted.

That is why a correct distributed lock is not one API call. It is a protocol
whose safety boundary extends all the way to the resource being protected.

---

# References

- [Apache ZooKeeper Recipes and Solutions](https://zookeeper.apache.org/doc/current/recipes.html)
- [etcd v3.6 API overview](https://etcd.io/docs/v3.6/learning/api/)
- [etcd concurrency API reference](https://etcd.io/docs/v3.6/dev-guide/api_concurrency_reference_v3/)
- [etcd comparison with other key-value stores](https://etcd.io/docs/v3.6/learning/why/)
- [Redis: Distributed Locks with Redis](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/)
- [Martin Kleppmann: How to Do Distributed Locking](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html)
