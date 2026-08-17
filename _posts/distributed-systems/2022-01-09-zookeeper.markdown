---
layout: single
comments: true
title: "Inside ZooKeeper: Znodes, Sessions, Watches, Zab, and Fencing"
date: 2022-01-09 00:12:00-0400
description: "A connected journey through ZooKeeper's namespace, sessions, watches, Zab write protocol, leader recovery, recipes, and fencing under failure."
tags: [zookeeper, distributed-systems, coordination, consensus, zab, leader-election]
categories: ['Distributed Systems Components']
---

# 1. One Coordination Story

Imagine an online shop named **Orchard**. Its checkout service sends payment
requests to a pool of workers. Workers come and go during deployments, so
checkout instances need a current worker list. One worker also acts as the
controller that assigns reconciliation jobs. Configuration can change while
the system is running, and no two controllers may safely update the same
external ledger at once.

The application needs answers to four small but difficult questions:

1. Which payment workers are alive?
2. How do clients learn that the membership changed?
3. Which worker is the current controller?
4. How does the ledger reject a controller that lost leadership but has not
   realized it yet?

ZooKeeper answers these questions with a deliberately small substrate:

- a replicated hierarchy of small records called **znodes**;
- **sessions** that give temporary ownership a lifetime;
- ordered, conditional updates;
- **watches** that tell clients to re-read state;
- a leader-based atomic broadcast protocol named **Zab**.

Service discovery, leader election, configuration, and locks are not separate
server features. They are client-side recipes composed from those primitives.

![The Orchard system and its ZooKeeper coordination plane](/assets/img/zookeeper/story-overview.svg)

This article follows one worker, `payments-3`, through the entire lifecycle. It
registers an ephemeral znode. Checkout clients watch the worker directory. A
write passes through Zab. The worker joins an election. Then a network
partition expires its session, another controller takes over, and a fencing
token prevents the old controller from corrupting the ledger.

That continuity matters. ZooKeeper is easiest to understand as a sequence of
state transitions, not a list of APIs.

## Why a Separate Coordination Service?

Orchard could place membership in a database table and poll it. That immediately
creates more questions: who removes a crashed worker, how quickly, under which
clock, and how do thousands of clients avoid polling together? It could embed a
consensus protocol inside every service, but then every service must implement,
test, deploy, and operate the same failure-sensitive machinery.

ZooKeeper centralizes the small amount of strongly ordered metadata that helps
other distributed systems coordinate. It is not the data plane for payments.
Payment events and ledger entries remain in systems built for large data. Only
their coordination state lives in ZooKeeper.

**Design boundary:** if Orchard starts storing receipts, images, or event
history in znodes, it has turned a coordination service into a badly shaped
database.

---

# 2. The Ensemble Behind the Story

Orchard runs five ZooKeeper servers across separate failure domains:

~~~text
Z1, Z2, Z3, Z4, Z5
~~~

At one moment, `Z2` is the ZooKeeper leader and the others are followers.
Clients can connect to any of them. This creates two different request paths:

- a read is normally served by the connected server from its local replicated
  state;
- a write is ordered by the leader and committed through a voting quorum.

The leader is an implementation role inside the ensemble. It is unrelated to
the Orchard payment controller elected by application znodes. ZooKeeper may
elect `Z2` as its protocol leader while Orchard elects `payments-3` as its
business controller.

## Voting Participants and Observers

A five-participant ensemble needs three votes for a quorum:

~~~text
quorum(5) = floor(5 / 2) + 1 = 3
~~~

It remains available after two participant failures, assuming the remaining
three can communicate. Adding a sixth voting server does not increase the
number of failures tolerated: both five and six require three surviving
failures to lose a majority. Odd participant counts usually buy fault tolerance
more efficiently.

An **observer** receives committed state and can serve clients, but does not
vote in leader election or Zab acknowledgement quorums. Observers can add read
capacity or provide a local endpoint without increasing the size and latency of
the voting quorum.

