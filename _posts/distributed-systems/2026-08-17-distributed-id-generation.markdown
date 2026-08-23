---
layout: single
comments: true
title: "Distributed ID Generation: Snowflake, Sequences, UUIDs, and Clock Failure"
date: 2026-08-17 06:00:00+0100
description: "A connected order-ID story explaining sequences, UUIDs, Snowflake layouts, replicated generator services, worker-allocation consensus, clock rollback, ordering, and failure safety."
tags: [distributed-ids, snowflake, uuid, ulid, sequences, clocks, databases, distributed-systems]
categories: ['Distributed Systems Components']
---

# 1. Two Orders Receive the Same ID

At 09:00, checkout instance `W17` creates order ID `725001937144832`. During a
deployment, a cloned virtual machine starts with the same configured worker ID
and an empty sequence counter. Its clock reads the same millisecond. It creates
the same number for a different order.

~~~text
W17 original   timestamp 1723885200123 | worker 17 | sequence 0
W17 clone      timestamp 1723885200123 | worker 17 | sequence 0
               ----------------------------------------------
               identical fields -> identical ID
~~~

The database's unique constraint rejects one insert, but an event carrying the
duplicate ID may already be in a queue. Logs now appear to describe one order,
and a retry for the rejected checkout may fetch the other customer's result.

![A cloned worker produces the same fields and therefore the same ID](/assets/img/distributed-id-generation/story-overview.svg)

The bit packing was correct. The deployment invariant was not.

This article follows one order ID through increasingly distributed designs:

1. a database sequence gives one authoritative order;
2. range allocation removes the per-ID round trip;
3. random and time-ordered 128-bit IDs remove worker assignment;
4. a Snowflake-style layout fits sortable IDs into 64 bits;
5. worker leases, clock rollback, and exhaustion reveal the operational cost;
6. storage, privacy, transport, migration, and testing complete the design.

---

# 2. “Unique” Is Only the First Requirement

An identifier scheme may need several independent properties.

![ID requirements pull the design in different directions](/assets/img/distributed-id-generation/requirement-space.svg)

| Property | Precise question |
|---|---|
| Uniqueness | Can two logical objects ever receive the same value in the required domain? |
| Availability | Can a node generate while disconnected from a coordinator? |
| Throughput | How many IDs can one node and the whole fleet generate? |
| Sortability | Do newer IDs usually sort after older IDs? |
| Total order | Can every pair of creations be placed in one authoritative order? |
| Compactness | Must the value fit a signed 64-bit column or protocol? |
| Opacity | Does the value reveal time, volume, topology, or business sequence? |
| Locality | How does insertion affect B-tree pages, caches, and shards? |
| Recoverability | What state must survive process and machine restart? |

No common scheme maximizes all of them. A central sequence gives a strong order
but requires coordination. Random UUIDs generate anywhere but carry no useful
creation order. Snowflake-style IDs are compact and locally generated, but
their correctness depends on time and worker identity.

Start with the contract, not the fashionable format.

---

# 3. Identifier, Idempotency Key, and Business Number

These values are often confused:

~~~text
order_id          identity of the stored order
idempotency_key   identity of the client's logical create request
invoice_number    regulated or customer-visible business sequence
trace_id          identity of one observability trace
~~~

![Object identity, request deduplication, and business numbering are separate](/assets/img/distributed-id-generation/idempotency-vs-identity.svg)

Generating a new unique order ID on every retry does not prevent duplicate
orders. The service must bind a stable idempotency key to one result:

~~~sql
CREATE UNIQUE INDEX one_checkout_result
ON checkout_requests(tenant_id, idempotency_key);
~~~

Likewise, a gapless invoice number usually belongs to a committed accounting
workflow, not a high-throughput ID generator. Rollbacks, abandoned ranges, and
crashes naturally create gaps in scalable generators.

One value can serve several roles only if their contracts truly align.

---

# 4. A Database Sequence Is the Simplest Strong Authority

A database sequence atomically returns a distinct number to concurrent callers:

~~~sql
CREATE SEQUENCE order_id_seq AS BIGINT;

INSERT INTO orders(id, tenant_id, status)
VALUES (nextval('order_id_seq'), 'acme', 'PENDING');
~~~

![Concurrent writers serialize ID allocation at one database sequence](/assets/img/distributed-id-generation/sequence-path.svg)

