---
layout: single
comments: true
title: "Inside Cassandra: Partitioning, Replication, Storage, and Repair"
date: 2022-01-16 00:00:00-0000
description: "A rigorous walk through Cassandra's data model, token-aware routing, leaderless replication, LSM storage engine, tunable consistency, hinted handoff, and anti-entropy repair."
tags: [cassandra, distributed-systems, databases, replication, lsm-tree, system-design]
categories: ['Distributed Systems Components']
---

# 1. From One Database to Cassandra

## Begin With One Database

The simplest durable application has one database authority:

~~~text
clients -> application -> primary database
~~~

The primary owns the complete dataset, serializes conflicting writes, maintains
indexes, and gives transactions one commit boundary. Read replicas can reduce
read load, but writes still converge on the primary and failover still chooses
one new authority. If this architecture meets the workload and recovery goals,
it is usually easier to query, transact across, back up, and operate than a
leaderless distributed database.

The model becomes strained when the durable dataset or write traffic must span
many machines, when one region cannot own the write path, or when the service
must continue accepting selected operations while replicas cannot communicate.
Simply adding independent writable databases creates conflicts without defining
which copy wins or how they converge.

![A single database and a Cassandra cluster distribute different responsibilities](/assets/img/cassandra/single-database-vs-cassandra.svg)

## What Cassandra Adds

Apache Cassandra is a distributed, partitioned **wide-column database** built
for workloads that need high write throughput, predictable key-based access,
horizontal scale, and continued service while machines or network links fail.

Its architecture combines two influential designs:

- Dynamo contributes consistent-hash partitioning, multi-replica writes,
  tunable consistency, gossip, hints, and anti-entropy repair.
- Bigtable contributes the sorted wide-column data model and an LSM-style
  storage engine built from commit logs, memtables, and immutable SSTables.

Calling Cassandra a key/value store hides the most useful part of its model.
A partition key locates a **partition**, while clustering columns order many
rows inside that partition. That combination supports queries such as "the
newest 100 events for this user today" without scanning the whole cluster.

Cassandra is also not "fully replicated." A keyspace defines a replication
factor, and each partition is placed on that many replica nodes. With a
replication factor of three, a partition normally has three copies—not one copy
on every server.

## The Central Tradeoff

Cassandra avoids a leader and a consensus round for ordinary reads and writes.
Any node can coordinate a request, and every natural replica for a partition
can accept its mutations. The client chooses how many replicas must respond by
setting a consistency level per operation.

This makes availability and latency tunable, but it moves work elsewhere:

- replicas can temporarily disagree;
- timestamps resolve concurrent cell versions;
- hints reduce missed-write windows but do not replace repair;
- compaction continually merges immutable files;
- operators must schedule anti-entropy repair;
- schemas must make the common query local to one bounded partition.

The rest of this article follows one mutation through all of those mechanisms.

## When This Trade Is Appropriate

Cassandra is a strong fit when access is dominated by known partition-key
queries, writes must scale horizontally, data should be replicated across
failure domains, and temporary replica disagreement can be handled explicitly.
Telemetry, time-bucketed events, device state, and large write-heavy lookup
tables often have that shape.

It is usually a poor default for ad hoc joins, arbitrary multi-row
transactions, small datasets that fit one relational database, or workloads
whose correctness requires every ordinary write to pass through one global
serial order. Cassandra removes the single write leader by giving the
application and operators more responsibility for schema locality,
consistency levels, conflict resolution, tombstones, compaction, and repair.

![Cassandra is chosen for a workload contract, not merely for having many nodes](/assets/img/cassandra/workload-fit.svg)

---

# 2. A Concrete Data Model

Assume an observability service needs the newest events for one user on one
day. A query-first Cassandra schema could be:

~~~sql
CREATE KEYSPACE telemetry
WITH replication = {
  'class': 'NetworkTopologyStrategy',
  'eu-west': 3,
  'us-east': 3
};

CREATE TABLE telemetry.user_events_by_day (
  tenant_id  text,
  user_id    uuid,
  event_day  date,
  event_time timestamp,
  event_id   timeuuid,
  event_type text,
  payload    text,
  PRIMARY KEY (
    (tenant_id, user_id, event_day),
    event_time,
    event_id
  )
) WITH CLUSTERING ORDER BY (event_time DESC, event_id DESC);
~~~

CQL is the database interface, so it remains CQL in this article. All
implementation examples are C++.

## Partition Key and Clustering Key

The primary key has two different jobs:

~~~text
PRIMARY KEY (
  (tenant_id, user_id, event_day),  // partition key
  event_time, event_id              // clustering columns
)
~~~

For the row

~~~text
tenant_id = acme
user_id   = 2f3a...
event_day = 2026-08-17
event_time = 10:42:03.120Z
event_id   = 8d1b...
event_type = checkout_started
~~~

the tuple `(acme, 2f3a..., 2026-08-17)` determines the token and therefore the
replica set. `event_time` and `event_id` determine the row's ordered position
inside that partition.

![Cassandra partition and clustering-key layout](/assets/img/cassandra/partition-key-layout.svg)

The application can efficiently ask:

~~~sql
SELECT event_time, event_id, event_type, payload
FROM telemetry.user_events_by_day
WHERE tenant_id = 'acme'
  AND user_id = 2f3a0000-0000-0000-0000-000000000001
  AND event_day = '2026-08-17'
  AND event_time >= '2026-08-17T10:00:00Z'
LIMIT 100;
~~~

The full partition key is equality-constrained, and the remaining predicate is
a contiguous clustering-key range. Cassandra can route to one replica set and
perform an ordered slice rather than scatter-gather across every node.

## Why the Day Bucket Matters

Using only `(tenant_id, user_id)` as the partition key would keep the user's
entire history in one ever-growing partition. A hot or old account could then
create:

- a disproportionately large storage and compaction unit;
- long reads and repairs;
- concentrated traffic on one replica set;
- expensive streaming during bootstrap or replacement.

The day bucket bounds growth. It also imposes a cost: a 30-day query becomes 30
partition queries that the application must issue and merge. Cassandra schema
design is the choice of that boundary, not normalization for its own sake.

**Design rule:** choose a partition key that distributes load and bounds size,
while keeping the dominant query inside as few partitions as possible.

---

# 3. Cluster Architecture

## Peer Nodes, Coordinators, and Replicas

Cassandra nodes are peers. There is no permanent primary server for a token
range and no special node through which every request must pass.

A client can connect to any node. For that request, the receiving node becomes
the **coordinator**. It:

1. serializes the partition key exactly as Cassandra expects;
2. computes its token;
3. consults local token and topology metadata;
4. selects the natural replicas;
5. sends requests to replicas;
6. waits for the requested consistency level;
7. returns success, data, or an error to the client.

The coordinator role ends with the request. The same node may coordinate one
partition while being a replica for another.

![Cassandra coordinator, replicas, and control plane](/assets/img/cassandra/cluster-architecture.svg)

Production drivers are normally token-aware. They learn cluster metadata and
prefer a replica as the initial contact, avoiding an extra coordinator-to-
replica hop. Server-side coordination still exists; good client routing merely
places it close to the data.

## Data Plane and Control Plane

Two kinds of traffic coexist:

- The **data plane** carries client mutations, replica writes, reads, hints,
  repair streams, and bootstrap streams.
- The **control plane** distributes membership, token metadata, schema state,
  and failure observations.

Cassandra uses gossip to spread cluster state. Each node also runs a phi
accrual failure detector and independently decides whether another endpoint is
reachable. `UP` and `DOWN` are therefore local conclusions, not a single
globally committed truth.

That distinction explains several behaviors. A coordinator can store a hint
because it considers a replica down. Another coordinator may still reach that
replica. Temporary disagreement is expected; token ownership is not reassigned
automatically merely because a node misses heartbeats.

## Seeds Are Not Masters

Seed nodes help a new node discover the cluster. They do not coordinate normal
reads, own extra data, vote on every mutation, or become a permanent source of
truth. Once discovery succeeds, peers communicate directly.

---

# 4. Tokens, Vnodes, and Replica Placement

## From a Partition Key to a Token

With `Murmur3Partitioner`, Cassandra hashes the serialized partition key into a
signed 64-bit token space. Conceptually:

~~~text
partition key bytes
    -> Murmur3 hash
    -> token t
    -> token range containing t
    -> natural replicas for that range
~~~

Only the partition key participates. Clustering columns choose a row inside an
already-located partition; they do not change its replica set.

## Virtual Nodes

A physical node normally owns multiple token ranges, called virtual nodes or
vnodes. Vnodes spread a machine's ownership around the ring, so adding or
removing a physical machine transfers smaller ranges to or from several peers
instead of moving one giant contiguous range.

The ring is a useful ownership model, not a picture of network topology. A
write does not hop server-by-server around the ring. The coordinator calculates
the replicas and contacts them over the network.

![Token ranges, virtual nodes, and replica placement](/assets/img/cassandra/token-replication.svg)