![Five voting participants, leader, followers, and an observer](/assets/img/zookeeper/ensemble-roles.svg)

## Availability Is Majority Availability

The ensemble does not continue accepting writes merely because some server is
alive. A partition containing `Z1` and `Z2` has two of five votes and cannot
form a quorum. A partition containing `Z3`, `Z4`, and `Z5` can elect a leader
and continue.

That majority rule is what prevents two sides from committing divergent write
histories. Every pair of majorities intersects in at least one server.

Now that the ensemble can order metadata, Orchard needs a shape for that
metadata.

---

# 3. The Namespace Becomes the Shared Story

ZooKeeper exposes a hierarchical namespace. A znode can hold a byte array and
also have children, so it resembles a file and directory at the same time.
Paths are absolute.

Orchard uses this tree:

~~~text
/orchard
├── config
│   └── checkout-routing
├── workers
│   ├── worker-0000000041
│   ├── worker-0000000042   <- payments-3
│   └── worker-0000000043
└── election
    ├── candidate-0000000017
    ├── candidate-0000000018
    └── candidate-0000000019
~~~

The worker znodes contain small endpoint records such as
`10.24.8.19:8443|zone-b`. The configuration znode contains a small versioned
document. Election znodes identify controller candidates.

![The connected Orchard znode namespace](/assets/img/zookeeper/namespace-tree.svg)

## Persistent, Ephemeral, and Sequential

The names describe lifecycle and naming behavior:

| Property | Meaning | Orchard use |
|---|---|---|
| persistent | remains until explicitly deleted | configuration and parent paths |
| ephemeral | removed when the owning session ends | live worker and election entries |
| sequential | server appends an increasing suffix under the parent | ordered membership and election candidates |

Properties can be combined. `payments-3` creates an
`EPHEMERAL | SEQUENTIAL` child using the prefix
`/orchard/workers/worker-`. ZooKeeper returns the actual path ending in
`0000000042`.

Ephemeral znodes cannot have children. Their lifetime belongs to a session, not
to a TCP socket, process ID, hostname, or application heartbeat timestamp.

## Data and Stat Travel Together

Reading a znode returns its data plus a `Stat` structure. Important fields
include:

- `version`: number of data changes;
- `cversion`: number of child-list changes;
- `aversion`: number of ACL changes;
- `czxid`: transaction that created the znode;
- `mzxid`: transaction that last changed its data;
- `pzxid`: transaction that last changed its children;
- `ephemeralOwner`: owning session ID, or zero for a non-ephemeral znode.

Versions support optimistic concurrency. If two administrators read
`checkout-routing` at version 8, only the first `setData(path, bytes, 8)` can
advance it to version 9. The second receives a bad-version result instead of
silently overwriting a concurrent change.

A **zxid** orders transactions across the entire ZooKeeper history; a znode
version counts changes to one znode. They answer different questions.

## Keep Znodes Small

ZooKeeper atomically reads or replaces an entire znode value. It is designed
for coordination data measured in kilobytes, with a hard sanity limit below
one megabyte per znode in normal configurations. Large values increase network,
serialization, snapshot, recovery, and tail-latency costs for the ensemble.

The namespace can now describe a worker. A session gives that description a
meaningful lifetime.

---

# 4. A Session Gives Membership a Lifetime

`payments-3` creates a ZooKeeper client with the ensemble address list and a
requested session timeout. The library connects to one server and negotiates a
timeout. ZooKeeper assigns a 64-bit session ID and credentials that allow the
session to reconnect through another server.

A **connection** is one network channel to one server. A **session** is logical
state that can survive a connection change.

This distinction drives the worker story:

1. `payments-3` connects to `Z4` and creates its ephemeral worker znode.
2. `Z4` fails, breaking the TCP connection.
3. The client library reconnects the same session through `Z1` before timeout.
4. The ephemeral znode remains; checkout does not see a false worker removal.