Advantages:

- simple uniqueness inside the database cluster;
- compact integer keys;
- no clock dependency;
- one obvious place to inspect allocation state;
- easy integration with the row insert.

Costs:

- every uncached allocation reaches the sequence authority;
- multi-region generation inherits database latency and availability;
- one global sequence can become a contention or operational boundary;
- cross-database migration needs explicit namespace planning.

If all objects are created in one database and its throughput is sufficient,
this is often the correct design. Distribution should solve a demonstrated
constraint, not merely remove a short SQL call.

---

# 5. Sequence Order Is Not Commit Order

Two transactions can obtain IDs in this order:

~~~text
T1 receives 101, then performs slow validation
T2 receives 102, then commits immediately
T1 commits later
~~~

![Sequence issue order and transaction commit order can differ](/assets/img/distributed-id-generation/issue-vs-commit.svg)

Queries ordered by ID will show `101` before `102`, although order 102 became
visible first. An allocated value can also disappear when its transaction
aborts. PostgreSQL explicitly does not reclaim `nextval` values, so sequences
are not gapless.

Separate these statements:

~~~text
ID allocation order != transaction commit order
ID numerical order  != causality between business events
no duplicate IDs    != no missing numbers
~~~

If commit order matters, record a commit log position, database timestamp under
a defined isolation rule, or a separate event sequence at the commit boundary.

---

# 6. Range Allocation Amortizes Coordination

Instead of requesting every ID, a node reserves a range:

~~~text
W1 receives [1,000,000, 1,009,999]
W2 receives [1,010,000, 1,019,999]
~~~

Each node then increments locally until its range is exhausted.

![A central allocator hands non-overlapping ranges to workers](/assets/img/distributed-id-generation/range-allocation.svg)

With range size `R`, one allocator request supports `R` IDs:

~~~text
allocator requests per second = ID rate / R
~~~

At one million IDs per second and `R = 10,000`, the allocator sees about 100
range requests per second rather than one million ID requests.

The allocator must persist the high-water mark before returning a range. A
transaction can atomically advance it:

~~~sql
UPDATE id_namespaces
SET next_value = next_value + 10000
WHERE namespace = 'orders'
RETURNING next_value - 10000 AS range_start,
          next_value - 1     AS range_end;
~~~

---

# 7. Ranges Trade Coordination for Waste and Ordering

If `W1` crashes after using only 27 values, the rest of its range must normally
remain unused. Reissuing the tail risks colliding with IDs that `W1` generated
but whose effects are delayed or stored outside the allocator's view.

![A crashed worker leaves a safe permanent hole in its allocated range](/assets/img/distributed-id-generation/range-failure.svg)

Large ranges:

- reduce allocator traffic;
- improve disconnected availability;
- increase gaps after failure;
- make numerical order diverge further from creation time.

`W2` may create ID 1,010,000 before `W1` creates 1,000,001. The ranges are
unique but not globally chronological.

Never reconstruct the high-water mark by scanning one database if IDs can be
published to several systems. Recovery must use the allocator's durable state,
and allocated ranges should not be reused.

---

# 8. UUIDv4 Removes the Coordinator

UUID version 4 is 128 bits. After version and variant fields, 122 bits are
random:

~~~text
example: 6ba7b812-9dad-4e9f-8b88-c4a19d13f230
                         ^ version 4 and variant are encoded in fixed positions
~~~

![UUIDv4 uses fixed format bits and 122 random bits](/assets/img/distributed-id-generation/uuid4-layout.svg)

Every process can generate locally. There is no worker registry, sequence
service, or clock requirement for uniqueness. Correctness depends on the
quality and independence of the random source.

UUIDv4 is useful when:

- decentralization and offline generation matter;
- 128-bit storage is acceptable;
- time ordering is unnecessary;
- opaque, hard-to-guess values are helpful;
- the implementation uses a cryptographically strong random generator.

Randomness does not make a UUID authorization. A leaked UUID may still grant
access if the application fails to check permissions.

---

# 9. Collision Probability Uses the Birthday Bound

For `n` uniformly random values from a space of size `N`, when collisions are
still unlikely:

~~~text
p(collision) ≈ n(n - 1) / (2N)
~~~

For UUIDv4, `N = 2^122`. At one trillion generated IDs:

~~~text
p ≈ 10^12 × (10^12 - 1) / (2 × 2^122)
  ≈ 9.4 × 10^-14