## Replication Strategy

`NetworkTopologyStrategy` defines a replication factor per data center and
places copies across distinct nodes while considering topology such as racks.
For the example keyspace, each partition should have three replicas in
`eu-west` and three in `us-east`.

Replica placement is deterministic from:

~~~text
(token metadata, snitch topology, replication strategy, RF)
    -> ordered natural-replica set
~~~

There is no primary replica in that set for ordinary mutations.

## C++: Replica Selection Sketch

The essential routing loop is small: start at the vnode containing the token,
walk with ring wraparound, and count distinct physical nodes rather than vnode
entries.

~~~cpp
std::vector<NodeId> replicasFor(Token token,
                                const std::vector<Vnode>& ring,
                                std::size_t rf) {
    std::size_t i = firstVnodeEndingAtOrAfter(token, ring);
    std::vector<NodeId> replicas;
    std::set<NodeId> seen;

    while (replicas.size() < rf) {
        const Vnode& vnode = ring[i % ring.size()]; // ring wraparound
        if (seen.insert(vnode.physicalNode).second) {
            replicas.push_back(vnode.physicalNode);
        }
        ++i;
    }
    return replicas;
}
~~~

This is an algorithm sketch, not a Cassandra client implementation. A real
driver must use Cassandra's exact partition-key serialization and Murmur3
token, while `NetworkTopologyStrategy` also considers data centers, racks, and
pending ranges. Using C++ `std::hash` would route keys incorrectly.

---

# 5. The Write Path

Consider this mutation at `LOCAL_QUORUM`:

~~~sql
INSERT INTO telemetry.user_events_by_day (
  tenant_id, user_id, event_day, event_time,
  event_id, event_type, payload
) VALUES (
  'acme',
  2f3a0000-0000-0000-0000-000000000001,
  '2026-08-17',
  '2026-08-17T10:42:03.120Z',
  now(),
  'checkout_started',
  '{"cart_id":"c-91"}'
);
~~~

Assume the local data center has `RF = 3`, with natural replicas `N1`, `N2`,
and `N3`, and the client contacts `N4`.

## Coordinator Work

`N4` becomes coordinator and:

1. computes the token from `(tenant_id, user_id, event_day)`;
2. finds `N1`, `N2`, and `N3` as natural replicas;
3. sends the mutation to the replica set;
4. waits for two local acknowledgements because
   `LOCAL_QUORUM(RF=3) = floor(3/2) + 1 = 2`;
5. returns success after the consistency condition is met.

The coordinator normally attempts all replicas; the consistency level controls
how many acknowledgements are required, not how many copies should ultimately
exist.

## Replica Work

On each replica, the local storage engine performs two foreground operations:

1. append the mutation to the node's commit log;
2. apply the mutation to the table's in-memory memtable.

The replica can then acknowledge according to its durability configuration.
An SSTable flush is not required on the request path.

![End-to-end Cassandra write at LOCAL_QUORUM](/assets/img/cassandra/write-path.svg)

If `N3` is unavailable, `N1` and `N2` can still satisfy `LOCAL_QUORUM`. The
coordinator records a hint for `N3` when hinting applies. The client sees a
successful write, while the replicas are temporarily inconsistent.

## Timeout Is Not the Same as Failure

Suppose `N1` and `N2` durably apply the mutation, but their acknowledgements
arrive after the coordinator's deadline. The client receives a timeout even
though the write may be stored on a quorum.

The safe application model is therefore:

~~~text
success  -> requested acknowledgements were observed
timeout  -> outcome is unknown
failure  -> do not infer that no replica stored the mutation
~~~

Retries should be idempotent. Re-inserting the same Cassandra primary key is an
upsert, but application side effects outside Cassandra still need their own
idempotency design.

---

# 6. The Local Storage Engine

Distribution decides **which replicas** store the partition. The LSM storage
engine decides **how each replica** persists and reads it.

## Commit Log

The commit log is an append-only write-ahead log shared by tables on a node.
Sequential append avoids an in-place random update. If a process crashes before
memtable contents reach SSTables, startup replays relevant commit-log mutations.

Commit-log segments can be recycled only when every mutation they protect has
been flushed from memtables to SSTables. A cold table can therefore pin an old
segment and cause a flush even when that table's own memtable is small.

## Memtables and Flush

Each table has an active memtable holding sorted in-memory mutations. A flush
trigger rotates it into an immutable memtable and writes a new immutable
SSTable. Triggers include memory pressure and the need to recycle commit-log
space.

SSTables are never updated in place. A partition may have versions spread
across the active memtable, immutable memtables, and many SSTables.