![Connection loss, session reconnect, and session expiration](/assets/img/zookeeper/session-lifecycle.svg)

## Disconnected Is Not Expired

When connectivity is lost, the client enters `DISCONNECTED`. It cannot know
whether the ensemble can still hear from the session through another delayed
path or whether the ensemble will soon expire it. The client should enter a
safe mode rather than inventing a local answer.

If the same session reconnects before the negotiated timeout, it returns to
`CONNECTED`. If the ensemble hears nothing for the timeout, it expires the
session. Expiration is decided by the ZooKeeper service, not the disconnected
client.

Expiration causes a replicated state transition:

1. the session becomes invalid;
2. its ephemeral znodes are deleted;
3. those deletions receive ordered zxids;
4. relevant watches are triggered;
5. the old session can never be revived.

The client may only learn that it expired when network connectivity returns.
By then another Orchard worker may already be controller.

## Timeout Is a Correctness–Recovery Tradeoff

A short timeout removes dead workers quickly but turns brief network stalls,
GC pauses, and overloaded servers into session expiration. A long timeout
survives longer pauses but delays membership cleanup and failover.

The timeout must be designed alongside:

- maximum expected process pause;
- network failover time;
- application recovery objective;
- cost of duplicate controllers;
- fencing support at external resources.

Ephemeral membership solves cleanup, but checkout instances still need to
learn that the child list changed. That is the job of watches.

---

# 5. Watches Turn Membership Into Discovery

At startup, a checkout instance calls `getChildren("/orchard/workers", watch)`.
The response contains a snapshot of current child names and installs a child
watch on the connected ZooKeeper server.

When `payments-3` registers, the parent child list changes. After the create is
committed and applied, ZooKeeper emits a watch event. The event does not contain
the new authoritative worker list. It means:

> Something relevant changed. Read the state again.

The checkout instance reissues `getChildren` with a new watch and replaces its
local routing snapshot.

![Read, install watch, mutate, notify, and re-read](/assets/img/zookeeper/watch-cycle.svg)

## Standard Watches Are One-Shot

A standard watch fires once and is removed. The safe loop is therefore:

~~~text
read state and install watch atomically
    -> use returned snapshot
    -> receive notification
    -> read state and install the next watch
    -> replace snapshot
~~~

Several changes may happen between notification and the next read. The client
must converge to current state, not assume one event per mutation. ZooKeeper
3.6 and later also support persistent and persistent-recursive watches, but
notifications still indicate change; clients should derive truth from state.

## C++: The Entire Discovery Loop

The application logic can remain compact when a client library wraps the raw
callbacks:

~~~cpp
void WorkerDirectory::refresh() {
    auto children = zk.getChildren(
        "/orchard/workers",
        [this](const WatchEvent&) { refresh(); });

    std::vector<Endpoint> next;
    for (const auto& child : children) {
        next.push_back(decode(zk.getData(
            "/orchard/workers/" + child)));
    }
    routes.replace(std::move(next));
}
~~~

Production code must serialize refreshes, handle session events, retry
recoverable failures, tolerate a child disappearing between `getChildren` and
`getData`, and avoid doing slow work on the watch callback thread. The important
pattern is visible: notification leads to a new read, and the read replaces
local state.

## Watch Ordering and Gaps

ZooKeeper orders watch events with updates and asynchronous replies for a
client. A client will receive the watch event before it observes the new data
through that connection.

Watches are not a durable event log:

- no watches arrive while disconnected;
- an existence watch can miss a create-and-delete entirely while disconnected;
- different clients need not receive events at the same wall-clock instant;
- watching every child can consume server memory and create notification load.

On disconnection, Orchard checkout freezes or conservatively ages its last
worker snapshot. On reconnection, it re-reads the directory. It never treats a
count of watch callbacks as the source of truth.

