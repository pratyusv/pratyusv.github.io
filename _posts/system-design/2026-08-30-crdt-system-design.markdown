---
layout: single
comments: true
title: "Designing CRDT Systems: Replicas, Merges, Offline Edits, and Convergence"
date: 2026-08-30 00:00:00-0000
description: "A system-design guide to CRDTs: replicas, logical clocks, operation-based and state-based synchronization, sequence CRDTs, offline edits, metadata, compaction, authorization, and production tradeoffs."
categories: ['System Design']
tags: [system-design, crdt, distributed-systems, collaborative-editing, eventual-consistency]
---

## 1. Why CRDTs Exist

A distributed system becomes difficult when more than one place can accept a
write. If every write must go through one leader, ordering is easier. The
system can say: this operation happened first, this operation happened second,
and every replica should apply the same ordered log.

That leader-based model is often the right design. It is the model behind many
databases, queues, and online collaborative editors. It also creates a hard
limit: if a client is offline, far from the leader, or connected to a different
region, it may not be able to write locally.

CRDTs solve a different problem. A Conflict-Free Replicated Data Type lets
multiple replicas accept updates independently and later merge those updates
without manual conflict resolution. The replicas may receive updates in
different orders. Some replicas may be offline for minutes, hours, or days. If
the CRDT is designed correctly, replicas that eventually receive the same
updates converge to the same value.

That property is powerful, but it is not free. A CRDT moves complexity into
data modeling, metadata, merge rules, deletion semantics, compaction, and
product behavior. The important system-design question is not "Are CRDTs
better than OT?" The better question is: "Does this product need independent
writes, and can we afford the metadata and semantic complexity required to
merge them?"

![CRDT replicas accept local writes and converge after synchronization](/assets/img/crdt/crdt-replica-convergence.svg)

> **What to remember:** A CRDT is a data type whose replicas can be updated
> independently and merged deterministically. It is useful when availability,
> offline work, local latency, or multi-region writes matter more than strict
> single-leader ordering.

---

## 2. The Problem CRDTs Are Solving

Consider a shared note that currently says:

```text
Meet at 10
```

Alice goes offline and changes it to:

```text
Meet at 10 in room A
```

Bob is online in another region and changes it to:

```text
Meet at 11
```

When Alice reconnects, the system has to combine two histories that were
created without talking to each other. A last-write-wins design might discard
one user's work. A lock-based design might have prevented Alice from editing
offline. A central sequencer could have ordered the writes if both users were
online, but that assumption has already failed.

A CRDT design asks the application to represent edits so they can be merged.
For a counter, merging can be simple. For a set, merging is harder because
deletes need meaning. For a rich-text document, merging is much harder because
the system must preserve order, formatting, comments, undo behavior, and user
intent.

CRDTs are not magic conflict erasers. They remove one class of conflict: the
system does not need a human or a single leader to decide how replicas converge
at the data-structure level. The product can still have semantic conflicts. If
two users edit the same paragraph in opposite ways, the CRDT can preserve both
operations, but the result may still need product design around suggestions,
comments, history, or review.

---

## 3. Strong Eventual Consistency

The core promise behind CRDTs is often called strong eventual consistency.

| Property | Meaning |
|---|---|
| Local update | A replica can accept a write without synchronously coordinating with every other replica. |
| Eventual delivery | If replicas keep syncing, updates eventually reach the other replicas. |
| Deterministic merge | Receiving the same updates leads to the same state, even if delivery order differs. |
| Convergence | Replicas that have seen the same updates end with equivalent values. |

This is different from strong consistency. Strong consistency usually means a
read observes a single, current value according to a global order or a leader's
order. CRDTs normally accept that replicas can temporarily disagree. The design
goal is that disagreement is temporary and mechanically repairable.

A useful mental model is:

```text
strong consistency:        coordinate before accepting the write
CRDT convergence:          accept locally, exchange information, merge later
```