## SSTable Components

An SSTable is a family of component files, including data, indexes, summaries,
Bloom filters, compression metadata, checksums, and statistics. Exact component
names vary by SSTable format, but the read idea is stable:

1. a Bloom filter cheaply rules out files that definitely lack a partition;
2. index structures narrow the location;
3. the data component supplies rows and cells;
4. Cassandra merges visible versions from all relevant sources.

![Cassandra local LSM storage engine](/assets/img/cassandra/storage-engine.svg)

For the generic sorted-file mechanics, see the separate
[SSTable article](/blog/2022/sstable/). Cassandra adds partition indexes,
clustering-row structure, per-cell timestamps, tombstones, repair metadata, and
compaction strategies to that foundation.

## Compaction Is Part of the Write Cost

Compaction reads SSTables, merges sorted partitions, resolves shadowed cell
versions, safely removes eligible tombstones, and writes new SSTables. It
reduces read amplification but creates background disk and CPU work.

In Cassandra 5.0, Unified Compaction Strategy is recommended for most new
workloads. The older workload-shaped choices remain useful mental models:

| Strategy | Favors | Main cost |
|---|---|---|
| UCS | configurable balance across mixed workloads | requires workload-aware tuning |
| STCS | write-heavy workloads and similarly sized files | more overlapping SSTables on reads |
| LCS | read-heavy or update-heavy workloads | higher write amplification |
| TWCS | mostly immutable TTL/time-series data | late writes can cross closed windows |

Our day-bucketed event table is a possible TWCS workload only if events are
mostly immutable and expiration policy aligns with time windows. A date column
alone is not enough reason to select TWCS.

---

# 7. The Read Path

Now read the newest 100 events for the example partition at
`LOCAL_QUORUM`.

## Coordinator Selection

The coordinator calculates the same token and replica set. It chooses enough
local replicas to satisfy the requested level, preferring replicas that are
live and responsive according to its local view.

The coordinator may use data and digest responses to detect disagreement. A
digest is a compact hash of the replica's result, not the row data needed by
the client.

## Work on Each Replica

Each replica constructs its answer from several sources:

1. check the active and flushing memtables;
2. use Bloom filters to skip SSTables that definitely lack the partition;
3. use indexes to seek to candidate partition and clustering ranges;
4. merge cells from all candidate sources by key and timestamp;
5. apply row, range, partition, and cell tombstones;
6. stop after the query limit is satisfied.

This is why one logical partition does not imply one disk read. An unhealthy
compaction backlog can spread its versions across many SSTables and increase
read amplification.

## Reconciliation

If replicas return different versions, Cassandra reconciles them and returns
the winning visible result. Depending on table read-repair settings and the
kind of read, the coordinator may issue blocking read repair before completing
the request.

It is incorrect to say every read always repairs every replica. Read repair is
best effort and scoped to data encountered by reads. Scheduled anti-entropy
repair is still required.

![Cassandra quorum read, local merge, and reconciliation](/assets/img/cassandra/read-path.svg)

## C++: Reconciliation Sketch

The full Cassandra reconciliation model includes rows, range tombstones,
collection elements, TTLs, and tie-breaking rules. This smaller C++ example
captures its central rule: compare versions before deciding whether a value is
visible.

~~~cpp
struct CellVersion {
    std::int64_t timestamp_us;
    bool tombstone;
    std::string value;
};

CellVersion reconcile(const std::vector<CellVersion>& versions) {
    return *std::max_element(
        versions.begin(), versions.end(),
        [](const CellVersion& a, const CellVersion& b) {
            if (a.timestamp_us != b.timestamp_us)
                return a.timestamp_us < b.timestamp_us;
            // Simplified tie-break: deletion wins at an equal timestamp.
            return !a.tombstone && b.tombstone;
        });
}
~~~

The real comparison has additional tie-break rules and must handle rows, range
tombstones, collections, and TTLs. The important point is that Cassandra
compares timestamped versions before deciding whether a value or deletion is
visible. A client clock far in the future can therefore shadow correct later
writes until time catches up or the bad version is removed.

---

# 8. Tunable Consistency

Consistency level is selected independently for each operation. It states how
many replicas the coordinator must hear from—not how many replicas exist.

For one data center with replication factor `N`:

~~~text
QUORUM(N) = floor(N / 2) + 1
~~~

For `N = 3`, quorum is two.

