---
title: "Designing a Distributed Top-K Heavy Hitters System"
date: 2026-09-01 09:00:00 +0000
description: "A technical design for maintaining Top-K heavy hitters under high write throughput: local ranking state, partition ownership, distributed merging, sliding windows, hot-key splitting, checkpointing, failover, and serving guarantees."
categories:
  - "System Design"
tags: [system-design, heavy-hitters, top-k, streaming, sketches, analytics, resiliency]
---

# 1. Introduction

## Start With One Process

A Top-K heavy hitters system answers a narrow question:

```text
For a given scope and time window, which K keys have the largest counts?
```

The key may be a search term, song ID, API route, tenant ID, source IP prefix,
or cache key. The scope may be global, regional, per customer, per product
surface, or per endpoint. The count may represent events, bytes, cost units, or
failed requests.

The single-process version is easy to reason about. Events arrive one at a
time. The process normalizes the key, increments a counter, and updates a small
ranking structure.

```text
event -> normalize -> counter[key] += cost -> update ranking
```

For a fixed window, the state can be:

```text
counts: key -> count
top:    heap or ordered set over counts
```

For a sliding window, the process also needs expiration. Suppose the query is:

```text
top 10 keys in the last 5 minutes
```

At `19:05:00`, the active window is:

```text
[19:00:00, 19:05:00)
```

At `19:05:01`, the active window becomes:

```text
[19:00:01, 19:05:01)
```

Events from `19:00:00` are no longer part of the answer. Recomputing the whole
five-minute window every second would be wasteful, so the process divides time
into small slices called **buckets**. With one-second buckets, each bucket stores
the counts contributed during one second:

```text
bucket[19:04:58] = {A: 3, B: 7}
bucket[19:04:59] = {A: 1, C: 4}
```

The **active buckets** are the buckets currently inside the query window. The
**active counts** are the sum of those active buckets:

```text
active_counts[A] = sum of A across all buckets in the last 5 minutes
active_counts[B] = sum of B across all buckets in the last 5 minutes
```

The buckets are stored in a fixed-size array called a **ring** because the same
array slots are reused as time advances. For a five-minute window with
one-second buckets, the process needs about 300 slots. After slot 299, it wraps
back to slot 0 and overwrites it only after subtracting the old slot's counts
from `active_counts`.

A common single-process structure is:

```text
active_counts: key -> sum over active buckets
buckets[slot]: key -> count within that time slice
top:           ranking over active_counts
```

When a new event arrives, the process increments both the current bucket and
`active_counts`. When the oldest bucket leaves the window, the process
subtracts that bucket from `active_counts`, clears the bucket slot, and repairs
the ranking.

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/single-node-state.svg" alt="Single process Top-K state with ring buckets, active counters, and a ranking heap" caption="A bucket stores counts for one time slice. Active counts are the sum of buckets inside the window, and the Top-K index ranks those active counts." %}</center>
</div>

This is the baseline. Every distributed design is a way of splitting,
replicating, or approximating this state.

## The Moment One Process Is Not Enough

The model breaks when the event stream no longer fits on one machine.

Write throughput may exceed one process's CPU or network capacity. Distinct key
cardinality may exceed memory. A single process may not recover quickly enough
after a crash. A single ranking writer may not satisfy availability or regional
latency requirements.

At that point, the central question changes from:

```text
How do we update a counter and a heap?
```

to:

```text
Which worker owns each part of the counting state, and how is a globally
correct Top-K list assembled while workers fail, restart, and lag?
```

This is the core distributed-systems problem in Top-K heavy hitters. The small
published list is not the hard part. The hard part is maintaining enough
distributed state to make that list fresh, bounded, and recoverable.

---

# 2. Data Model

The event should contain only the fields required to place the update into the
right counter.

```json
{
  "event_id": "01J7T4KGMJ9NS8W3B6FMZP4QAJ",
  "event_time": "2026-09-01T19:00:02.481Z",
  "metric": "api_requests",
  "scope": "region=eu-west|route=/v1/search",
  "key": "tenant_42",
  "cost": 1
}
```

The aggregation key is usually:

```text
metric | scope | key
```

The window key is usually:

```text
metric | scope | window_start | window_size
```