That tradeoff explains where CRDTs fit. They are attractive for collaborative
editors, offline-first apps, distributed databases with multi-master writes,
local-first productivity tools, edge systems, shopping carts, counters, sets,
presence-like structures, and replicated configuration where temporary
divergence is acceptable.

They are a poor fit when every write must be globally validated before it is
visible. Bank transfers, inventory with strict scarcity, uniqueness constraints,
and access-control changes often need coordination somewhere in the design.

---

## 4. The Smallest CRDT Example: A Counter

A counter sounds trivial until two replicas increment it independently.

```text
initial value = 0

replica A increments once     local value = 1
replica B increments twice    local value = 2
```

If the system stores only the final value, merging `1` and `2` is ambiguous.
The correct merged value is `3`, not `2`. The CRDT counter fixes this by storing
more structure than the user-visible value.

A grow-only counter keeps one count per replica:

| Replica | A component | B component | Visible value |
|---|---:|---:|---:|
| A after local increment | 1 | 0 | 1 |
| B after two local increments | 0 | 2 | 2 |
| Both after merge | 1 | 2 | 3 |

The merge function takes the maximum value for each replica component:

```text
merge({A: 1, B: 0}, {A: 0, B: 2}) = {A: 1, B: 2}
value = 1 + 2 = 3
```

This works because the per-replica component only grows. Replaying the same
message does not double count it. Receiving messages in a different order does
not change the result. Merging again does not change the result.

![A grow-only counter stores one monotonic component per replica](/assets/img/crdt/g-counter-merge.svg)

This example reveals a general CRDT pattern: the internal representation is not
always the same as the value shown to the user. The implementation stores enough
metadata to make merging deterministic.

---

## 5. CRDTs Need Merge-Friendly Semantics

For a data type to behave like a CRDT, its updates and merges need properties
that make ordering less important.

| Property | Why it matters |
|---|---|
| Commutative | Applying independent updates in different orders should not change the final result. |
| Associative | Merging grouped updates should not depend on grouping shape. |
| Idempotent | Receiving the same update more than once should not duplicate its effect. |
| Monotonic metadata | Replicas need a way to know that information has advanced. |

These properties do not mean the application has no conflicts. They mean the
data structure has a deterministic answer for combining concurrent information.

A shopping cart is a good example. Adding item X on one device and item Y on
another device can merge naturally. Removing an item is harder. If one device
adds an item while another removes it, the product must define the intended
behavior. Should add win? Should remove win? Should the cart track each
individual add event and remove only observed adds? Different CRDTs encode
different answers.

That is why CRDT design starts with product semantics, not syntax. Before
choosing a CRDT, decide what concurrent add, update, delete, move, and rename
mean to users.

---

## 6. State-Based and Operation-Based CRDTs

CRDTs are commonly described in two families: state-based and operation-based.
Both aim for convergence, but they synchronize different things.

| Model | What replicas exchange | Typical merge rule | Operational requirement |
|---|---|---|---|
| State-based CRDT | Current state or a delta of state | Merge states using a deterministic join | Messages may be duplicated or reordered. |
| Operation-based CRDT | Update operations | Apply operations once in a causally valid way | Delivery usually needs duplicate suppression and causal assumptions. |

In a state-based CRDT, a replica can send its current CRDT state to another
replica. The receiver merges that state into its own state. This is robust but
can be expensive if the state is large.

In an operation-based CRDT, a replica sends operations such as "add element with
ID x" or "insert character with ID c after ID p." This can be bandwidth
efficient, but the transport and storage layer must preserve enough information
to avoid missing or duplicating important operations.

Many production systems mix the ideas. They exchange compact operation streams
for normal sync, keep durable logs for recovery, send snapshots for fast load,
and use state or delta-state transfer when a replica is far behind.

![State-based CRDTs merge state while operation-based CRDTs exchange updates](/assets/img/crdt/state-vs-operation-crdt.svg)

---

## 7. Causality, Logical Time, and Version Vectors

