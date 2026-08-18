---
layout: single
comments: true
title: "Inside Replication: Logs, Acknowledgements, Failover, and Convergence"
date: 2022-01-11 00:00:00-0000
description: "A connected account-update story explaining why systems replicate, leader-follower and leaderless protocols, acknowledgement boundaries, stale reads, failover, split brain, quorums, repair, and reconfiguration."
tags: [replication, leader-follower, quorums, failover, consistency, distributed-systems]
categories: ['Distributed Systems Components']
---

# 1. Begin With One Durable Copy

Ledger stores account `A7` on one database server:

~~~text
balance(A7) = £500
~~~

A client withdraws £100. The server validates the balance, appends the change
to durable storage, updates its index, and returns success:

~~~text
client -> one server -> one durable copy
~~~

This model has one authority and one commit order. It is easy to reason about.
Replication should not be introduced merely because multiple machines sound
more distributed.

The limitation is failure. If the server or its storage is destroyed, the
latest state may be unavailable or lost. Maintenance also interrupts service,
and readers far from the server pay the full network distance.

![One copy has simple authority but one failure and locality boundary](/assets/img/replication-internals/one-copy.svg)

Replication creates additional copies so the system can survive selected
failures, move service, or place reads nearer clients. The copies create a new
problem: **which operations have reached which replicas, in what order, and
which copy may act as authority after failure?**

---

# 2. What Replication Does—and Does Not—Provide

A **replica** is a copy of logical state maintained by applying an ordered or
reconciled stream of changes. A replication protocol defines:

- where writes are accepted;
- how changes are identified and transmitted;
- which acknowledgements complete a client operation;
- what reads may observe;
- how lagging copies catch up;
- how authority changes after failure;
- how concurrent histories reconcile.

![Replication separates copies, authority, acknowledgement, and read policy](/assets/img/replication-internals/replication-contract.svg)

Replication is not backup. A mistaken delete, corrupt application write, or
malicious mutation can be copied perfectly to every replica. Backups preserve
older or isolated recovery points. Production systems commonly need both.

Replication also does not create write capacity automatically. A single-leader
system may distribute reads while every write still passes through one leader.
Each additional synchronous replica can increase durability and reduce write
availability or latency.

---

# 3. State the Goal Before Choosing a Protocol

Different systems replicate for different reasons:

| Goal | Relevant question |
|---|---|
| Durability | How many independent failures may occur after acknowledgement? |
| Availability | Which failures still allow reads or writes? |
| Read scale | May followers serve reads, and how stale may they be? |
| Geography | Which operations can complete without a cross-region round trip? |
| Maintenance | Can authority move without an outage or lost committed state? |
| Disaster recovery | How much recent data may be lost if a region disappears? |

The recovery point objective, recovery time objective, read-consistency model,
and failure domains should be explicit. "Three replicas" alone says none of
those things.

---

# 4. Leader–Follower Replication Creates One Write Authority

Ledger runs three replicas. `R1` is leader; `R2` and `R3` are followers:

~~~text
client write -> R1 leader -> replication log -> R2, R3
~~~

![One leader orders writes and followers reproduce that order](/assets/img/replication-internals/leader-follower.svg)

The leader validates and orders writes. Each change receives a monotonically
increasing log position:

~~~text
position 841: withdraw(A7, £100)
position 842: deposit(A9, £40)
~~~

Followers persist and apply the same stream in order. Their state may lag, but
one leader prevents ordinary concurrent histories from being created at
several replicas.

The leader can replicate logical operations, changed rows, or physical storage
records. Whatever is sent must reproduce equivalent committed state on the
followers and survive restart according to the advertised durability contract.

---

# 5. The Acknowledgement Boundary Defines Durability

The client cares about the moment success is returned. Consider three policies.

## Leader-Only Acknowledgement

`R1` responds after local durability. Latency is low, but if `R1` is destroyed
before replication, a promoted follower does not contain the acknowledged
withdrawal.

## One Synchronous Follower

`R1` waits for at least one follower to persist the entry. The system can lose
one qualifying copy without losing the write, assuming promotion chooses a
replica containing committed positions.

## Every Follower

Waiting for all copies maximizes immediate replication but lets the slowest or
partitioned follower stop every write. Most fault-tolerant protocols instead
wait for a defined quorum or required subset.

![Different acknowledgement points expose different loss and latency windows](/assets/img/replication-internals/ack-boundaries.svg)

"Synchronous replication" is incomplete unless it says which replicas, which
durable event on those replicas, and what happens when one cannot respond.

---

# 6. Replication Lag Is a Position Difference

Suppose the leader has persisted through position 850:

~~~text
R1 leader    durable=850 applied=850
R2 follower  durable=850 applied=848
R3 follower  durable=844 applied=844
~~~

Durable and applied positions are different. `R2` can recover entry 850 after
a restart but may not expose its effects to reads yet. `R3` is missing six log
entries.

![Durable, applied, and acknowledged positions expose replication progress](/assets/img/replication-internals/log-positions.svg)