The create and delete operations that trigger those watches must appear in one
order on every server. Zab supplies that order.

---

# 6. One Registration Through Zab

Follow the registration of `payments-3`. The client happens to be connected to
follower `Z4`, while `Z2` is leader.

## Step 1: Forward to the Leader

`Z4` accepts the create request and forwards the write toward `Z2`. Clients do
not need to discover and connect directly to the ZooKeeper leader.

## Step 2: Assign a zxid and Propose

The leader validates the request, chooses the sequential suffix, and assigns a
zxid. A zxid is a 64-bit transaction identifier:

~~~text
zxid = (epoch, counter)
~~~

The epoch identifies a period of ZooKeeper leadership. The counter increases
for proposals within that epoch. Lexicographic ordering of the pair exposes the
global transaction order.

`Z2` broadcasts the proposal to followers. Zab pipelines proposals, so it can
have several writes in flight while preserving FIFO proposal order.

## Step 3: Persist and Acknowledge

Participants append the proposal to their transaction logs before acknowledging.
When the leader has acknowledgements from a voting quorum—including itself—it
can commit the proposal.

In a five-node ensemble, three durable acknowledgements suffice. Observers do
not count toward this quorum.

## Step 4: Commit, Apply, Notify, Reply

The leader broadcasts the commit. Servers apply the create to their in-memory
data tree in zxid order. The server holding the checkout watch can now enqueue
the child-change event. The originating server returns the created sequential
path to `payments-3`.

![The complete ZooKeeper Zab write path](/assets/img/zookeeper/zab-write-path.svg)

The simplified foreground path is:

~~~text
client -> connected server -> leader
       -> durable proposal on quorum
       -> commit broadcast
       -> ordered apply
       -> watch notification and client response
~~~

## What the Acknowledgement Means

A successful create means the write was committed in ZooKeeper's global order.
It does not mean every follower has applied it at the exact response instant.
Lagging followers learn and apply committed proposals in order.

A connection-loss result is different. The server may have committed the
create and lost the response, or it may never have processed it. This outcome
ambiguity is especially important for sequential creates because retrying can
create a second child with a different suffix.

Recipes commonly include a unique client GUID in the prefix or data, then scan
children after reconnecting to determine whether the original create succeeded
before retrying.

The Zab path explains linearizable writes. Reads deliberately take a shorter
path and therefore have a different guarantee.

---

# 7. Reads Are Fast Because They Are Local

Checkout is connected to `Z5`. When it reads `/orchard/workers`, `Z5` normally
answers from its local data tree without asking a quorum and without forwarding
the read to `Z2`.

That keeps read latency and throughput attractive for read-heavy coordination
workloads. It also means `Z5` can briefly lag the newest committed transaction.

## The Actual Consistency Contract

ZooKeeper provides several connected guarantees:

- writes are linearizable and globally ordered;
- each client's operations preserve program order;
- a client's view never moves backward after it has observed a zxid;
- reads are sequentially consistent but may be stale;
- watches are ordered with the updates observed by the client.

ZooKeeper does **not** guarantee that two clients connected to different
servers have identical views at every wall-clock instant.

![A committed write and temporarily different client read views](/assets/img/zookeeper/read-consistency.svg)

## Read-Your-Writes Without Global Freshness

If one client successfully writes and then reads, ZooKeeper preserves that
client's ordering as it moves between servers. That does not make an arbitrary
read by a different client linearizable.

Historically, applications used `sync(path)` before a read to advance the
connected server. Current ZooKeeper documentation makes an important formal
qualification: `sync` is not itself a quorum operation, so it is not a strict
proof of the freshest possible state under every theoretical failure. An actual
quorum operation before the read provides the stronger barrier.

Most discovery use cases do not need every read to be linearizable. They need
ordered changes, bounded staleness, session-aware failure handling, and a
re-read when notified. The design should state which is required rather than
calling all ZooKeeper operations "strongly consistent."