CRDT systems need to reason about what a replica has already seen. Wall-clock
time is not enough. Clocks can drift, messages can be delayed, and offline
devices can create work long after the server's current time has advanced.

Instead, CRDT systems commonly use logical metadata.

| Metadata | Purpose |
|---|---|
| Replica ID | Identifies the device, browser tab, region, or server that created an update. |
| Operation ID | Makes each update uniquely identifiable and deduplicatable. |
| Lamport timestamp | Gives a logical ordering signal without relying on wall-clock time. |
| Version vector | Tracks the latest known counter per replica. |
| Causal context | Describes which prior updates were visible when this update was created. |

A version vector can answer questions like:

```text
replica A has seen: A:7, B:3, C:0
replica B has seen: A:5, B:4, C:2
```

From this, the sync layer can infer that A needs B's operations after B:3 and
C's operations after C:0. B needs A's operations after A:5. Neither replica has
a complete global picture.

Version vectors are not free. If there are millions of clients, a naive vector
with one entry per client becomes too large. Production designs often scope
replica IDs to active devices, compact old metadata, shard documents, garbage
collect inactive actors, or use server-assigned epochs to bound the size of
causal metadata.

![Version vectors describe which operations each replica has observed](/assets/img/crdt/version-vector-sync.svg)

---

## 8. Sets: Why Deletes Are Hard

Adding to a replicated set is easy. If Alice adds `A` and Bob adds `B`, the
merged set contains both values.

Deleting from a replicated set is harder because a delete needs to say what it
is deleting. Suppose one replica removes `X` while another replica concurrently
adds `X`. The system needs deterministic semantics.

| Set design | Concurrent add/remove behavior | Cost |
|---|---|---|
| Grow-only set | No removes | Simple but limited. |
| Two-phase set | Once removed, an item cannot be re-added | Easy to merge, awkward for users. |
| Last-write-wins set | Timestamp decides winner | Simple, but clock behavior and lost intent are risks. |
| Observed-remove set | Remove deletes the add events the remover has observed | More metadata, better semantics. |

An observed-remove set stores unique tags for adds. Removing an item removes
the tags the replica has seen. If another replica concurrently creates a new
add tag, that add was not observed by the remove, so it can survive the merge.

This is a recurring CRDT idea: a delete is not just "remove value." It often
needs to reference the specific creation events or identifiers it has observed.
That extra metadata is what makes concurrent behavior deterministic.

---

## 9. Sequence CRDTs for Text

Text collaboration is harder than counters and sets because order matters.
Users do not only add values; they insert characters, delete ranges, move
blocks, apply formatting, undo changes, and expect cursors to remain sensible.

Position-based operations are fragile in a multi-writer system:

```text
insert "X" at index 5
```

Index 5 can mean different things on different replicas if those replicas have
not seen the same prior edits. A sequence CRDT avoids relying only on current
array indexes. It gives inserted items stable identifiers and orders those
identifiers deterministically.

![A sequence CRDT inserts text between stable identifiers instead of relying only on array indexes](/assets/img/crdt/sequence-crdt-text.svg)

At a simplified level, a text CRDT stores records like this:

| Field | Meaning |
|---|---|
| Element ID | Globally unique ID for a character, token, or text chunk. |
| Parent or position ID | Logical location where the element was inserted. |
| Value | Character, token, or formatting marker. |
| Visibility | Whether the element is visible or deleted. |
| Causal metadata | What the actor had observed when creating the element. |

If Alice and Bob both insert text at the same logical location, the CRDT uses a
deterministic ordering rule over identifiers. Every replica that receives both
inserts will render them in the same order.

This solves convergence. It does not automatically solve user intent. If two
people insert words at the same sentence boundary, the merged sentence may be
mechanically valid but awkward. Product design still matters. Systems often add
comments, suggestions, conflict markers, history views, or paragraph-level
semantics to help users understand concurrent edits.

---

## 10. A CRDT-Based Collaborative Document Architecture