| Level | Required response | Typical tradeoff |
|---|---|---|
| `ONE` | one replica | lowest coordination, greater stale-read exposure |
| `QUORUM` | quorum across all replicas | quorum intersection, possibly cross-DC latency |
| `ALL` | every replica | strongest acknowledgement, lowest availability |
| `LOCAL_ONE` | one replica in local DC | local latency and high availability |
| `LOCAL_QUORUM` | quorum in local DC | common multi-DC balance |
| `EACH_QUORUM` | quorum in every DC for writes | stronger per-DC acknowledgement, WAN-sensitive |

## Quorum Intersection

If a write is acknowledged by `W` replicas and a later read obtains responses
from `R` replicas, then:

~~~text
R + W > N
~~~

forces the read and write sets to overlap. With `N=3`, `W=2`, and `R=2`, at
least one read response comes from a replica that acknowledged the write.

![Consistency-level response sets for RF=3](/assets/img/cassandra/consistency-levels.svg)

Intersection is important, but the slogan "quorum means strong consistency"
is incomplete. Cassandra still reconciles timestamped cell versions. Client
clock errors, simultaneous writes, failed requests with unknown outcomes, and
topology changes all affect the application-visible history. Standard quorum
operations are not a substitute for a linearizable compare-and-set.

## Availability Matrix for RF=3

| Reachable local replicas | `ONE` | `QUORUM` | `ALL` |
|---:|:---:|:---:|:---:|
| 3 | available | available | available |
| 2 | available | available | unavailable |
| 1 | available | unavailable | unavailable |
| 0 | unavailable | unavailable | unavailable |

The same arithmetic applies to reads and writes, but their latency profiles
differ. A write normally appends locally. A read may touch many SSTables and
reconcile replica responses.

---

# 9. Conflicts, Deletes, and TTL

## Last-Write-Wins Is Per Cell

Cassandra stores timestamps with mutations and reconciles conflicting cell
versions using those timestamps. Two coordinators can accept concurrent
updates without first agreeing on a global order.

That design favors availability, but "last" means greatest mutation timestamp,
not necessarily the update that a user issued last in wall-clock reality.

Practical consequences:

- synchronize clocks and monitor skew;
- be cautious with explicit `USING TIMESTAMP`;
- do not generate timestamps far in the future;
- model operations that must not race with lightweight transactions or another
  coordination mechanism.

## A Delete Is a Write

Because SSTables are immutable, Cassandra cannot immediately erase every older
copy of a cell. A delete writes a timestamped **tombstone**. Reads observe the
tombstone and suppress older values. Compaction may later remove both the
tombstone and shadowed values when doing so is safe.

TTL expiry uses related mechanics: expired data becomes logically invisible,
then background compaction reclaims its bytes.

## Zombie Resurrection

Suppose `N3` is offline when a delete reaches `N1` and `N2`:

1. `N3` still holds the old live value.
2. The tombstone remains on `N1` and `N2` for a safety window.
3. Repair should deliver the tombstone to `N3`.
4. If the tombstone is purged before the stale replica is repaired, the old
   value can look newer than "nothing" and reappear.

Tombstone garbage collection and repair cadence are therefore coupled. The
safe setting depends on failure duration, replacement procedure, repair
schedule, and table semantics—not merely on a desire to reclaim disk faster.

Tombstone-heavy reads are also expensive. A query may scan many deleted cells
to return very few live rows. Time bucketing, bounded partitions, appropriate
TTL policy, and compaction choice are data-model decisions with direct latency
consequences.

---

# 10. Hinted Handoff and Repair

Cassandra uses several convergence mechanisms. They overlap, but they are not
interchangeable.

## Hinted Handoff

When a coordinator cannot deliver a mutation to a replica, it can store a hint
on its own local disk. A hint records the target replica and mutation. When the
target returns, the coordinator replays the hint.

Hints shorten inconsistency after brief outages, but they are best effort:

- the coordinator holding a hint may also fail;
- hints have a configured retention window;
- a long outage can exceed that window;
- hints cover writes observed by that coordinator, not arbitrary historical
  divergence.

Hints improve recovery time. They do not prove replicas have converged.

## Read Repair

When a read observes divergent replicas, Cassandra can send the reconciled
version to stale replicas within the read's scope. This repairs data that users
happen to read. Cold partitions may never benefit from it.

## Anti-Entropy Repair

Repair compares replicas that share token ranges. At a high level:

1. replicas build trees of hashes over corresponding data ranges;
2. matching branches are skipped;
3. mismatching branches are narrowed recursively;
4. differing data is streamed between replicas;
5. the repaired range converges.