~~~

![Collision probability grows quadratically with the number generated](/assets/img/distributed-id-generation/birthday-bound.svg)

The calculation assumes uniform independent randomness. A broken random-number
generator, cloned VM state, repeated seed, or truncated representation can
dominate the mathematical probability.

Keep the database unique constraint. It is a cheap final invariant and turns a
theoretical collision or software bug into a visible error rather than silent
corruption.

---

# 10. UUIDv7 Adds Time Ordering to the UUID Format

RFC 9562 defines UUID version 7 with a 48-bit Unix timestamp in milliseconds in
the most significant bits. The remaining variable fields contain 74 bits that
may be random or may include carefully constructed sub-millisecond and counter
state.

![UUIDv7 places Unix milliseconds before version, variant, and random fields](/assets/img/distributed-id-generation/uuid7-layout.svg)

~~~text
48 bits unix_ts_ms | 4 version | 12 rand_a | 2 variant | 62 rand_b
~~~

Compared with UUIDv4, UUIDv7 values generated later usually sort later. This
improves index locality and time-range inspection while preserving the standard
128-bit UUID representation.

Important limits remain:

- two machines with skewed clocks can generate values out of real-time order;
- plain random fields do not promise monotonic order inside one millisecond;
- a monotonic implementation needs carefully defined counter state and overflow
  behavior;
- the timestamp is visible to anyone who receives the ID.

Use a well-reviewed RFC 9562 implementation rather than inventing bit handling.

---

# 11. ULID Is a 128-Bit Time-Prefixed Alternative

The canonical ULID format contains:

~~~text
48-bit Unix millisecond timestamp | 80-bit randomness
26 Crockford Base32 characters
~~~

![ULID encodes a time prefix and randomness as 26 sortable characters](/assets/img/distributed-id-generation/ulid-layout.svg)

The string representation is lexicographically sortable when encoded
canonically. The alphabet avoids visually confusing characters and is URL-safe.

Within one millisecond, the base specification does not make ordinary random
ULIDs creation-ordered. A **monotonic factory** increments the random component
for repeated timestamps. It must fail rather than wrap if that component
overflows.

ULID and UUIDv7 solve similar product needs with different standardization,
encoding, library, and database-type trade-offs. UUIDv7 fits native UUID
columns and RFC semantics. ULID offers a compact, familiar 26-character text
form. Store either as 16 binary bytes when possible rather than expanding every
index entry to text.

---

# 12. Sortability Is Not One Property

At least four ordering claims appear in ID discussions:

![Local monotonicity, approximate time order, and total order are distinct](/assets/img/distributed-id-generation/ordering-levels.svg)

| Claim | Meaning |
|---|---|
| Per-generator monotonic | One generator's next ID is numerically greater |
| Millisecond sortable | High bits group IDs by observed millisecond |
| Globally roughly time ordered | Clock skew may reorder a bounded neighborhood |
| Strict total order | One authority orders every generation event |

UUIDv7, ULID, and Snowflake-style IDs are normally **time sortable**, not
proofs of causality. If event B causally follows event A but B's host clock is
behind, B's ID may sort first.

An ID allocated before a transaction also cannot describe commit order. Use a
consensus log index, database log position, or Hybrid Logical Clock when that
ordering is the actual requirement.

---

# 13. Snowflake Packs Time, Worker, and Sequence into 64 Bits

A classic Snowflake-style signed 64-bit layout is:

~~~text
0 | 41-bit timestamp | 10-bit worker | 12-bit sequence
~~~

![Snowflake bit layout with timestamp, worker, and per-millisecond sequence](/assets/img/distributed-id-generation/snowflake-layout.svg)

The original Twitter design used milliseconds since a custom epoch, 1,024
worker identities, and 4,096 sequence values per worker per millisecond.

An ID is composed as:

~~~text
id = ((timestamp_ms - epoch_ms) << 22)
   | (worker_id << 12)
   | sequence
~~~

![Timestamp, worker, and sequence fields combine into one integer](/assets/img/distributed-id-generation/snowflake-compose.svg)

The fields create uniqueness only if:

~~~text
for one timestamp, no two live generators use the same worker ID
for one worker and timestamp, sequence never repeats
timestamp never moves into a previously used tuple without protection
~~~

Those are operational invariants, not consequences of bitwise shifts.