A CRDT collaborative editor can accept local edits immediately, persist them
locally, and synchronize them later. The server may still be important, but it
does not need to be the only place where an edit can be created.

```text
local edit first, sync second
```

The architecture usually separates local editing, sync transport, durable
storage, merge/compaction work, and authorization.

| Component | Responsibility |
|---|---|
| Editor client | Applies local edits immediately and stores pending operations. |
| Local CRDT store | Keeps the document CRDT state, operation IDs, and version vector. |
| Sync client | Exchanges missing updates with the server or peers. |
| Sync service | Receives operations or deltas, deduplicates them, stores them, and forwards them. |
| Durable operation store | Keeps updates for replay, audit, and recovery. |
| Snapshot service | Creates compact document snapshots to avoid replaying unbounded history. |
| Awareness service | Handles ephemeral cursors, selections, and online status. |
| Authorization service | Decides who can read, write, sync, share, or administer a document. |

![A CRDT document system separates local editing from synchronization and compaction](/assets/img/crdt/crdt-document-architecture.svg)

The server can still reject unauthorized writes, limit document size, enforce
quotas, compact metadata, store snapshots, and provide a rendezvous point for
devices. CRDT does not mean "no server." It means the data model allows
independent replicas to create mergeable updates.

---

## 11. The Write Path

When a user types in a CRDT editor, the experience is local first. The client
does not wait for a server round trip before showing the edit.

1. User types into the editor.
2. Client converts the input into one or more CRDT operations.
3. Client assigns stable operation IDs using its replica ID and local counter.
4. Client applies the operations to its local CRDT state.
5. Client stores the operations in a local pending log.
6. Sync client sends operations to the server when connectivity is available.
7. Server authenticates the session and validates write permission.
8. Server deduplicates operations it has already seen.
9. Server persists accepted operations.
10. Server forwards missing operations to other replicas.
11. Other replicas merge the operations into their local CRDT state.

The local-first path explains why CRDTs feel good in unreliable networks. The
author sees their own edit immediately. The system can sync later.

The same path also explains the engineering cost. The client is no longer a
thin view over server state. It owns local persistence, operation generation,
deduplication, retry behavior, and recovery from partially synced work.

---

## 12. Reconnect and Sync

Reconnect in a CRDT system is not just "open a new WebSocket." The transport is
only the pipe. The important question is which operations each side is missing.

A reconnecting client usually sends:

| Field | Purpose |
|---|---|
| Document ID | Identifies the replicated object. |
| User/session identity | Lets the server authenticate and authorize sync. |
| Replica ID | Identifies the local actor/device. |
| Version vector or sync token | Describes what the client has already seen. |
| Pending operations | Sends local work that has not been acknowledged. |

The server compares the client's known version with its own durable history.
Then it accepts missing client operations, sends missing server-side operations,
and returns an updated sync token.

```text
client: I have A:10, B:2 and pending A:11..A:14
server: I have A:10, B:7, C:3

server accepts A:11..A:14
server sends B:3..B:7 and C:1..C:3
both sides advance after merge
```

This is different from an OT design. In a central OT system, reconnect often
means resuming from the last acknowledged document revision. In a CRDT system,
there may be no single central revision that explains all local work. The sync
protocol cares about missing operations and causal knowledge.

---

## 13. Authentication and Authorization

CRDT systems still need normal security controls. A common mistake is to assume
that because data can merge, any replica can write anything. That is not a
valid production design.

| Moment | Required check |
|---|---|
| Document open | User can read the document. |
| Sync handshake | User can participate in this document's sync session. |
| Operation upload | User can write the affected object or range. |
| Share change | User can administer document permissions. |
| Snapshot read | User can read the snapshot and replay log. |
| Device revocation | Future sync from that device should be rejected or limited. |

Long-lived local replicas create an important security problem. A user may
create operations while they appear authorized, then upload them after their
permission has changed. The product must define the rule.