Merkle trees reduce comparison traffic, but repair still consumes disk,
network, CPU, and compaction capacity. Cassandra does not automatically run
the complete operational repair schedule for the cluster; operators must plan
and execute it.

![Hints, read repair, and anti-entropy repair](/assets/img/cassandra/repair-paths.svg)

## Why All Three Exist

| Mechanism | Trigger | Best at | Not sufficient for |
|---|---|---|---|
| Hint | failed write delivery | short, recent replica outage | guaranteed convergence |
| Read repair | inconsistent data encountered by a read | hot partitions | cold data and full ranges |
| Anti-entropy repair | scheduled/operator-driven comparison | systematic convergence | zero-cost continuous operation |

**Operational invariant:** complete repair within the table's tombstone safety
assumptions. If that cannot be guaranteed, revisit the garbage-collection and
recovery design rather than hoping hints will cover the gap.

---

# 11. Failure Scenarios

## Replica Fails Before the Write

At `RF=3`, `LOCAL_QUORUM`, two reachable replicas can complete the write. The
coordinator stores a hint for the missing replica when possible. If only one
replica is reachable, the operation cannot satisfy the consistency level.

## Coordinator Fails Before Replying

Replicas may have stored the mutation even though the client never receives a
reply. Retrying an idempotent mutation is appropriate; assuming the mutation
failed is not.

## Coordinator Fails After Replying

The acknowledged replicas retain their writes. The coordinator is not a
leader whose continued survival is required. A new request can use another
coordinator and the same token metadata.

## One Replica Returns Stale Data

At `ONE`, that stale replica can determine the response. At quorum, the
coordinator compares enough responses for intersection with a quorum write and
reconciles versions. Repair mechanisms later converge missed replicas.

## A Rack or Availability Zone Fails

Replication protects the workload only if placement spans failure domains.
Three replicas on three processes but one rack do not tolerate a rack loss.
Accurate snitch/topology configuration is therefore part of the data safety
model.

## Network Partition Splits the Cluster

Each side makes reachability decisions locally. A side with enough replicas for
the requested consistency level can continue. A side without enough replicas
rejects the operation at that level. Lower levels may allow both sides to
accept conflicting writes, which later reconcile by timestamp.

This is Cassandra's practical CAP tradeoff: the application chooses how much
replica coordination each operation requires, while the system remains
partition-tolerant and repairs divergence later.

## Disk Is Slow but the Node Is Alive

Partial failures are often worse than clean crashes. A slow replica may remain
reachable while driving coordinator tail latency, accumulating compaction debt,
or timing out reads. Speculative retry can reduce latency for some reads, but
it adds duplicate work and cannot repair saturated storage.

---

# 12. Multi-Data-Center Behavior

With `NetworkTopologyStrategy` and RF 3 in both `eu-west` and `us-east`, one
partition has six replicas.

A common regional request policy is:

~~~text
write at LOCAL_QUORUM -> wait for 2 of 3 local replicas
read  at LOCAL_QUORUM -> wait for 2 of 3 local replicas
~~~

The coordinator still forwards mutations toward remote natural replicas, but
the client acknowledgement need not wait for a remote quorum. This keeps the
normal latency tied to the local region while maintaining remote copies.

It also creates a disaster-recovery question: a successful local acknowledgement
does not prove that the other data center stored the mutation before the local
region disappeared. If the business requires that proof, use an appropriate
cross-DC consistency level and accept WAN latency and availability costs.

`QUORUM` and `LOCAL_QUORUM` are not synonyms in a multi-DC keyspace. Global
`QUORUM` counts replicas across all data centers; `LOCAL_QUORUM` counts only the
coordinator's local data center.

Keep client load-balancing local-DC aware. Accidentally coordinating routine
requests in a remote region adds WAN latency even when the selected consistency
level is local.

---

# 13. Lightweight Transactions and Batches

## When Timestamp Reconciliation Is Not Enough

Suppose an application must reserve a username only if it is absent:

~~~sql
INSERT INTO accounts.usernames (username, user_id)
VALUES ('ada', 2f3a0000-0000-0000-0000-000000000001)
IF NOT EXISTS;
~~~

An ordinary read followed by an ordinary write races. Cassandra implements
lightweight transactions (LWT) with Paxos to provide linearizable compare-and-
set semantics for such conditions.

That guarantee requires multiple replica coordination phases and costs much
more latency and capacity than a normal mutation. Use LWT for invariants that
actually require conditional agreement, not as the default write path.

## Batches Are Not a Bulk Loader