Now consider what happens when ZooKeeper's own leader fails with proposals in
flight.

---

# 8. Leader Failure and Zab Recovery

Suppose leader `Z2` has assigned three zxids:

~~~text
(7, 41) committed
(7, 42) acknowledged by a quorum, commit message partly delivered
(7, 43) seen only by Z2 and Z4
~~~

Then `Z2` crashes.

The ensemble must preserve `(7,42)` because a quorum accepted it. It must not
accidentally commit `(7,43)` merely because one possible leader saw it. Before
serving new writes, ZooKeeper enters leader election and synchronization.

## Discovery and Election

Peers exchange election state including epochs and zxids. The elected leader
must have a history capable of containing every committed proposal. Quorum
intersection ensures at least one voter from the old commit quorum participates
in the new quorum.

## Synchronization

The prospective leader establishes a common prefix with followers. Depending
on their state, learners may receive missing committed transactions, truncate
uncommitted suffixes, or install a snapshot plus subsequent log entries.

## New Epoch and Broadcast

Only after a quorum synchronizes with the new leader does it become active. It
uses a higher epoch—say 8—so new proposals cannot collide with zxids from the
old leadership period.

~~~text
(7,42) preserved and committed
(7,43) discarded if it was never committed
(8, 1) first proposal of the new leader
~~~

![Zab recovery preserves committed history and removes unsafe suffixes](/assets/img/zookeeper/zab-recovery.svg)

Leader activation is why Zab is more than "send a write to a majority." The
protocol coordinates an ordered stream and makes a new leader reconcile that
stream before accepting more writes.

ZooKeeper's internal leader is recovered. Orchard still needs to elect its
application controller from the worker sessions.

---

# 9. Application Leader Election Without a Herd

Each eligible payment worker creates an ephemeral sequential znode under
`/orchard/election`:

~~~text
candidate-0000000017  payments-3
candidate-0000000018  payments-7
candidate-0000000019  payments-9
~~~

The smallest suffix wins, so `payments-3` becomes controller.

## The Naive Watch

Every candidate could watch the smallest znode. When it disappears, every
candidate wakes, lists all children, sorts them, and attempts leadership. With
thousands of candidates, one failure creates a traffic spike known as the
**herd effect**.

## Watch the Predecessor

Instead, each non-leader watches only the candidate immediately before it:

~~~text
17  <- leader
18  watches 17
19  watches 18
20  watches 19
~~~

When 17 disappears, only 18 wakes. It re-lists the election directory, confirms
that it is now smallest, and becomes leader. If 18 disappears too, 19's watch
fires.

![Ephemeral sequential election with predecessor watches](/assets/img/zookeeper/election-queue.svg)

## The Correct Election Loop

The recipe must handle races:

1. create one ephemeral sequential candidate;
2. list and sort children;
3. if this candidate is smallest, become leader;
4. otherwise call `exists(predecessor, watch)`;
5. if the predecessor already vanished, return to step 2 immediately;
6. if the watch is installed, wait for deletion and return to step 2.

The `exists` result closes the gap between listing and installing the watch.
The candidate never assumes a watch event grants leadership; it re-reads and
proves that it is smallest.

Election establishes who ZooKeeper considers current. It does not physically
stop an old process from continuing to use an external database. That requires
fencing.

---

# 10. Election Is Not Fencing

Now partition `payments-3` from ZooKeeper but not from the payment ledger.

From the old controller's perspective:

- its process is alive;
- a reconciliation request is still running;
- it has not yet received a session-expired event.

From the ensemble's perspective, the session eventually expires. ZooKeeper
deletes candidate 17, triggers candidate 18's watch, and elects `payments-7`.
For a period, both processes may believe they can act.

An ephemeral znode removes an ownership claim inside ZooKeeper. It cannot reach
into the ledger and pause an old process.

![A stale controller and a fenced external resource](/assets/img/zookeeper/fencing.svg)