The ranking key is usually:

```text
metric | scope | window_size
```

These are separate on purpose. The aggregation key decides where updates are
counted. The window key decides which time bucket is affected. The ranking key
decides which result is served to readers.

Every derived record should carry enough metadata to make replay safe:

```text
partition_id
partition_epoch
input_offset or sequence
window_start
window_end
normalization_version
topology_version
```

Without this metadata, retries and failover create ambiguous state. The system
may not know whether a published partial ranking is newer, older, duplicated,
or produced with incompatible rules.

---

# 3. The Basic Distributed Shape

A scalable Top-K system separates the write path from the read path.

The write path ingests events, partitions them, updates stateful workers, and
publishes ranking snapshots. The read path serves the latest complete snapshot.
Reads should not scan raw events or contact every stream worker.

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/distributed-architecture.svg" alt="Distributed Top-K architecture with ingestion, partitioned log, stateful workers, reducers, ranking store, query API, and checkpoint storage" caption="The write path owns counting and ranking state. The read path serves complete materialized ranking versions." %}</center>
</div>

The common components are:

- **Ingestion layer:** receives events and applies validation.
- **Durable log:** stores events in ordered partitions.
- **Stateful workers:** own partitions and maintain local counting state.
- **Reducers:** merge local Top-K summaries into scope-level rankings.
- **Ranking store:** keeps immutable ranking versions and a latest pointer.
- **Checkpoint store:** keeps recoverable worker and reducer state.
- **Query API:** serves the latest complete version for a ranking key.

The durable log is the source of recovery. A worker can lose memory and rebuild
from a checkpoint plus replay. A reducer can lose memory and rebuild from local
summaries or its own changelog. The ranking store is an output cache, not the
only copy of truth.

There is also a control plane. It does not count events on the hot path. It
defines the rules that workers and reducers use while counting:

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/data-control-plane.svg" alt="A Top-K data plane processes events through logs, workers, reducers, and APIs while a control plane manages metric definitions, ownership, split registry, windows, and versions" caption="Separate the event-counting data plane from the rule-changing control plane. Worker and reducer outputs should identify the control-plane versions they used." %}</center>
</div>

---

# 4. Partition Ownership

Partitioning is the first major design decision.

If all events for one scope go to one worker, then computing that scope's Top-K
is simple. That worker owns every key in the scope.

```text
partition = hash(metric, scope)
```

This fails when one scope is much hotter than others. A single region, tenant,
or route can overload one worker while the rest of the fleet is underused.

For high-throughput systems, partition by key within scope:

```text
partition = hash(metric, scope, key)
```

Now all events for a particular key still go to one owner, but a hot scope is
spread across many workers. Each worker can compute a local Top-K for the keys
it owns. A reducer then merges those local lists.

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/partition-ownership.svg" alt="Events partitioned by metric, scope, and key so each key has one counting owner and each scope is spread across workers" caption="Partitioning by metric, scope, and key gives each key one owner while allowing a hot scope to use many workers." %}</center>
</div>

This ownership rule is important because it makes exact distributed Top-K
possible without sending every key to the reducer.

If a key is owned by exactly one worker, and a key is not in that worker's local
Top-K, then at least K keys on the same worker have counts greater than or
equal to it. That key cannot be in the global Top-K. Therefore, the reducer can
compute the exact global Top-K for a scope by merging the local Top-K lists from
all workers that own keys for that scope.

For K = 10 and 200 workers, the reducer merges at most 2,000 candidates per
scope per publication interval, not every distinct key.

```text
worker local Top-K lists -> reducer candidate set -> global Top-K
```

The proof depends on single ownership of each key. It stops being true when a
single logical key is split across several workers.

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/merge-correctness-boundary.svg" alt="Single-owner keys can be merged exactly from local Top-K lists, while split logical keys require partial counts to be recombined first" caption="The reducer can merge local Top-K lists exactly only while each logical key has one owner. Split hot keys need a recombination step before final ranking." %}</center>
</div>

---

# 5. Maintaining Local Top-K

Each worker receives an ordered stream for its assigned log partitions. For
each active window, it maintains:

```text
active_counts[(scope, key)] -> count
bucket_counts[(bucket, scope, key)] -> count in the bucket
ranking[scope] -> Top-K over active_counts for that scope
```