A logged batch helps mutations eventually apply together, including across
partitions, by first recording batch state. It does not turn arbitrary CQL into
a general ACID transaction with global isolation, and scattering a large batch
across many partitions can overload the coordinator.

Mutations within one partition already have useful atomicity and isolation
properties. Group a batch because the mutations belong together, not merely to
reduce client round trips.

---

# 14. Scaling and Topology Changes

## Bootstrap

When a node joins, token ownership changes and existing replicas stream the
required ranges to it. Streaming competes with foreground traffic, repair, and
compaction for disk and network capacity.

A node becoming visible in membership does not mean all assigned data arrived
instantaneously. Topology changes involve pending ranges and a transition
period during which the system must preserve availability and correctness.

## Decommission and Replacement

- **Decommission** streams a live node's ranges to remaining owners before it
  leaves.
- **Replacement** introduces a node for a failed endpoint and rebuilds data
  from surviving replicas.
- **Removal** of an unreachable node changes metadata and requires careful
  operator intent; failure detection alone does not evict it.

Do one topology operation at a time unless the exact Cassandra version and
procedure explicitly support more. Simultaneous range movement increases load
and makes recovery harder to reason about.

## Vnodes Do Not Eliminate Hotspots

Vnodes improve ownership distribution and movement granularity. They do not
split one hot partition: all rows with one partition key still share one
replica set.

If the example tenant/user/day partition becomes hot, fixes include a smaller
time bucket or an explicit shard suffix. Both make reads fan out across more
partitions. Capacity improves by spending query complexity.

---

# 15. Data Modeling from Queries

Cassandra performs best when the application already knows the partition key
and an ordered clustering range. Start with access patterns, then build tables
for them.

## One Query, One Table Shape

If the service also needs "all checkout failures for a tenant by hour," do not
scan `user_events_by_day` and filter. Build another table whose primary key
matches that query, for example:

~~~sql
CREATE TABLE telemetry.failures_by_tenant_hour (
  tenant_id text,
  event_hour timestamp,
  event_time timestamp,
  event_id timeuuid,
  user_id uuid,
  payload text,
  PRIMARY KEY ((tenant_id, event_hour), event_time, event_id)
) WITH CLUSTERING ORDER BY (event_time DESC, event_id DESC);
~~~

The application writes the event into both query tables. This denormalization
trades extra write and storage work for bounded, directly routable reads.

## Questions to Ask Before Creating a Table

- Which exact queries must it serve?
- Does every normal query provide the full partition key?
- How many rows and bytes can one partition accumulate?
- Can one partition become a tenant or celebrity hotspot?
- What are the TTL, update, and delete patterns?
- Which compaction strategy fits those patterns?
- How will repair and backup complete at the expected data size?
- Does the application tolerate duplicate or retried writes?
- Which invariant, if any, genuinely needs LWT?

Avoid `ALLOW FILTERING` as a substitute for a deliberate access path. It can
turn a key-routed operation into unpredictable scanning whose cost grows with
data rather than returned rows.

---

# 16. Capacity and Operations

## Disk Capacity Includes Amplification

Raw logical data is only the start:

~~~text
replicated bytes ~= logical bytes * replication factor
usable disk must also cover:
  compaction overlap
  repair and streaming
  snapshots/backups
  tombstones and expired data awaiting collection
  free-space safety margin
~~~

Do not plan a node to run near full. Compaction may need to write new SSTables
before old inputs can be deleted, and streaming a failed node's ranges consumes
the headroom precisely when the cluster is degraded.

## The Important Signals

Monitor the path, not only process liveness:

- coordinator read and write p50/p95/p99 latency;
- timeouts, unavailable errors, and dropped mutations;
- pending compactions and compaction throughput;
- SSTables per read and Bloom-filter effectiveness;
- tombstones scanned and large-partition warnings;
- commit-log and memtable pressure;
- disk utilization, IOPS, throughput, and queue depth;
- hints created, stored, and replayed;
- repair age, failures, and streamed bytes;
- cross-node latency and client coordinator locality;
- ownership balance by bytes and traffic, not token count alone.

## Failure Capacity

Steady-state utilization is not enough. With one node down, surviving replicas
receive its coordinator and replica work while repair or rebuild adds more I/O.
Capacity planning must keep the target consistency level available and latency
acceptable during that degraded state.

## A Practical Operational Loop

1. load-test the actual schema and value sizes;
2. measure read and write amplification under sustained compaction;
3. test a node failure at production-like utilization;
4. verify hints and repair catch up;
5. test bootstrap, replacement, and restore procedures;
6. confirm clients use token-aware, local-DC routing;
7. alert on repair age and compaction debt before disk fills.