Measure lag in entries, bytes, and time. Time lag can look small when clocks are
wrong; entry lag can look small when one entry is enormous. Retained logs must
cover the maximum expected outage or the follower will need a new snapshot.

---

# 7. Follower Reads Need an Explicit Consistency Contract

After the withdrawal commits on `R1`, a client reads from lagging `R3` and sees
£500 instead of £400.

![A follower can serve an older state after the leader acknowledged a write](/assets/img/replication-internals/stale-read.svg)

Possible contracts include:

- **eventual:** follower may return its latest applied state;
- **bounded staleness:** serve only if lag is within a threshold;
- **read-your-writes:** route a session to a replica at or beyond its write
  position;
- **monotonic reads:** never move a session backward to an older position;
- **linearizable:** read through the leader or prove the serving replica is
  current through a quorum, lease, or read barrier.

The API can return a commit position with the write and require later reads to
wait until `applied >= position`. Geography and read scaling then have visible
consistency costs instead of an unspecified "read replica" promise.

---

# 8. Failover Is a Data-Safety Election

When `R1` stops responding, selecting the nearest or fastest follower is not
enough. The new leader must contain every entry the protocol considers
committed and must prevent the old leader from continuing to accept protected
writes.

![Promotion must preserve committed history and establish a new authority generation](/assets/img/replication-internals/failover.svg)

A safe failover normally needs:

1. a failure detector decides the leader is unavailable;
2. candidates compare terms, epochs, and log positions;
3. a quorum authorizes one new generation;
4. the winner finishes or truncates log state according to the protocol;
5. routing moves writes to the new leader;
6. the old leader is fenced before it can rejoin.

The recovery time objective includes detection, election, catch-up, routing
propagation, and client retry—not only the election algorithm.

---

# 9. Split Brain Is Two Write Authorities

`R1` is partitioned from `R2` and `R3` but remains reachable to some clients.
The pair promotes `R2`. If `R1` continues accepting writes, two histories form:

~~~text
R1: 841, 842, 843a
R2: 841, 842, 843b, 844b
~~~

![A partitioned old leader and a promoted leader create incompatible histories](/assets/img/replication-internals/split-brain.svg)

Quorum terms, leases with strict assumptions, or external fencing must ensure
only one generation can commit. On rejoin, the old leader cannot simply append
its divergent suffix. The protocol must reject, truncate, or reconcile it.

DNS or load-balancer health checks only redirect clients. They do not revoke
the old leader's authority to mutate storage.

---

# 10. Snapshots Bootstrap Replicas That Fell Too Far Behind

Logs cannot grow forever. Once old entries are compacted, a follower missing
those positions needs a snapshot:

~~~text
snapshot state through position 10,000
remaining log begins at 10,001
~~~

![A snapshot installs a base state before incremental log catch-up](/assets/img/replication-internals/snapshot-catchup.svg)

Snapshot installation must identify the included position, verify integrity,
and switch atomically from old state. Copying a live data directory without a
consistent boundary can combine files from different logical moments.

Bootstrap traffic should be throttled and topology-aware. Adding one replica
should not saturate the leader or the same network link used by foreground
requests.

---

# 11. Leaderless Replication Accepts Writes at Several Replicas

In a leaderless design, a coordinator sends a versioned write to several
natural replicas:

~~~text
coordinator -> R1, R2, R3
complete after W acknowledgements
~~~

![Leaderless replication replaces one write authority with versioned replica coordination](/assets/img/replication-internals/leaderless.svg)

This avoids routing every write through one permanent leader and can remain
writable through different failures. It also permits replicas to accept
concurrent versions. The system needs a version relation and reconciliation
rule rather than assuming arrival order is globally meaningful.

Wall-clock last-write-wins is simple but can discard a causally later update
when clocks move. Logical versions, hybrid clocks, or version vectors can expose
ordering and concurrency more safely, while the application may still need to
merge concurrent business values.

---

# 12. Quorum Arithmetic Is Not a Complete Protocol

For `N` replicas, a write may wait for `W` and a read may query `R`. The common
intersection condition is:

~~~text
R + W > N
~~~

For `N=3`, `W=2`, and `R=2`, every read set overlaps every completed write set
in at least one replica.

![Read and write quorums overlap in at least one replica](/assets/img/replication-internals/quorum-overlap.svg)

Intersection does not by itself guarantee a linearizable register. The read
must identify the newest valid version; concurrent coordinators, sloppy
quorums, failed writes, membership changes, and read repair all affect the
result. A write that reached one replica before timing out may later reappear.

Quorum numbers state how many responses are required. The surrounding protocol
states what those responses mean.

---

# 13. Hints and Repair Solve Different Gaps

If `R3` is temporarily unavailable, another node may store a **hint** describing
the missed write and replay it when `R3` returns. Hints shorten common outage
windows; they are not a permanent convergence proof.

Repair compares replica state and transfers missing or conflicting ranges.
Read repair fixes keys encountered by reads. Anti-entropy repair covers cold
keys that clients may never read.

![Hints repair recent delivery while anti-entropy covers the complete keyspace](/assets/img/replication-internals/hints-repair.svg)