The update path is:

```text
bucket = floor(event_time / bucket_size)

bucket_counts[(bucket, scope, key)] += cost
active_counts[(scope, key)] += cost
ranking[scope].update(key, active_counts[(scope, key)])
```

The expiration path runs when a bucket leaves the window:

```text
for each (scope, key, count) in expired_bucket:
    active_counts[(scope, key)] -= count
    if active_counts[(scope, key)] == 0:
        delete active_counts[(scope, key)]
    ranking[scope].update_or_remove(key)
```

There are several implementation choices for `ranking[scope]`.

An indexed heap supports efficient updates, but it is more complex to implement
correctly. An ordered map keyed by `(count, key)` is simple and supports update
by removing the old pair and inserting the new pair. A lazy heap appends new
`(count, key, version)` entries and discards stale entries when reading the
heap; this is simple but needs periodic compaction.

For exact local ranking, the worker still needs exact `active_counts` for all
keys it owns in the active window. The ranking structure only avoids sorting
all keys on every publish.

---

# 6. Publishing Local Summaries

Workers should not publish on every event. At high TPS that would move the
bottleneck from the counter update to the reducer.

A worker normally publishes a local summary:

```text
every publish_interval
or when local Top-K changes materially
or when a bucket expires
```

The summary is versioned:

```json
{
  "summary_id": "partition-17:epoch-4:seq-98211",
  "partition_id": 17,
  "partition_epoch": 4,
  "sequence": 98211,
  "metric": "api_requests",
  "scope": "region=eu-west|route=/v1/search",
  "window": "5m",
  "watermark": "2026-09-01T19:05:00Z",
  "items": [
    {"key": "tenant_42", "count": 992104},
    {"key": "tenant_81", "count": 620440}
  ]
}
```

The reducer keeps the latest accepted summary per `(partition_id, scope,
window)`. It ignores summaries with an older sequence or a stale partition
epoch. This makes publish retries idempotent.

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/local-summary-merge.svg" alt="Workers publish versioned local Top-K summaries, and the reducer keeps the latest summary per partition before merging candidates" caption="Reducers merge the latest local summary from each partition. Sequence numbers and epochs make retries and failover safe." %}</center>
</div>

The global ranking version is built from the union of latest local candidates:

```text
candidates = union(latest_summary[p].items for p in partitions_for_scope)
global_top_k = largest K candidates by count
watermark = min(latest_summary[p].watermark for p in partitions_for_scope)
```

The watermark is the minimum across contributing partitions because the global
ranking is only complete through the slowest required partition.

---

# 7. Sliding Windows

Window maintenance is where many Top-K systems become expensive.

The system needs a way to remove old events from the count. If the query is
"last five minutes" and the clock moves forward by one second, then one second
of old events leaves the answer and one second of new events enters it.

The direct but expensive approach is:

```text
every second:
    scan all events from the last five minutes
    rebuild all counts
    sort or repair Top-K
```

A real-time worker usually avoids that by storing counts in small time buckets.
For a five-minute window refreshed every second:

```text
window_size = 5 minutes
bucket_size = 1 second
bucket_count = 300
```

Each bucket is a map of key counts for one second:

```text
bucket 0  -> counts for 19:00:00
bucket 1  -> counts for 19:00:01
bucket 2  -> counts for 19:00:02
...
bucket 299 -> counts for 19:04:59
```

The worker also keeps `active_counts`, which is the current five-minute total:

```text
active_counts[key] = bucket0[key] + bucket1[key] + ... + bucket299[key]
```

The buckets are arranged as a ring so the worker does not allocate a new set of
bucket objects forever. The slot for a timestamp is computed with modulo
arithmetic:

```text
slot = floor(event_time / bucket_size) % bucket_count
```

When time advances from `19:04:59` to `19:05:00`, the bucket for `19:00:00`
expires. Before that slot can be reused for `19:05:00`, the worker subtracts
the old counts from `active_counts`:

```text
expired_bucket = buckets[slot_for_19_00_00]

for each (key, count) in expired_bucket:
    active_counts[key] -= count
    ranking.update(key, active_counts[key])

clear expired_bucket
reuse the slot for 19:05:00
```