---

# 17. End-to-End Example

The complete event mutation now looks like this:

1. The C++ service issues the CQL insert at `LOCAL_QUORUM`.
2. Its token-aware driver selects a nearby replica, `N1`, as coordinator.
3. `N1` serializes `(acme, user-42, 2026-08-17)` and computes token `t`.
4. Token metadata and `NetworkTopologyStrategy` map `t` to `N1`, `N2`, `N3`.
5. The coordinator sends the timestamped mutation to all three.
6. `N1` and `N2` append to commit logs and update memtables.
7. `N3` is unreachable, so the coordinator stores a hint.
8. Two local acknowledgements satisfy `LOCAL_QUORUM`; the client succeeds.
9. A later flush writes new immutable SSTables on `N1` and `N2`.
10. A quorum read merges memtables/SSTables and returns the event.
11. `N3` returns and receives the replayed hint.
12. Scheduled repair later verifies replica convergence rather than assuming
    the hint covered every missed mutation.
13. Compaction eventually merges obsolete versions and reclaims safe
    tombstones or expired cells.

![The complete lifecycle of one Cassandra mutation](/assets/img/cassandra/end-to-end.svg)

Each stage answers a different question:

| Stage | Question answered |
|---|---|
| schema | which rows belong together? |
| partitioner | which token owns this partition? |
| replication strategy | which nodes keep copies? |
| consistency level | how many responses complete this operation? |
| commit log + memtable | how does a replica acknowledge quickly and recover? |
| SSTable + compaction | how is immutable disk state organized? |
| hints + repair | how do replicas converge after missed writes? |

---

# 18. What Cassandra Guarantees—and What It Does Not

Cassandra provides:

- deterministic partition placement and topology-aware replication;
- atomic, isolated mutations within a partition under documented limits;
- local crash recovery through commit log plus SSTables, subject to the
  configured commit-log synchronization policy;
- tunable per-operation replica acknowledgement;
- timestamp-based reconciliation of ordinary mutations;
- linearizable conditional updates through LWT/Paxos;
- mechanisms for replica convergence after failure.

Cassandra does not provide automatically:

- a leader or consensus order for every ordinary write;
- fresh reads at weak consistency levels;
- cross-partition relational joins;
- globally isolated arbitrary multi-partition transactions;
- balanced traffic merely because tokens are balanced;
- guaranteed convergence from hints or incidental reads alone;
- an automatic repair schedule tailored to the deployment;
- a good schema independent of application query patterns.

The system works when these boundaries are part of the application and
operational design, not discovered after production data has accumulated.

---

# 19. Conclusion

Cassandra's normal write path is short because it deliberately postpones other
work. The coordinator maps one partition to a replica set. Replicas append to a
commit log and update memtables. The requested acknowledgement count completes
the operation without a leader election or consensus round.

The postponed work is real: SSTables must be compacted, divergent replicas must
be reconciled, tombstones must remain long enough to suppress stale data, and
operators must run repair. The schema must keep queries local and partitions
bounded, while client consistency levels express the failure/latency tradeoff
the application can actually tolerate.

That is the useful mental model for Cassandra:

~~~text
query-shaped partition
    -> deterministic token and replica set
    -> leaderless replicated mutation
    -> local LSM persistence
    -> tunable acknowledgement
    -> explicit convergence and repair
~~~

Its scale comes not from making coordination disappear, but from keeping most
coordination off the ordinary data path and making its remaining costs visible
to the designer.

---

# References

1. [Apache Cassandra architecture overview](https://cassandra.apache.org/doc/latest/cassandra/architecture/overview.html)
2. [Dynamo-style partitioning, replication, consistency, and membership](https://cassandra.apache.org/doc/latest/cassandra/architecture/dynamo.html)
3. [Apache Cassandra storage engine](https://cassandra.apache.org/doc/stable/cassandra/architecture/storage-engine.html)
4. [CQL data definition and primary-key semantics](https://cassandra.apache.org/doc/latest/cassandra/developing/cql/ddl.html)
5. [Hinted handoff](https://cassandra.apache.org/doc/stable/cassandra/managing/operating/hints.html)
6. [Anti-entropy repair](https://cassandra.apache.org/doc/latest/cassandra/managing/operating/repair.html)
7. [Compaction strategies](https://cassandra.apache.org/doc/stable/cassandra/managing/operating/compaction/overview.html)
8. [Cassandra consistency guarantees and lightweight transactions](https://cassandra.apache.org/doc/stable/cassandra/architecture/guarantees.html)