## Monotonic Fencing Tokens

When a candidate becomes controller, it derives a monotonically increasing
token from its ordered election record—for example the candidate creation zxid
stored in `Stat.czxid`. Every command to the ledger includes that token.

The ledger persists the greatest accepted token and rejects any smaller token:

~~~cpp
bool Ledger::apply(std::uint64_t fencingToken, Mutation mutation) {
    std::lock_guard lock(mu);
    if (fencingToken < greatestToken) return false; // stale controller

    greatestToken = fencingToken;
    commit(std::move(mutation));
    return true;
}
~~~

If `payments-7` first writes with token 92, a delayed command from
`payments-3` carrying token 87 is rejected. This protection exists only because
the **external resource validates the token atomically with the mutation**.

The new controller should establish its token at the protected resource before
performing work. A token checked only in application memory is not fencing.

## Disconnection Policy Still Matters

On `DISCONNECTED`, the old controller should stop starting new work as quickly
as practical. Fencing is the final correctness boundary for work already in
flight or for a paused process that resumes late. Safe-mode behavior reduces
conflicts; fencing makes stale conflicts harmless.

The same principle applies to locks. Acquiring a ZooKeeper lock grants an
ordered claim, but a resource that cannot reject stale holders may still be
damaged after pauses or partitions.

---

# 11. Other Recipes Reuse the Same Primitives

The story has already built the pieces used by most ZooKeeper recipes.

## Dynamic Configuration

Checkout reads `/orchard/config/checkout-routing` with its version and a watch.
An administrator updates it conditionally:

~~~text
setData(path, newBytes, expectedVersion=8)
~~~

Zab commits version 9. The data watch fires. Checkout re-reads and atomically
replaces its local configuration snapshot. If another administrator already
created version 9, the stale update fails rather than overwriting it.

## Exclusive Lock

Lock contenders create ephemeral sequential children. The smallest child owns
the lock; others watch their predecessors. This is the election recipe with a
different application meaning.

Correct error handling is essential. If `create` succeeds but the reply is
lost, blindly retrying creates two lock nodes. A GUID lets the client recover
its original node. A session expiration means the contender must start over,
not resume as if it still owned the lock.

## Atomic Multi-Operation

ZooKeeper's `multi` groups several checks and mutations into one atomic
transaction. Orchard could check a configuration version, update a pointer, and
create an audit marker so either all operations commit or none do.

This is atomicity inside the ZooKeeper namespace. It is not a transaction that
atomically spans ZooKeeper and the external ledger. Cross-system workflows
still need idempotency, fencing, or a protocol designed for both systems.

## ACLs Are Part of Coordination Correctness

Orchard should not leave the namespace world-writable. ZooKeeper ACLs control
create, delete, read, write, and administrative permissions. Workers may create
membership children without gaining permission to rewrite configuration.

Authentication and TLS protect different boundaries:

- client authentication identifies principals;
- znode ACLs authorize operations;
- client TLS protects client-server traffic;
- quorum TLS protects election and replication traffic between servers.

---

# 12. Failure Scenarios in the Running Story

## A Client Loses One Server

The library reconnects the same session to another server. Ephemeral nodes and
watches remain logically associated with the session. The application treats
the interval as disconnected and refreshes state after reconnecting.

## A Client Is Partitioned Past Its Timeout

The ensemble expires the session and deletes its ephemerals. The client cannot
resurrect that session. It creates a new session, re-registers, reinstalls
watches, and re-enters elections as a new candidate.

## The ZooKeeper Leader Fails

Followers stop processing new writes, elect a new leader, reconcile committed
history, establish a new epoch, and resume. Reads from connected servers may
continue only within the behavior of the current server state and connection;
applications must handle disconnection and retryable errors.

## A Minority of the Ensemble Is Isolated

It cannot elect an active leader or commit writes. Clients connected there are
redirected by reconnection behavior or become disconnected. The majority side
continues.