This turns expiration into a bounded batch of negative updates. The worker does
not need to revisit all events in the five-minute window.

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/sliding-window-state.svg" alt="Sliding window state represented by a ring of buckets, active counts, local Top-K, and expiration deltas" caption="Only buckets inside the time window contribute to active counts. When the oldest bucket leaves, its counts are subtracted and the slot is reused." %}</center>
</div>

The bucket size controls a tradeoff:

- smaller buckets give fresher expiration and smoother rankings;
- larger buckets reduce memory overhead and expiration work;
- very small buckets can create high metadata overhead for sparse scopes.

Late events require a policy. If the system accepts events up to two minutes
late, then recently closed buckets must remain mutable until the watermark
passes them. If the event is later than the allowed lateness, either drop it
from the real-time ranking or send it to an offline correction path.

The serving API should expose both:

```text
as_of:     when this ranking version was published
watermark: event time through which the ranking is complete
```

A fresh publish with an old watermark is not a fresh result. It only means the
system recently republished stale input.

---

# 8. Exact Counting Versus Bounded Summaries

Exact local Top-K requires exact local counts for all active keys. This is often
practical for bounded keyspaces such as tenants, routes, products, or known
songs. It is harder for open-ended keyspaces such as raw search queries, URLs,
error messages, and user-generated labels.

The memory cost is driven by active distinct keys:

```text
memory ~= active_distinct_keys * bytes_per_count_entry
```

If a worker owns 20 million active keys and each count entry costs 80 to 150
bytes after map overhead and metadata, the worker may need multiple gigabytes
for one window family before checkpoints, buckets, and indexes are included.

Approximate summaries reduce memory by accepting controlled error. The common
choices are:

| Technique | What it keeps | Merge behavior | Main limitation |
|---|---|---|---|
| Count-Min Sketch | Counter arrays | Add arrays with same parameters | Estimates known keys; does not enumerate keys |
| Space-Saving | Fixed candidate table | Mergeable with care, often used as local candidates | Can overestimate candidates near the cutoff |
| Misra-Gries | Bounded candidate table | Mergeable summaries | Candidate counts need error handling |
| Sampling | Sampled event subset | Merge samples statistically | Weak for rare or bursty keys |

For distributed Top-K, approximation has two separate jobs:

```text
candidate discovery: which keys might be in the top list?
count estimation: what are their counts?
```

A Count-Min Sketch helps with count estimation but not discovery. A Space-Saving
table helps with discovery but may not provide exact counts. A practical
approximate design often uses a larger internal candidate set:

```text
visible K = 10
internal candidate size = 100 or 1000
```

The reducer publishes only K items, but it receives enough candidates to avoid
losing keys that are near the boundary.

Approximation should be visible in metadata:

```text
mode = exact | approximate
max_count_error = ...
candidate_size = ...
```

The system should not represent an approximate rank as exact simply because it
is convenient for the API.

---

# 9. Hot Keys

Partitioning by `(metric, scope, key)` gives one owner per key. That is useful
for correctness, but a single heavy key can overload its owner.

Example:

```text
key = tenant_42
traffic = 250,000 events/second
owner = partition 17
```

The system may have hundreds of workers, but partition 17 still receives all
updates for `tenant_42`.

The common mitigation is key splitting. Once a key is classified as hot, events
for that key are distributed across several salts:

```text
partition = hash(metric, scope, key, salt)
salt = hash(event_id) % split_factor
```

Each salted owner maintains a partial count. A combine stage sums the partial
counts for the logical key before the reducer computes the final Top-K.

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/hot-key-splitting.svg" alt="A hot key is split into salted subkeys across several workers and recombined before global ranking" caption="Splitting a hot key removes the single-owner bottleneck, but the system must recombine partial counts before ranking." %}</center>
</div>

This changes the correctness model. The earlier proof that local Top-K lists
are sufficient no longer applies to split keys because the logical key's count
is distributed. The reducer must know the split-key registry and wait for all
required partial summaries for that hot key.

Hot-key splitting also needs a transition plan. If a key becomes hot halfway
through a sliding window, some buckets may contain unsplit counts and newer
buckets may contain salted counts. The ranking layer must sum both forms until
the unsplit buckets age out.