---

# 14. The Bit Budget Is a Capacity Contract

For `T` timestamp bits at resolution `u`, `W` worker bits, and `S` sequence bits:

~~~text
lifetime                 = 2^T × u
maximum workers          = 2^W
IDs per time unit/worker = 2^S
fleet theoretical rate  = 2^W × 2^S / u
~~~

![Changing one field's width takes capacity from another field](/assets/img/distributed-id-generation/capacity-budget.svg)

For `41/10/12` at one millisecond:

~~~text
lifetime          ≈ 69.7 years from the custom epoch
workers           = 1,024
per worker        = 4,096 IDs/ms
theoretical fleet = 4,194,304 IDs/ms
~~~

The theoretical fleet number ignores CPU, locking, clock calls, serialization,
network, and downstream storage. The per-worker ceiling is the immediate
generator constraint.

Choose the epoch before launch and document its exhaustion date. Reclaiming the
sign bit or changing field widths later changes interoperability and comparison
semantics.

---

# 15. One Generator Is a State Machine

Each worker stores at least:

~~~text
worker_id
last_timestamp
sequence_for_last_timestamp
assignment_generation or lease state
~~~

The transition is:

![Snowflake generator transitions for new, same, and backward milliseconds](/assets/img/distributed-id-generation/worker-state-machine.svg)

~~~text
now > last_timestamp:
    last_timestamp = now
    sequence = 0

now == last_timestamp:
    sequence += 1
    if sequence overflows: wait or reject

now < last_timestamp:
    invoke explicit rollback policy
~~~

A compact composition function can make the range checks visible:

~~~cpp
std::uint64_t compose(std::uint64_t timestampMs,
                      std::uint16_t workerId,
                      std::uint16_t sequence) {
    if (timestampMs < kEpochMs || workerId >= (1u << 10) ||
        sequence >= (1u << 12)) {
        throw std::invalid_argument("ID field out of range");
    }

    const auto elapsed = timestampMs - kEpochMs;
    if (elapsed >= (1ULL << 41)) {
        throw std::overflow_error("ID epoch exhausted");
    }

    return (elapsed << 22) |
           (static_cast<std::uint64_t>(workerId) << 12) |
           sequence;
}
~~~

Generation for one worker must be serialized or use an atomic state transition.
Two threads reading the same `last_timestamp` and sequence can otherwise emit
the same ID.

---

# 16. Sequence Exhaustion Must Not Wrap

With 12 sequence bits, a worker has values `0..4095` in one millisecond. The
4,097th request cannot reuse zero.

![The 4097th request waits for a later timestamp instead of wrapping](/assets/img/distributed-id-generation/sequence-exhaustion.svg)

Possible responses:

- wait until the observed timestamp advances;
- return backpressure and let a caller retry;
- allocate more sequence bits in the original design;
- distribute load across more safely assigned workers;
- use finer timestamp resolution, sacrificing lifetime bits.

Busy-waiting consumes CPU and can hang indefinitely if the clock is pinned
during rollback handling. Bound the wait, expose an exhaustion metric, and
propagate a meaningful unavailable error.

Capacity tests must concentrate calls in one worker and one time unit. An
average-rate load test will miss this boundary.

---

# 17. Worker Identity Is a Distributed Coordination Problem

Static configuration is simple until machines are cloned, autoscaled, restored
from images, or moved between environments. Deriving identity from an IP
address, process ID, or container ordinal can repeat after restart.

![A worker registry assigns one identity to one live generator](/assets/img/distributed-id-generation/worker-assignment.svg)

A registry can allocate from the worker-ID space and bind the assignment to a
session or lease:

~~~text
worker slot       17
assignment        gen-884
holder            pod checkout-7f9c
lease expires     authority time 09:05:30
~~~

The generator must stop before it can no longer prove the assignment is valid.
But a lease alone does not physically stop a paused old process. Reassigning
slot 17 while that process can resume recreates the opening collision.

---

# 18. Worker-ID Reuse Is Harder Than Lock Handoff

Suppose old `W17` pauses, its assignment expires, and a new process receives
worker ID 17. Both may generate the same timestamp/sequence tuple when the old
process resumes.

![Reusing a worker slot can overlap an old paused generator](/assets/img/distributed-id-generation/worker-reuse.svg)

Unlike a fenced database write, consumers normally accept the ID alone. The
classic 64-bit layout contains no assignment generation for a consumer to
validate. Safe strategies include:

- do not reuse worker IDs within the system's operational horizon;
- persist and restore per-worker timestamp/sequence state on durable storage;
- quarantine a slot until the old process is proven terminated and its delayed
  output cannot arrive;
- reserve bits for a boot or assignment generation;
- route generation through a service that owns stable worker slots;
- carry and validate assignment generation outside the ID at every sink.

"Stop when the lease is lost" reduces risk but is not a complete proof against
a long process pause. The deployment and consumer protocol must close the gap.

---

# 19. Clock Rollback Is a Safety Event

Wall clocks can move backward because of synchronization corrections, operator
error, virtualization, restored snapshots, hardware issues, or incorrect time
configuration.

![A backward clock reading enters an already-used timestamp range](/assets/img/distributed-id-generation/clock-rollback.svg)

If `last_timestamp = 12,500` and the next clock read is 12,497, resetting the
sequence at 12,497 may reproduce an earlier tuple.

Common policies are:

| Policy | Safety and availability trade-off |
|---|---|
| Refuse generation | Safe and explicit; unavailable until time catches up |
| Wait for small rollback | Simple for bounded skew; still needs a maximum wait |
| Pin logical timestamp to last value | Continues until sequence space exhausts |
| Persist last timestamp across restart | Prevents forgotten history; adds durable state |
| Encode rollback/boot generation | Consumes bits and needs unique generation assignment |
| Ask a time/ID authority | Stronger coordination; generation loses full locality |

A monotonic process clock measures elapsed duration but does not by itself
provide a persistent epoch across restart. A robust implementation can combine
wall time, monotonic elapsed time, persisted high-water state, and explicit
startup checks.

Never silently mask a large rollback. Alert and stop before uniqueness becomes
probabilistic.

---

# 20. Multiple Regions Do Not Share One Clock

Assume `eu-west` is 4 ms ahead of `us-east`:

~~~text
real time 10:00:00.004  Europe creates E with encoded time 008
real time 10:00:00.006  US creates U with encoded time 002
numeric order: U before E
real-time order: E before U
~~~

![Clock skew reverses the numeric order of cross-region IDs](/assets/img/distributed-id-generation/regional-ordering.svg)

Time-prefixed IDs provide an approximate global grouping whose error is at
least the clock uncertainty plus generation and delivery delay. Worker bits
also determine arbitrary ordering for IDs in the same millisecond.

Do not use Snowflake, UUIDv7, or ULID comparison as a last-write-wins rule for
distributed data unless this clock model is acceptable. Causality needs version
vectors, logical clocks, HLCs, or an ordered log depending on the requirement.

---

# 21. ID Shape Changes Database Behavior

In a B-tree, random UUIDv4 inserts land across the key range. Time-prefixed IDs
mostly target recent pages.

![Random and time-ordered IDs create different B-tree insertion patterns](/assets/img/distributed-id-generation/index-locality.svg)

Time locality can improve cache use and reduce random page modification. It can
also concentrate writes on the rightmost page or one range shard. A distributed
database that range-partitions by primary key may send all new IDs to one
tablet.

Possible responses:

- hash-shard independently of the display/order key;
- prefix a controlled shard value before time;
- use database-managed automatic range splitting;
- keep the time-ordered ID as a secondary index rather than partition key;
- measure page splits, fill factor, write amplification, and hot partitions.

Store 128-bit values in a native UUID or 16-byte binary type when possible.
Text representation increases primary and secondary index size.

---

# 22. IDs Can Leak Time, Volume, and Topology

A sequential ID reveals approximate object count and enables enumeration.
Snowflake IDs normally reveal creation time, worker bits, and per-millisecond
sequence. UUIDv7 and ULID reveal a millisecond timestamp.

![Different ID formats expose different embedded information](/assets/img/distributed-id-generation/information-leakage.svg)

This may expose:

- business growth or transaction volume;
- object creation time before it is public;
- infrastructure or region allocation;
- burst patterns;
- existence of neighboring resources.

Do not place sensitive semantics such as tenant tier or database shard in a
public ID merely because bits are available. Use an opaque public token when
enumeration or metadata leakage matters, and map it to an internal sortable ID.

Random IDs are not access controls. Every object lookup still requires
authorization.

---

# 23. A 64-Bit Integer Does Not Fit Every Client Runtime

JavaScript's ordinary `Number` represents integers exactly only through
`2^53 - 1`. Modern Snowflake values exceed that range.

![A 64-bit ID loses precision when forced through a 53-bit number](/assets/img/distributed-id-generation/transport-width.svg)

~~~json
{
  "order_id": "725001937144832001"
}
~~~

Serialize large integer IDs as decimal strings across JSON unless every client
uses an explicit 64-bit or big-integer representation. Define protobuf fields,
database signedness, language types, and textual parsing consistently.

Beware of:

- signed versus unsigned comparison;
- accidental floating-point conversion;
- ORM schemas that choose 32-bit integers;
- CSV and spreadsheet formatting;
- leading-zero loss in textual formats;
- lexicographic sorting of variable-width decimal strings.

An ID format is an end-to-end protocol, not only a server-side integer.

---

# 24. A Generator Service Centralizes Operational Complexity

Instead of embedding clock and worker logic in every application process,
deploy a small generator tier. Each generator owns a stable worker identity and
serves batches of IDs over RPC.

![A generator service hides worker assignment and clock policy from applications](/assets/img/distributed-id-generation/service-architecture.svg)

Advantages:

- one audited implementation;
- stable worker lifecycle and durable high-water state;
- easier clock monitoring and rollback policy;
- language-independent clients;
- applications can request batches to amortize RPC cost.

Costs:

- another latency and availability dependency;
- service discovery and load balancing;
- batch waste on client failure;
- careful retry semantics after an uncertain response;
- generators still need unique worker assignments.

An RPC timeout does not reveal whether a batch was allocated. Give each batch
request a stable idempotency key or permit safe waste by never reissuing an
uncertain batch.

---

# 25. The Generator Tier Must Not Recreate a Singleton

A generator service should be a fleet, not one process behind a new network
address. Clients call any healthy replica through discovery or a load balancer.
Each generator has a distinct worker assignment and generates independently on
the common path.

![A replicated generator tier preserves disjoint worker ownership](/assets/img/distributed-id-generation/generator-ha.svg)

For Snowflake-style IDs, availability comes from **disjoint authority** rather
than having every replica mutate one shared counter:

~~~text
generator G17 -> worker slot 17 -> its own timestamp/sequence state
generator G23 -> worker slot 23 -> its own timestamp/sequence state
generator G41 -> worker slot 41 -> its own timestamp/sequence state
~~~

If `G17` fails, callers move to `G23` or `G41`. Their IDs remain unique because
the worker bits differ. They do not take over slot 17 immediately. Abandoning a
slot preserves safety; eager reuse turns failover into a collision risk.

The worker allocator is a separate control plane. It must keep one durable,
replicated assignment history. A safe allocator uses a consensus-backed log or
transactional store and maintains:

> At most one unexpired assignment generation for a worker slot may be
> accepted by generators and downstream validators.

![Worker allocation failover must not assign one slot twice](/assets/img/distributed-id-generation/worker-allocator-ha.svg)

The allocator leader records `(slot=17, holder=G17, generation=884)` on a
quorum before returning it. If the reply is lost, `G17` retries with the same
request ID and receives the same assignment instead of consuming another slot.
After allocator leader failure, a new leader recovers every live assignment
before issuing any free slot.

An isolated old allocator must not allocate. Its term fences stale grants. An
isolated generator may continue only while its assignment contract permits and
its clock/high-water checks remain safe; once it cannot prove that, it stops.

## 25.1 Separate the Failure Domains

The complete service has different failure consequences:

| Failure | Safe behavior |
|---|---|
| one generator process dies | route new requests to another worker; leave uncertain batch unused |
| generator restarts | recover durable high-water state or obtain a new non-overlapping worker generation |
| allocator leader dies | existing assignments continue; elect from committed assignment history |
| allocator loses quorum | no new worker assignments; existing valid generators may continue |
| one region is isolated | use region-disjoint worker space or stop when regional assignment authority expires |
| clock service or local clock is unsafe | stop the affected generator, not the entire fleet |

Preallocating worker-ID ranges per region removes a cross-region allocator call
from startup and ensures two regions cannot choose the same worker bits. The
tradeoff is stranded ID space and an explicit process for transferring a range
between regions without overlap.

Database-sequence and range-allocation designs need the same treatment at a
different boundary. The sequence authority must be durably replicated, and an
acknowledged range must never be handed out again after failover. Applications
can continue consuming already allocated ranges while the authority is down,
but cannot safely obtain new ones.

The result contains logical authorities—the worker allocator, the per-worker
state machine, or the sequence authority—without requiring any one physical
machine to remain alive.

---

# 26. Failure Matrix

![ID-generation failures and the mechanisms that contain them](/assets/img/distributed-id-generation/failure-matrix.svg)

| Failure | Risk | Containment |
|---|---|---|
| Database sequence unavailable | Creation stops | Fail over sequence authority or use preallocated ranges |
| Generator node unavailable | RPC generation pauses | Route to another independently assigned worker |
| Worker allocator leader fails | New workers cannot start briefly | Elect from quorum-committed assignment history |
| Worker allocator loses quorum | Duplicate slot allocation | Reject new assignments; existing valid workers continue |
| Range holder crashes | Unused gap | Never reuse abandoned range |
| RNG repeats after clone | UUID collision | OS CSPRNG, reseeding guarantees, unique constraint |
| Two workers share one ID | Same tuple emitted | Strong assignment invariant and reuse policy |
| Clock moves backward | Old timestamp range reused | Refuse, wait, pin with bound, or persistent generation state |
| Sequence overflows in one tick | Counter wraps | Wait/backpressure; never wrap |
| Generator restarts empty | Forgets last timestamp/sequence | Durable high-water or safe startup barrier |
| Epoch expires | Timestamp field wraps | Versioned migration planned years in advance |
| Allocation reply is lost | Range outcome uncertain | Idempotent request or permanently abandon uncertain range |
| 64-bit value enters JavaScript Number | Precision collision | Serialize as string or BigInt-aware protocol |
| Random/time key becomes storage hotspot | Index or shard overload | Choose partition key separately and measure locality |

---

# 27. Operations and Observability

Track:

- IDs generated per second by namespace and worker;
- highest timestamp and sequence utilization;
- sequence-exhaustion waits and rejected generations;
- observed clock offset, rollback events, and maximum rollback;
- worker assignment, renewal, conflict, and reuse events;
- allocator term, quorum health, commit latency, and free-slot capacity;
- generator instances quarantined because assignment or clock safety is unknown;
- range allocation and abandoned-range counts;
- generator RPC latency, timeout, and batch size;
- database uniqueness violations;
- timestamp decode lag between generated ID and ingestion;
- remaining epoch lifetime;
- policy and layout version in use.

Do not emit every generated ID as a metric label. Use structured logs and
sampled traces for individual diagnosis.

On startup, a Snowflake worker should report its assigned worker identity,
assignment generation, persisted high-water timestamp, current clock, and
decision to start or quarantine. Startup safety deserves the same visibility as
steady-state throughput.

---

# 28. Test the Boundaries, Not Only a Million Happy IDs

Property tests should assert uniqueness and field ranges across concurrent
generators. A controllable clock and worker registry make the important cases
deterministic.

Test:

1. many threads at the same millisecond;
2. exactly 4,096 and then 4,097 requests for a 12-bit sequence;
3. clock moves backward by 1 ms and by minutes;
4. restart with wall time below the persisted high-water mark;
5. two processes receive the same worker ID;
6. assignment lease expires while the old process is paused;
7. delayed IDs arrive after worker-ID reuse;
8. range allocation commits but its response is lost;
9. generator crashes after reserving a batch;
10. random generator is deliberately seeded identically on two workers;
11. timestamp and sequence fields reach their maximum values;
12. IDs cross JSON, protobuf, database, logging, and browser clients;
13. lexicographic and binary sort order match the documented format;
14. B-tree and sharded-storage behavior under realistic skew;
15. migration accepts old and new formats concurrently;
16. allocator leader fails before and after committing an assignment;
17. allocator loses quorum while existing workers continue;
18. stale allocator term attempts to assign an occupied slot;
19. generator service retry does not reuse an uncertain batch.

Keep a database unique constraint even after stress tests pass.

---

# 29. Migrating an ID Format

Changing primary identifiers touches databases, caches, URLs, messages,
analytics, and external clients.

![A versioned migration carries old and new IDs across every consumer](/assets/img/distributed-id-generation/migration.svg)

A safe migration commonly uses:

1. add a new nullable ID column with a unique index;
2. write both formats for new rows;
3. backfill old rows with stable mappings;
4. teach readers and events to carry both;
5. move foreign keys and caches deliberately;
6. switch public producers only after consumers understand the new format;
7. retain lookup aliases for old URLs and external references;
8. verify no precision-losing clients remain;
9. stop old writes, then remove old dependencies later.

Do not decode business meaning from an ID unless the format version is known.
Reserve an explicit version boundary in the protocol even if the current bits
do not contain a version field.

---

# 30. The Complete Order-ID Journey

Suppose the final design chooses UUIDv7 for external order identity and a
database log position for strict event order:

1. The client sends idempotency key `checkout-9f2`.
2. The checkout service looks up that key before creating anything.
3. A reviewed UUIDv7 library generates a 128-bit ID using current milliseconds
   and safe random/counter state.
4. The service inserts the order and idempotency mapping in one transaction.
5. A unique constraint is the final collision defense.
6. Commit assigns an outbox/log position independent of the UUID.
7. Events carry the UUID as object identity and the log position for ordering.
8. The API serializes the UUID in canonical text.
9. The database stores it in a native 16-byte UUID type.
10. Authorization checks tenant ownership; the UUID is not a credential.
11. Retries with `checkout-9f2` return the original order and UUID.
12. Cross-region consumers order causal streams by the log protocol, not by
    comparing UUID timestamps.

![The final design separates identity, deduplication, and event order](/assets/img/distributed-id-generation/end-to-end.svg)

If compact 64-bit IDs were mandatory instead, the same workflow would add an
operated Snowflake generator, worker-assignment safety, clock rollback policy,
and string serialization for JavaScript clients.

---

# 31. Selection Guide

| Requirement | Useful starting point |
|---|---|
| One database, compact integer key | Database sequence |
| Fewer allocator calls, gaps acceptable | Durable range allocation |
| Fully local opaque generation | UUIDv4 |
| Standard time-ordered 128-bit value | UUIDv7 |
| Sortable 26-character text ecosystem | ULID |
| Compact 64-bit time-sortable value | Operated Snowflake-style generator |
| Strict global creation order | Consensus/database ordering authority |
| Gapless legal/business number | Assign at the committed business boundary, usually with serialization |
| Public identifier must hide time and volume | Random public token mapped to internal key |

For any design, ask:

1. What is the uniqueness domain: table, tenant, region, or all time?
2. Is approximate time sorting enough, or is strict order required?
3. Must generation continue without a coordinator?
4. How many bits and which client protocols are available?
5. What happens on rollback, restart, clone, and sequence exhaustion?
6. Who allocates worker identities, and when can one be reused?
7. Are gaps acceptable?
8. What information does the ID reveal?
9. How will the key affect indexes and partition hotspots?
10. What final uniqueness constraint detects an invariant violation?
11. How will the format migrate before its epoch or capacity expires?
12. Is an idempotency key or commit-order field also required?
13. How is the worker or sequence authority replicated and fenced?
14. Which generators continue when the allocator or a region is unavailable?

---

# 32. Final Mental Model

The mechanisms now separate cleanly:

~~~text
database sequence -> uniqueness and one allocation order through coordination
range allocation  -> local throughput by permanently spending number ranges
UUIDv4            -> probabilistic uniqueness from 122 random bits
UUIDv7 / ULID     -> 128-bit local generation with visible time prefix
Snowflake         -> compact time + worker + sequence under operational invariants
allocator quorum  -> one durable worker-assignment history without one machine
idempotency key   -> one logical create despite retries
commit/log order  -> authoritative ordering independent of object identity
~~~

An ID generator is small code sitting on top of large assumptions. The shifts,
masks, and random bytes are the easy part. Correctness depends on the uniqueness
domain, clock model, worker lifecycle, durable state, transport types, and the
meaning consumers assign to the resulting value.

---

# References

- [RFC 9562: Universally Unique IDentifiers](https://www.rfc-editor.org/rfc/rfc9562.html)
- [Canonical ULID specification](https://github.com/ulid/spec)
- [Twitter Snowflake archived repository](https://github.com/twitter-archive/snowflake)
- [PostgreSQL sequence manipulation functions](https://www.postgresql.org/docs/current/functions-sequence.html)
- [PostgreSQL CREATE SEQUENCE](https://www.postgresql.org/docs/current/sql-createsequence.html)