| Policy | Behavior |
|---|---|
| Check at upload time | Reject operations uploaded after permission was removed. |
| Check at creation time with signed capability | Accept operations created while a bounded write ticket was valid. |
| Hybrid | Allow short offline windows, then require reauthorization. |

The right answer depends on the product. A private note app may tolerate longer
offline windows. A shared enterprise document may require tighter enforcement.
The design should make this explicit instead of hiding it inside the sync
implementation.

---

## 14. Metadata, Tombstones, and Compaction

CRDTs often store more metadata than centralized designs. That metadata is what
allows deterministic merge, but it can grow over time.

| Metadata | Why it exists | Production concern |
|---|---|---|
| Operation IDs | Deduplication and replay safety | IDs must be unique and compact enough to store. |
| Replica IDs | Attribute updates to actors/devices | Inactive replicas need lifecycle management. |
| Version vectors | Track what each replica has seen | Large actor sets can bloat sync metadata. |
| Tombstones | Preserve delete knowledge | Deleted text can accumulate. |
| Causal context | Interpret concurrent operations | Expensive if not summarized. |
| Snapshots | Speed up loading | Need correct compaction boundaries and compatibility. |

Text CRDTs are especially sensitive to tombstones. If every deleted character
stays forever as hidden metadata, a heavily edited document can become much
larger than the visible text.

Compaction is the process of removing or summarizing metadata without breaking
future merges. It is difficult because a disconnected replica may later return
with old operations. If the system has compacted away the context needed to
interpret those operations, it needs a recovery path.

Practical systems usually combine several techniques:

| Technique | Purpose |
|---|---|
| Snapshot at known sync frontier | Create a compact base once enough replicas have observed prior operations. |
| Actor retirement | Remove or summarize metadata for inactive devices. |
| Server epochs | Require very old clients to resync from a newer snapshot. |
| Tombstone compaction | Remove deleted elements only when no valid replica can reference them. |
| Chunked text representation | Store runs or blocks instead of one record per character where possible. |

Compaction is a principal-engineer-level design issue because it affects
correctness, storage cost, migration, backwards compatibility, and disaster
recovery. A CRDT prototype can ignore it. A production editor cannot.

---

## 15. Server-Side Management

Even in a CRDT system, servers do significant work.

| Server responsibility | Why it matters |
|---|---|
| Authentication | Identifies users, sessions, and devices. |
| Authorization | Prevents unauthorized reads and writes. |
| Operation durability | Makes synced edits recoverable. |
| Deduplication | Makes retries and reconnects safe. |
| Fanout | Delivers new operations to connected replicas. |
| Snapshotting | Keeps open and replay latency bounded. |
| Compaction | Controls metadata growth. |
| Abuse control | Limits operation size, rate, and document growth. |
| Migration | Handles CRDT format evolution. |

The sync service should be idempotent. If a client retries the same operation
after a timeout, the server should recognize the operation ID and avoid storing
or broadcasting it twice.

The server should also separate durable document updates from ephemeral
awareness. Cursor movement, typing indicators, and live selections can use
best-effort sync with throttling. Text operations and comments need durable
storage, replay, and authorization checks.

---

## 16. Multi-Region Design

CRDTs are attractive in multi-region systems because independent regions can
accept writes and merge later. That does not mean every multi-region CRDT
system is simple.

| Design | Behavior | Tradeoff |
|---|---|---|
| Single home region | CRDT logic exists mostly for offline clients; server writes still concentrate in one region. | Simpler operations, higher latency for distant users. |
| Active-active regions | Multiple regions accept writes and replicate operations. | Lower write latency, harder auth, quotas, ordering of side effects, and compaction. |
| Edge-assisted sync | Edge nodes accept or buffer operations near users. | Needs careful durability and replay guarantees. |
| Peer-to-peer sync | Clients exchange operations directly. | Harder identity, security, moderation, and availability. |

CRDTs handle the mergeable document state. They do not automatically solve
global side effects. Notifications, billing events, audit logs, permission
changes, search indexing, and export pipelines may still need ordered workflows
or idempotent processing.