```text
logical_count(key) =
    unsplit_count(key)
  + sum(partial_count(key, salt) for active salts)
```

The split registry should be versioned and included in event metadata or worker
configuration. Otherwise replay can route old events differently from the
original processing path.

---

# 10. Reducer State

Reducers are stateful. For each ranking key they keep:

```text
latest_summary_by_partition
latest_summary_by_hot_key_partial
current_candidate_counts
current_global_top_k
published_version
watermark_by_partition
```

The reducer does not need all raw event counts when keys are single-owner. It
needs the latest local summaries. For split hot keys, it also needs enough
partial information to reconstruct the logical key count.

Reducers can be partitioned by ranking key:

```text
reducer_partition = hash(metric, scope, window_size)
```

This keeps all summaries for one served ranking on one reducer. If one scope
becomes too hot at the reducer layer, use a tree:

```text
worker summaries -> regional reducers -> global reducer -> ranking store
```

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/aggregation-tree.svg" alt="Worker summaries flow into regional reducers and then a global reducer before publishing ranking versions" caption="A reducer tree limits fan-in per process while preserving a single published ranking for each scope." %}</center>
</div>

Each level should publish versioned summaries in the same way workers do. This
keeps the merge protocol uniform and makes recovery simpler.

---

# 11. Ranking Publication

The published ranking should be immutable.

```text
write ranking:{metric}:{scope}:{window}:{version}
verify item count and metadata
atomically update ranking:{metric}:{scope}:{window}:latest -> version
expire older versions after retention
```

Readers follow the latest pointer and receive one complete ranking version.
They should not observe a ranking while it is being written.

The ranking record should include:

```text
items:       rank, key, count, optional error
as_of:       publication time
watermark:   minimum contributing event-time watermark
mode:        exact or approximate
version:     monotonically increasing ranking version
input_range: optional partition offsets or summary sequences
```

The `input_range` is useful during incidents. It answers which log offsets or
summary sequences contributed to a visible result.

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/publish-commit-protocol.svg" alt="A reducer writes an immutable ranking version, verifies it, and atomically moves the latest pointer so readers only see complete versions" caption="The latest pointer is the serving commit point. Crashes before it leave readers on the previous version; crashes after it expose a complete new version." %}</center>
</div>

---

# 12. Worker Failure and Replay

A stateful worker can fail at any time. Recovery needs three pieces:

```text
durable input log
checkpointed local state
committed input offset
```

The checkpoint contains count maps, bucket state, ranking state, watermarks, and
any approximate summaries. The committed offset records the point in the input
log represented by that checkpoint.

On restart:

```text
load latest checkpoint
seek input partitions to checkpoint offset
replay events after that offset
resume publishing with a new partition epoch
```

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/recovery-and-fencing.svg" alt="A worker restores checkpointed state, replays from the durable log, and publishes with a new fenced epoch" caption="Checkpoint plus replay restores state. Partition epochs fence off summaries from old worker instances." %}</center>
</div>

The partition epoch matters. During failover, the old worker may still be alive
but partition ownership has moved. If both old and new workers publish
summaries, the reducer must accept only the active epoch.

```text
accept summary if partition_epoch == current_owner_epoch
ignore summary if partition_epoch < current_owner_epoch
reject or quarantine summary if partition_epoch > known_owner_epoch
```

This fencing rule prevents stale owners from corrupting the published Top-K.

---

# 13. Reducer Failure

A reducer can recover in several ways.

The simplest method is to rebuild from the latest summaries published by all
workers. This requires workers, or an intermediate summary topic, to retain
their latest summary for each active ranking key.

A stronger method gives the reducer its own changelog:

```text
summary update -> reducer state update -> reducer changelog -> publish
```

On restart, the reducer loads its checkpoint and replays summary updates after
the checkpoint. This mirrors the worker recovery path.

Reducer recovery should be tested with partial publish failures. If the reducer
crashes after writing a ranking version but before advancing the latest pointer,
readers continue seeing the old version. If it crashes after advancing the
pointer, the version is already complete. The atomic pointer is the commit point
for serving.

---

# 14. Duplicate Events and Exactly-Once Claims