## A Write Response Is Lost

The result is unknown. Retrying `setData` with the same expected version is
often naturally detectable. Retrying an unconditional or sequential create may
duplicate state. Each operation needs a recovery rule.

## A Follower Is Slow

The leader does not need every follower to commit, but disk or network latency
on enough voting participants raises write latency or loses quorum. Slow
servers can also accumulate synchronization work and serve staler local reads.

## The Old Orchard Controller Keeps Running

Session expiration and election choose a new controller, but only the ledger's
fencing-token check rejects the old controller's delayed mutation.

![Failure outcomes across client, server, quorum, and resource boundaries](/assets/img/zookeeper/failure-matrix.svg)

---

# 13. Persistence and Recovery

Every voting server maintains a transaction log and periodic snapshots of the
in-memory data tree.

## Transaction Log

Zab acknowledgements depend on durable proposal logging. Sequential disk
latency therefore sits directly on the write path. ZooKeeper should not share a
busy data disk with compaction-heavy databases, log aggregation, or other
unpredictable I/O workloads.

## Snapshots

Snapshots bound recovery work by capturing the data tree. ZooKeeper snapshots
are paired with subsequent transaction-log replay to reconstruct committed
state. Old snapshots and logs require retention and purge configuration; an
unbounded data directory eventually becomes its own outage.

## Restart and Catch-Up

A restarted follower loads a snapshot, replays transactions, then synchronizes
with the leader before participating normally. Depending on how far it lags,
catch-up may send a transaction diff or a larger snapshot.

![Transaction log, snapshots, restart, and leader catch-up](/assets/img/zookeeper/storage-recovery.svg)

## Ensemble Metadata Is Small but Critical

Because ZooKeeper state is small, operators sometimes under-plan its storage.
The bytes may be modest, but their latency and durability decide whether every
dependent control plane can make progress. Fast, isolated, monitored storage is
more important than raw capacity.

---

# 14. Operating the Ensemble

## Placement

Place voting participants across independent machines and failure domains.
Five servers behind one switch or on one storage host are not five meaningful
failure domains. Keep quorum latency within the write-latency objective; a
geographically stretched voting ensemble pays WAN latency on coordination
writes and leader recovery.

## Session Capacity

Every connected client creates server-side connection and watch state. Avoid
one ZooKeeper session per application request. Long-lived service processes
should reuse managed clients, and applications should control watch cardinality.

## Signals That Tell the Story

Monitor:

- request latency and outstanding requests;
- leader and follower role changes;
- quorum health and election duration;
- transaction-log fsync latency;
- follower synchronization and packet backlog;
- active connections, sessions, and expirations;
- watch count and watch-related memory;
- znode count, data size, and large-request failures;
- snapshot age and data-directory growth;
- authentication, ACL, and TLS failures.

Useful health endpoints should be restricted and allow-listed. ZooKeeper's
administrative commands expose sensitive operational state and are not a public
application interface.

## Test the Failure Path

Orchard should rehearse:

1. one follower loss;
2. current ZooKeeper leader loss;
3. client reconnect within timeout;
4. client session expiration;
5. ambiguous sequential-create outcome;
6. stale Orchard controller rejected by the ledger;
7. snapshot restore and follower replacement;
8. rolling maintenance while preserving quorum.

If the test stops at "a new leader was elected," it has not tested fencing or
application recovery.

---

# 15. The Whole Story, End to End

The pieces now form one causal chain:

1. `payments-3` opens session 61 through follower `Z4`.
2. It creates `/orchard/workers/worker-` as ephemeral sequential.
3. `Z4` forwards the write to ZooKeeper leader `Z2`.
4. `Z2` assigns zxid `(7,42)` and proposes it.
5. Three participants persist and acknowledge; Zab commits the create.
6. The znode becomes `worker-0000000042` on the replicated data tree.
7. Checkout's child watch fires, so checkout re-reads and replaces its routes.
8. The worker creates `candidate-0000000017` under `/orchard/election`.
9. It is the smallest candidate and becomes Orchard controller with fencing
   token equal to its creation zxid.