For many products, a good intermediate design is to keep CRDT document edits
mergeable while using ordinary coordinated systems for non-mergeable side
effects.

---

## 17. Failure Handling

A CRDT design should be explicit about what happens when parts of the system
fail.

| Failure | Expected behavior |
|---|---|
| Client goes offline | Local edits continue if policy allows; operations stay in local pending log. |
| Client reconnects | Sync compares version vectors and exchanges missing operations. |
| Same operation is retried | Operation ID deduplication prevents duplicate effects. |
| Server crashes before persisting | Client retries pending operation later. |
| Server persists but acknowledgement is lost | Client retries; server deduplicates. |
| Replica returns after long absence | Server either syncs missing history or requires resync from a compacted snapshot. |
| Unauthorized client uploads operations | Server rejects operations according to authorization policy. |
| Compaction removed required context | Client must reload from a newer snapshot or run a migration path. |

The failure model should separate user-visible guarantees from internal
mechanics. "Your local edits are saved on this device" is not the same as "your
edits have synced to other devices" or "your edits are durably stored on the
server." A good product makes those states visible enough that users understand
risk during long offline periods.

---

## 18. Capacity Planning

CRDT capacity planning is not only request rate. The expensive parts are often
metadata, replay, fanout, local storage, and compaction.

| Dimension | Why it matters |
|---|---|
| Active documents | Determines sync-service working set. |
| Replicas per document | Drives version-vector size and sync complexity. |
| Operations per second | Drives ingestion, deduplication, persistence, and fanout. |
| Operation size | Large inserts, formatting changes, and embedded object metadata can dominate bandwidth. |
| Replay length | Affects document open, reconnect, and replica catch-up latency. |
| Tombstone volume | Affects storage, memory, and rendering performance. |
| Snapshot frequency | Trades write amplification for faster load and recovery. |
| Offline duration | Determines how much history must be retained for returning replicas. |
| Hot-document fanout | One edit may need delivery to many connected clients. |
| Compaction throughput | Determines whether metadata growth remains bounded. |

A system-design interview answer should estimate at least the active replica
count, edit rate, operation size, retention window, snapshot size, and fanout
cost. A production design should also track worst-case documents, not only
averages. One heavily edited document with thousands of collaborators can be
more dangerous than millions of quiet documents.

---

## 19. Observability

CRDT correctness issues can look like normal latency problems until users
report missing edits or strange document order. The system needs metrics that
distinguish sync delay, merge cost, replay pressure, and convergence failures.

| Area | Useful signals |
|---|---|
| Sync | Sync handshake latency, pending operation count, missing operation count, resume success rate. |
| Merge | Merge latency, operations applied per merge, duplicate operation rate, causal-gap count. |
| Storage | Operation-log append latency, snapshot age, replay length, compaction backlog. |
| Client | Local pending log size, local storage failures, render latency, offline duration. |
| Fanout | Connected replicas per document, publish latency, dropped awareness messages. |
| Security | Authorization rejection rate, expired capability usage, revoked device upload attempts. |
| Correctness | Divergence detection, checksum mismatch, invalid operation rejection, migration failure. |

Some systems periodically compare document checksums between replicas that
claim to have the same version frontier. A mismatch does not repair the
document by itself, but it is a strong signal that the implementation has a
merge, migration, or compaction bug.

---

## 20. CRDTs Versus OT

CRDTs and OT both appear in collaborative editing, but they optimize for
different system shapes.

| Question | OT answer | CRDT answer |
|---|---|---|
| Where are writes ordered? | Usually by a central document sequencer. | At replicas, then merged by deterministic rules. |
| Does offline editing fit naturally? | Possible, but harder for long offline windows. | Usually a core strength. |
| What does the server do? | Orders and transforms operations. | Stores, relays, authorizes, deduplicates, snapshots, and compacts. |
| What is the main correctness burden? | Transformation rules and server ordering. | Identifier design, causality, tombstones, merge semantics, and compaction. |
| What is easier to explain? | Linear revision history. | Independent replicas and eventual convergence. |
| What is harder operationally? | Sequencer scaling and failover for hot documents. | Metadata growth, old replicas, and semantic merges. |