Most real systems are at least once at one or more boundaries. Producers retry.
Log clients retry. Workers restart after checkpointing. Publishers retry
summary writes.

There are two defensible approaches.

The first approach makes event processing idempotent. Keep a bounded dedupe set
keyed by `event_id` per partition and window. This is expensive but useful when
counts drive enforcement or billing-like decisions.

The second approach accepts that duplicate events can enter real-time Top-K and
corrects through offline jobs. This can be acceptable for exploratory
dashboards, but the duplicate rate should be measured and visible.

Exactly-once stream processing frameworks can reduce application complexity,
but the design should still name the commit point:

```text
input offset is committed only after state checkpoint and output summary are durable
```

If the system cannot state this boundary, the exactly-once claim is probably
not precise enough to debug.

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/commit-boundary.svg" alt="A worker applies an event, checkpoints state, publishes a summary, and only then commits the input offset" caption="The committed input offset should describe both durable state and durable output. Otherwise recovery can skip work or duplicate visible summaries." %}</center>
</div>

---

# 15. Backpressure and Staleness

A high-TPS Top-K system should degrade by becoming stale, not by serving
partially merged rankings as if they were complete.

Backpressure can appear in several places:

- ingestion cannot append to the log fast enough;
- one log partition accumulates lag;
- workers cannot update state fast enough;
- bucket expiration takes too long;
- reducers cannot merge summaries fast enough;
- the ranking store throttles writes.

The serving layer should continue returning the last complete ranking with
metadata:

```text
as_of = 19:05:05
watermark = 19:04:58
staleness = now - watermark
```

Callers can decide whether that result is usable. Internally, alerts should be
based on watermark lag per ranking key, not only CPU or queue depth. A system
with low CPU and an old watermark is still failing its freshness contract.

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/stale-watermark.svg" alt="Several partitions advance event-time watermarks, one partition lags, and the serving layer returns the last complete ranking with visible staleness" caption="When one partition lags, the global watermark stops. The system should serve the last complete ranking as stale instead of publishing a partial result as current." %}</center>
</div>

---

# 16. Regional Failure

Multi-region Top-K can be built in two ways.

In a regional-first design, each region maintains local rankings and publishes
regional summaries. A global reducer merges regional summaries.

```text
region workers -> regional reducer -> global reducer
```

This keeps regional results available when cross-region links fail. The global
result may become incomplete. The global watermark should make that visible.

In a global-stream design, all events replicate into one logical stream before
aggregation. This gives a simpler global ordering model but adds dependency on
cross-region replication and can increase latency.

For operational heavy hitters such as hot tenants or hot API routes,
regional-first is often useful because incidents are frequently regional. For
strict global counts, the real-time ranking should be paired with an offline
reconciliation path.

During a regional outage, avoid silently mixing complete and incomplete input.
A global ranking can be marked:

```text
complete_regions = [eu-west, us-east]
missing_regions = [ap-south]
watermark = min(watermark of complete regions)
```

This is better than publishing a precise-looking global list that excludes one
region.

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/regional-failure.svg" alt="Regional Top-K summaries from eu-west and us-east reach the global reducer while ap-south is isolated and marked stale in the global response" caption="Regional-first aggregation can keep local rankings available while the global ranking exposes which regions are complete, stale, or missing." %}</center>
</div>

---

# 17. Control Plane

The data plane processes events. The control plane manages the rules used by
the data plane.

Control-plane state includes:

- metric definitions;
- normalization versions;
- allowed scopes;
- partition assignments;
- worker ownership epochs;
- hot-key split registry;
- window definitions;
- approximate-summary parameters;
- publication cadence;
- retention policy.

The control plane should be versioned. Workers should stamp outputs with the
versions they used. Reducers should not merge summaries produced with
incompatible versions unless the merge rule explicitly supports it.

For example, if normalization changes from raw URL to route template, the
counts are no longer comparable:

```text
/users/1/orders/9
/users/2/orders/4
```

may become:

```text
/users/:id/orders/:id
```

The system should either start a new ranking version family or run a migration
that makes the change explicit.

---

# 18. Storage Choices

The hot state usually lives close to the stream workers.