10. A network partition disconnects `payments-3` from ZooKeeper.
11. The ensemble expires session 61 and commits deletion of its ephemerals.
12. Only successor candidate 18 wakes, rechecks, and becomes controller.
13. The new controller establishes its greater token at the ledger.
14. The old controller resumes and sends a delayed command with its old token.
15. The ledger rejects the command as stale.
16. `payments-3` reconnects, learns that its session expired, creates a new
    session, and starts again as a new worker and election candidate.

![The complete Orchard coordination story](/assets/img/zookeeper/end-to-end-story.svg)

No single primitive solved the whole problem:

| Need | ZooKeeper mechanism | Application responsibility |
|---|---|---|
| ordered metadata | Zab and zxids | keep metadata small |
| live membership | session-owned ephemeral znode | safe mode on disconnect |
| change notification | watch | re-read and replace state |
| controller election | ephemeral sequential recipe | predecessor watch and recovery |
| stale-controller safety | ordered token source | external resource enforces fencing |

---

# 16. What ZooKeeper Guarantees—and What It Does Not

ZooKeeper provides:

- globally ordered, linearizable writes;
- FIFO ordering for each client's operations;
- sequentially consistent, locally served reads;
- atomic znode data replacement and version checks;
- atomic multi-operation transactions within ZooKeeper;
- session-bound ephemeral state;
- ordered change notifications;
- quorum-based recovery of committed history.

ZooKeeper does not provide automatically:

- a general-purpose database or blob store;
- linearizable local reads from arbitrary followers;
- identical cross-client views at every instant;
- a durable stream containing every watch event;
- application leader fencing at an external resource;
- exactly-once effects after connection loss;
- a safe lock recipe without error recovery;
- availability without a voting majority;
- good ACLs, TLS, placement, or session timeouts by default.

Most ZooKeeper production failures come from crossing one of these boundaries:
using watch events as data, equating connection loss with session expiration,
assuming election stops an old process, retrying an ambiguous create blindly,
or placing large/high-volume application data in the coordination plane.

---

# 17. Conclusion

ZooKeeper turns a replicated ordered namespace into a coordination vocabulary.
Zab orders writes. Znodes give those writes names and versions. Sessions give
temporary ownership a lifetime. Watches tell clients when to re-read. Recipes
compose ephemeral and sequential znodes into discovery, elections, and locks.
Fencing carries ZooKeeper's order across the boundary to an external resource.

The Orchard story can be summarized as:

~~~text
session
  -> ephemeral registration
  -> committed Zab transaction
  -> watch notification
  -> state refresh
  -> sequential election
  -> session expiration
  -> successor election
  -> external fencing
~~~

The final arrow is the one most often missed. ZooKeeper can say who owns a
coordination claim now. A correctly designed system also prevents whoever owned
it before from continuing to cause side effects.

---

# References

1. [Apache ZooKeeper 3.9 Programmer's Guide](https://zookeeper.apache.org/doc/current/zookeeperProgrammers.html)
2. [Apache ZooKeeper Internals: atomic broadcast and consistency](https://zookeeper.apache.org/doc/current/zookeeperInternals.html)
3. [Apache ZooKeeper Recipes and Solutions](https://zookeeper.apache.org/doc/current/recipes.html)
4. [Apache ZooKeeper Administrator's Guide](https://zookeeper.apache.org/doc/current/zookeeperAdmin.html)
5. [ZooKeeper: Wait-free coordination for Internet-scale systems](https://www.usenix.org/legacy/event/atc10/tech/full_papers/Hunt.pdf)
6. [Zab: High-performance broadcast for primary-backup systems](https://ieeexplore.ieee.org/document/5958223)