Deletion makes repair safety harder. A tombstone must remain long enough that
every stale replica is repaired; otherwise an old value can return after the
deletion marker is compacted away.

---

# 14. Replicas Must Cross Real Failure Domains

Three replicas on one host do not survive host failure. Three hosts on one
power circuit do not survive site failure. Three regions may survive a site but
add latency, cost, and correlated software risk.

Placement should consider:

- process, disk, host, rack, zone, and region;
- network and power topology;
- cloud account or administrative boundary;
- common software and configuration faults;
- latency of the acknowledgement set;
- bandwidth required for catch-up and repair.

Replica count and independent failure tolerance are not synonymous.

---

# 15. Membership Change Must Preserve Authority Overlap

Moving from replicas `{R1,R2,R3}` to `{R2,R3,R4}` cannot be implemented by
letting different clients independently use either configuration. Old and new
groups might each authorize conflicting writes.

![Safe reconfiguration overlaps authority while a new replica catches up](/assets/img/replication-internals/reconfiguration.svg)

A safe sequence typically:

1. adds `R4` as a non-voting or non-authoritative learner;
2. catches it up to the committed position;
3. commits a configuration transition with overlapping authority;
4. activates `R4` in the new configuration;
5. removes `R1` only after the transition is durable.

Consensus systems formalize this through joint consensus or another
overlapping-quorum mechanism. Leaderless systems likewise need versioned
topology so coordinators agree which replicas constitute `N`.

---

# 16. Failure Matrix

| Failure | Risk | Required response |
|---|---|---|
| follower down | growing lag | retain log or install snapshot later |
| leader destroyed after local ack | acknowledged write loss | synchronous durable copy or accept RPO |
| old leader isolated | split brain | quorum generation and fencing |
| follower read after write | stale result | position-aware session or stronger read |
| write reply lost | caller outcome unknown | idempotency and status lookup |
| replica rejoins with divergent suffix | conflicting history | term/version validation and reconciliation |
| repair misses deleted key | value resurrection | tombstone horizon and complete repair |
| new replica serves too early | incomplete reads | catch-up gate before eligibility |
| entire zone fails | correlated replica loss | topology-aware placement and capacity |

---

# 17. Operations and Testing

Observe:

- leader term, identity, and changes;
- durable, replicated, committed, and applied positions;
- lag in time, entries, and bytes;
- acknowledgement latency by replica and failure domain;
- follower-read staleness and wait-for-position latency;
- election, catch-up, snapshot, and route-change duration;
- divergent version and reconciliation counts;
- repair age, throughput, backlog, and tombstone horizon;
- replica placement violations and failover headroom.

Test the interleavings averages hide:

1. kill the leader before and after each acknowledgement boundary;
2. partition the old leader from quorum but not clients;
3. pause a follower through log compaction;
4. lose the client reply after commit and retry;
5. change membership while one replica is unreachable;
6. corrupt a snapshot or one log segment;
7. delay repair beyond the deletion-retention window;
8. fail an entire zone at peak load.

---

# 18. The Complete Withdrawal

1. The client sends `withdraw(A7, £100)` with request ID `req-91`.
2. Leader `R1` validates the balance and assigns log position 841.
3. `R1` and required followers persist the entry.
4. The configured acknowledgement boundary marks it committed.
5. The client receives success plus position 841.
6. A later follower read waits until `applied >= 841` for read-your-writes.
7. `R1` becomes partitioned from quorum.
8. `R2` is elected in a higher generation because it contains committed 841.
9. Fencing prevents `R1` from committing an alternative position 842.
10. Routing sends new writes to `R2`.
11. `R1` rejoins, recognizes the newer generation, and catches up.
12. Repair and monitoring confirm every intended replica contains the history.

![One acknowledged update crosses ordering, durability, reading, and failover boundaries](/assets/img/replication-internals/end-to-end.svg)

---

# 19. Final Mental Model

Replication is not "copy data to three servers." It is a protocol for deciding
which history each copy may expose and which failures an acknowledgement
survives:

~~~text
client operation
  -> authority or coordinator
  -> versioned log/state change
  -> replica persistence
  -> acknowledgement boundary
  -> read visibility contract
  -> failover generation
  -> catch-up, repair, and reconfiguration
~~~

A leader makes one order explicit but concentrates the write path. A leaderless
design distributes write coordination but must expose and reconcile concurrent
versions. Synchronous copies reduce loss windows but add latency and failure
dependencies. Asynchronous copies improve the common path but permit lag and a
non-zero recovery point.

The correct design starts with the required failure and consistency contract,
then chooses replicas, acknowledgement, reading, and recovery mechanisms that
actually implement it.

---

# References

1. [Viewstamped Replication Revisited](https://pmg.csail.mit.edu/papers/vr-revisited.pdf)
2. [In Search of an Understandable Consensus Algorithm](https://raft.github.io/raft.pdf)
3. [Dynamo: Amazon's Highly Available Key-value Store](https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf)
4. [Chain Replication for Supporting High Throughput and Availability](https://www.cs.cornell.edu/home/rvr/papers/OSDI04.pdf)