Worker state can be stored in embedded RocksDB, an in-memory map with periodic
snapshots, or a framework-managed state store. The choice depends on state size
and recovery expectations.

Reducers need less state than workers but have stricter publication semantics.
Their state can also be checkpointed locally and changelogged to a durable log.

The ranking store should optimize reads:

```text
GET latest ranking for metric + scope + window
```

Redis, DynamoDB, Cassandra, FoundationDB, or a relational table can all work
depending on the surrounding system. The store should support atomic latest
pointer updates or equivalent compare-and-set semantics.

Raw events should be retained separately in object storage or a long-retention
log when rebuilds, audits, or offline validation are required.

---

# 19. Observability

The main health signal is not the size of the final list. It is whether every
required partition is contributing fresh, compatible input to that list.

Track:

- ingest events per second by metric, scope, and region;
- log append latency and partition lag;
- active distinct keys per worker;
- worker state size and checkpoint duration;
- bucket expiration duration;
- local Top-K publish rate and payload size;
- reducer fan-in per ranking key;
- stale or missing partition summaries;
- global watermark lag;
- ranking publication failures;
- latest-pointer update failures;
- hot-key split count and partial-summary completeness;
- replay duration after worker restart;
- exact-vs-approximate error on sampled windows.

For approximate Top-K, track rank overlap with exact offline results:

```text
overlap = size(real_time_top_k ∩ exact_top_k) / K
```

Count error alone is not enough. A system can have small count error and still
publish an unstable rank order near the cutoff.

---

# 20. Failure Matrix

| Failure | Expected behavior |
|---|---|
| Worker crash | New owner restores checkpoint, replays log, publishes with a new epoch. |
| Old worker resumes | Reducer ignores summaries from the stale epoch. |
| Reducer crash | Reducer reloads checkpoint or rebuilds from retained latest summaries. |
| Ranking store write fails | Readers continue using the previous complete version. |
| Latest pointer update fails | New version remains invisible until retry succeeds. |
| One partition lags | Global watermark stops advancing; result is served as stale. |
| Hot key overloads owner | Control plane enables salted split; reducer recombines partial counts. |
| Region isolated | Regional ranking continues; global ranking marks missing or stale regions. |
| Bad normalization deploy | Outputs carry normalization version; reducers reject incompatible summaries. |
| Backfill replays old events | Backfill writes to a separate rebuild path or uses idempotent window versions. |

This matrix is part of the design, not an operations appendix. Top-K systems
are often used during incidents. Their own failure modes must be visible.

---

# 21. Reference Design

A practical high-throughput exact design for bounded keyspaces looks like this:

1. Events are validated and written to a durable partitioned log.
2. Partitions are assigned by `hash(metric, scope, key)`.
3. Each worker owns a set of partitions under a fenced epoch.
4. Workers maintain exact active counts, bucket counts, and local Top-K per
   scope.
5. Workers checkpoint state and committed offsets.
6. Workers publish versioned local Top-K summaries at a fixed cadence.
7. Reducers keep the latest summary per partition and merge candidates.
8. Reducers publish immutable ranking versions and atomically advance latest
   pointers.
9. The query API reads the latest complete ranking version.
10. Offline jobs validate selected windows and rebuild when needed.
11. The control plane manages partition ownership, hot-key splits, and
    compatible configuration versions.

For unbounded keyspaces, replace exact local counts with bounded summaries
where necessary, but keep the same ownership, publication, recovery, and serving
structure. Approximation changes the accuracy contract; it should not remove
versioning, watermarks, fencing, or replay.

<div>
  <center>{% include figure.html path="assets/img/top-k-heavy-hitters/reference-design.svg" alt="Reference design for distributed Top-K with partitioned event log, fenced workers, checkpoints, reducers, ranking store, query API, control plane, and offline validation" caption="The reference design keeps the hot write path partitioned, the read path materialized, and the recovery path based on checkpoint plus replay." %}</center>
</div>

The system is correct only relative to the contract it publishes. For exact
mode, the contract depends on single ownership of each logical key or complete
recombination of split keys. For approximate mode, the contract depends on the
summary algorithm and candidate size. For both modes, resiliency depends on
durable input, checkpointed state, fenced ownership, idempotent summaries,
atomic publication, and visible watermarks.