For a Google Docs-like always-online editor, OT with a central sequencer can be
a practical and understandable design. For local-first editors, offline-first
apps, peer collaboration, or active-active regions, CRDTs are often the better
conceptual fit.

The design choice should come from requirements. Do not choose CRDTs because
they sound more distributed. Choose them when independent writes and later
merge are product requirements.

---

## 21. When Not to Use CRDTs

CRDTs are not the right abstraction for every distributed write problem.

| Need | Better fit |
|---|---|
| Strict uniqueness | Coordinator, transaction, lease, or consensus-backed allocator. |
| Scarce inventory | Reservation system with strong coordination. |
| Money movement | Ledger with transactional guarantees and audit controls. |
| Simple online collaboration | Central sequencer with OT may be easier. |
| Rare conflicts | Version checks and explicit conflict UI may be enough. |
| Small single-region product | Ordinary database transactions may be simpler and safer. |

The practical rule is simple: use CRDTs when the cost of coordination is higher
than the cost of mergeable metadata and eventual convergence. If coordination
is cheap and product semantics require one current answer, a coordinated design
is usually better.

---

## 22. A Practical CRDT Design Checklist

By the end of a CRDT system design, the following boundaries should be clear.

| Boundary | Design decision |
|---|---|
| Replication scope | Is the CRDT per document, per paragraph, per object, or per field? |
| Replica identity | What creates replica IDs, and how are old replicas retired? |
| Operation identity | How are operations deduplicated across retries and reconnects? |
| Merge semantics | What do concurrent add, delete, update, move, and format operations mean? |
| Sync protocol | Does sync exchange operations, state, deltas, snapshots, or a mix? |
| Offline policy | How long can a client write offline before reauthorization or resync? |
| Authorization | Are permissions checked at operation creation, upload, or both? |
| Compaction | When can tombstones and causal metadata be safely removed? |
| Recovery | What happens when a replica returns after compaction? |
| Observability | How does the system detect divergence, causal gaps, and replay pressure? |

If these questions are unanswered, the design is not finished. The CRDT data
structure may converge in a demo, but the production system may still fail
under old clients, revoked users, hot documents, large deletes, or long offline
periods.

---

## 23. The Mental Model

A CRDT system is built around one idea: replicas do not need to ask permission
from a single ordering server before every local update, but they must create
updates that can be merged later.

That changes the shape of the system.

| Centralized editing mindset | CRDT mindset |
|---|---|
| The server decides the next document revision. | Replicas create operations with stable identities. |
| Clients wait for authoritative ordering. | Clients apply local operations immediately. |
| Reconnect resumes from one revision number. | Reconnect exchanges missing operations using causal metadata. |
| Deletes remove data from the current state. | Deletes often create metadata that must be retained until safe. |
| Scaling focuses on sequencer ownership and fanout. | Scaling also includes metadata, compaction, and old-replica recovery. |

The beginner-level lesson is that CRDTs let independently edited replicas
converge. The senior-level lesson is that convergence is a data-model property,
not a complete product architecture. The principal-level lesson is that the
hardest parts are often lifecycle problems: authorization over time, compaction
without breaking old replicas, format migration, cost control, and explaining
merged results to users.

---

## 24. References

1. M. Shapiro et al., "Conflict-Free Replicated Data Types"
2. M. Shapiro et al., "A Comprehensive Study of Convergent and Commutative Replicated Data Types"
3. N. Preguica, J. M. Marques, M. Shapiro, and M. Letia, "A Commutative Replicated Data Type for Cooperative Editing"
4. M. Kleppmann and A. R. Beresford, "A Conflict-Free Replicated JSON Datatype"
