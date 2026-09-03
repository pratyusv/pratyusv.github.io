---
layout: single
comments: true
title: "Design Collaborative Editing: Real-Time Documents, Conflict Resolution, and Recovery"
date: 2022-01-18 00:00:00-0000
description: "A system-design guide to collaborative editing: document sessions, WebSocket gateways, operation ordering, operational transformation, presence, persistence, reconnection, and failure handling."
categories: ['System Design']
tags: [system-design, collaborative-editing, websockets, operational-transformation, crdt, distributed-systems]
---

## 1. What Collaborative Editing Must Provide

Collaborative editing lets several people modify the same document at the same
time and see each other's changes quickly. A single-user editor has one local
truth. A collaborative editor has many clients, many network paths, and one
shared document that must remain understandable after concurrent edits.

Consider a document that currently contains:

```text
The cat sat.
```

Two users edit at nearly the same time:

- user A inserts `black ` before `cat`;
- user B deletes `sat`;
- both users expect their cursor, undo history, and document view to remain
  sensible.

The core problem is not only low latency. The system must preserve a coherent
document while clients optimistically edit local copies, messages arrive in
different orders, users disconnect, and servers are replaced.

A collaborative editor usually separates several kinds of state:

| State | Examples | Durability requirement |
|---|---|---|
| Document state | Text, paragraphs, formatting, comments, title | Durable |
| Edit history | Ordered operations, revisions, snapshots | Durable enough for recovery and audit needs |
| Session state | Active connection, last acknowledged revision, selected ranges | Recoverable |
| Presence state | Cursor position, user color, typing indicator | Ephemeral |
| Authorization state | Who can read, comment, suggest, or edit | Durable policy with current enforcement |

The design goal is to let users edit locally for responsiveness while the
service establishes one authoritative order for durable changes.

> **What to remember:** Collaborative editing is not just WebSocket broadcast.
> It is optimistic local editing plus authoritative ordering, conflict handling,
> persistence, presence, reconnection, and permission enforcement.

---

## 2. Requirements

Assume we are designing an online document editor similar to a simple Google
Docs-style product.

### Functional Requirements

The functional surface has three layers: document operations, live
collaboration, and access control.

| Area | Requirement |
|---|---|
| Document lifecycle | Users can create, open, edit, and save documents. |
| Editing model | Users can change text and basic rich-text attributes. |
| Concurrent editing | Multiple users can edit the same document at the same time. |
| Live propagation | Accepted edits appear on other clients with low delay. |
| Presence | Users can see collaborators' cursors and selections. |
| Annotations | Comments or suggestions are stored as durable document annotations. |
| Recovery | A reconnecting client can reload history and resume from persisted state. |
| Permissions | Viewers, commenters, and editors have different capabilities. |

### Non-Functional Requirements

The non-functional requirements define the quality of the editing experience
and the failure behavior.

| Property | Requirement |
|---|---|
| Local responsiveness | The author sees their own edit immediately, before the server round trip completes. |
| Convergence | Clients that receive the same accepted operations eventually render the same document. |
| Bounded resources | Document sessions, WebSocket connections, queues, and presence state have explicit limits. |
| Durable recovery | Acknowledged edits survive client, gateway, and collaboration-service failure. |
| Horizontal scale | The system can spread documents, connections, and fanout across many servers. |
| Operability | Metrics explain edit latency, conflicts, reconnects, fanout pressure, and storage lag. |

### Useful Simplifications

This article focuses on collaborative document editing, not a complete office
suite. It does not design image embedding, spreadsheet formulas, offline mobile
editing for weeks, legal hold, malware scanning, or full version-history UI.
Those can be added later, but they should not obscure the central editing path.

---

## 3. The Document Model

A collaborative editor needs a representation that can be changed by small
operations. The simplest model is a string:

```text
document = "hello world"
```

Production editors often use a tree or sequence of blocks:

```text
document
    block p1: paragraph
        text run: "hello"
        text run: " world" {bold: true}
    block p2: paragraph
        text run: "..."
```

The exact structure depends on product requirements, but the same ownership
model appears in most designs:

| Owner | Responsibility |
|---|---|
| Client | Render a local copy and create operations against the revision it has seen. |
| Collaboration service | Validate, authorize, order, transform or merge, persist, and publish accepted operations. |
| Other clients | Apply accepted operations in revision order until their local copies converge. |

The service gives each durable document version a monotonically increasing
revision:

```text
document d1
revision 100: "hello world"
revision 101: insert "!" at position 11
revision 102: delete 1 character at position 5
```

A client usually tracks:

```text
documentId
sessionId
userId
local document copy
server revision last applied
pending local operations not yet acknowledged
cursor and selection
```

Revision numbers give the system a shared language for causality. Operation
`op-17` based on revision `100` means the client created the operation while
its known server state ended at revision `100`.

---

## 4. High-Level Architecture

A practical collaborative editor has a durable document path and a live session
path.

![Collaborative editing architecture](/assets/img/collabdoc/collab-edit-architecture.svg)

One possible architecture is:

```text
browser editor
    -> load balancer
    -> WebSocket gateway
    -> collaboration/session service
    -> operation log
    -> snapshot/document store
    -> pub/sub or stream
    -> other gateways with active users
```

The components have separate responsibilities:

| Component | Responsibility |
|---|---|
| Browser editor | Maintains a local document copy, captures user edits, renders remote edits, and preserves cursor state. |
| WebSocket gateway | Owns live connections, heartbeats, backpressure, authentication context, and message framing. |
| Collaboration service | Validates edit permissions, orders operations for a document, transforms or merges concurrent operations, and emits accepted revisions. |
| Operation log | Stores accepted operations in revision order for replay, audit, and recovery. |
| Snapshot store | Stores compact document snapshots so opening a document does not replay infinite history. |
| Pub/sub stream | Carries accepted operations from the document owner to gateways that have subscribed clients. |
| Presence service | Tracks ephemeral cursors, selections, and active users. |
| Auth service | Answers who can read, comment, suggest, or edit the document. |

The WebSocket gateway should not be the only durable source of document state.
It owns live sockets. The collaboration service and storage own the document's
accepted history.

---

## 5. Opening a Document Session

When a user opens a document, the system needs both durable state and live
routing state.

A typical open flow is:

```text
1. Browser requests document d1.
2. Service authenticates the user and checks read permission.
3. Browser loads a snapshot at revision R.
4. Browser opens a WebSocket session for document d1.
5. Gateway authenticates the socket and subscribes to document d1 updates.
6. Client asks for operations after revision R.
7. Client applies missed operations and becomes current.
8. Presence service announces the user's cursor and selection.
```

The snapshot and operation replay step matters. A document may have changed
between the HTTP snapshot load and WebSocket subscription. The client therefore
needs a catch-up boundary:

```text
loaded snapshot revision: 250
latest accepted revision: 256
client applies:          251..256
```

After catch-up, the client can apply new accepted operations as they arrive.
This avoids a race where the page renders revision `250`, subscribes too late,
and silently misses revision `251`.

---

## 6. The Editing Path

For low perceived latency, the client normally applies the user's edit
optimistically before the server round trip completes.

Suppose the current document at revision `100` is:

```text
hello world
```

User A types `!` at the end. The browser immediately renders:

```text
hello world!
```

and sends an operation:

```json
{
  "type": "insert",
  "documentId": "d1",
  "clientId": "cA",
  "operationId": "a17",
  "baseRevision": 100,
  "position": 11,
  "text": "!"
}
```

The server path is:

```text
receive operation
    -> authenticate session
    -> authorize edit
    -> validate operation shape and size
    -> transform or merge against operations after baseRevision
    -> assign next document revision
    -> persist operation
    -> acknowledge author
    -> publish to other subscribed sessions
```

![One collaborative edit from local operation to accepted revision](/assets/img/collabdoc/edit-operation-flow.svg)

The acknowledgement tells the author that its local speculative operation is
now part of the authoritative history:

```json
{
  "type": "ack",
  "operationId": "a17",
  "assignedRevision": 101
}
```

Other clients receive the accepted operation:

```json
{
  "type": "remote_operation",
  "documentId": "d1",
  "revision": 101,
  "operation": {
    "type": "insert",
    "position": 11,
    "text": "!"
  }
}
```

This is not ordinary message broadcast. The operation must be accepted in a
single document order. If every gateway independently broadcasts edits, clients
can apply operations in different orders and diverge.

> **What to remember:** The client can be optimistic, but the document needs an
> authoritative operation order.

---

## 7. Concurrent Edits and Operational Transformation

Concurrent edits are edits created by clients that have not yet seen each
other's operations. The system must preserve each user's intention as much as
the data type allows.

Operational transformation, or **OT**, is one common technique. In an OT system,
operations are transformed against concurrent operations so they can be applied
to a newer document state.

Given a text document with string `abc`, two users create concurrent operations:

```text
O1 = Insert(position=0, text="x")
O2 = Delete(position=2, text="c")
```

If `O1` is applied first, the document becomes:

```text
xabc
```

The original `O2` says "delete at position 2." On the new document, position 2
contains `b`, not `c`. To preserve the intent of deleting `c`, the server
transforms `O2` against `O1`:

```text
O2' = Delete(position=3, text="c")
```

Applying `O2'` to `xabc` produces:

```text
xab
```

<br/>
<div>
    <center>{% include figure.html path="assets/img/collabdoc/OT.png" %}</center>
</div>
<br/>

The simplified server rule is:

```text
incoming operation is based on revision B
current document revision is R
if B < R:
    transform incoming operation against accepted operations B+1..R
assign revision R+1
persist and publish
```

For simple text:

| Concurrent pair | Typical transformation intuition |
|---|---|
| Insert before insert | Later operation's position may shift right. |
| Insert before delete | Delete position may shift right. |
| Delete before insert | Insert position may shift left. |
| Delete before delete | Deleting the same range may collapse or become a no-op. |

Real editors handle more than characters: paragraphs, attributes, comments,
tables, embeds, undo, redo, and selections. Each operation type needs precise
transformation rules and tests for convergence.

### Server-Side Ordering

Many production OT designs use a central sequencer per document or shard. The
sequencer decides the next revision for a document:

```text
op from client A at base 100
op from client B at base 100

sequencer chooses:
revision 101 = transformed A
revision 102 = B transformed against revision 101
```

![A document sequencer orders concurrent operations and transforms the later one](/assets/img/collabdoc/ot-sequencer.svg)

This makes reasoning easier because every accepted operation has one document
revision. The trade-off is that all edits for one hot document pass through the
same ordering point. That is usually acceptable for documents because one
document's human edit rate is modest compared with system-wide traffic.

### Client-Side Pending Operations

While waiting for acknowledgement, the client may have local pending operations.
When a remote operation arrives, the client applies it carefully:

```text
server document at revision 100
client applies local pending op A
remote accepted op B arrives as revision 101
client transforms B against pending A for local display
client also transforms pending A against B for future acknowledgement
```

This keeps the screen responsive while preserving convergence with the server's
accepted order.

---

## 8. OT Versus CRDTs

Operational transformation is not the only approach. Conflict-free replicated
data types, or **CRDTs**, represent document state so concurrent updates can be
merged without a single central transformation point.

The high-level trade-off is:

| Approach | Good fit | Main cost |
|---|---|---|
| OT with server ordering | Online editors with a central service, revision history, and relatively short offline windows. | Transformation rules are subtle and every operation type must be correct. |
| Sequence CRDT | Local-first or offline-heavy editors where replicas may accept edits independently and merge later. | Metadata, tombstones, ordering identifiers, and compaction can become complex. |

In a system-design interview or architecture document, it is usually enough to
choose one model and explain the consequences. This post uses OT with a
server-assigned document order because it matches many browser-based
collaborative editors and keeps the durable history easy to explain.

The same surrounding architecture still matters with CRDTs: authentication,
presence, WebSocket gateways, storage, replay, snapshots, backpressure, and
observability do not disappear.

---

## 9. Persistence: Operation Log and Snapshots

The operation log is the durable source of accepted changes:

```text
documentId=d1
revision=101
operation=insert("!", 11)
author=userA
timestamp=...
```

The document can be reconstructed by applying operations in order to a previous
snapshot:

```text
snapshot at revision 100
    + operations 101..250
    -> document at revision 250
```

![Snapshots accelerate load while the operation log remains authoritative](/assets/img/collabdoc/persistence-snapshots.svg)

Replaying from revision zero forever is expensive, so the system periodically
stores snapshots:

```text
snapshot revision 1000
snapshot revision 2000
snapshot revision 3000
```

Opening a document then becomes:

```text
load latest snapshot <= requested revision
load operation suffix after that snapshot
apply suffix
return document and current revision
```

Snapshot creation must be tied to the operation log position. A snapshot that
claims revision `3000` must include every accepted operation through `3000` and
none after it. Otherwise clients can miss or duplicate changes during load and
replay.

### Storage Choices

Common storage layout:

| Data | Storage pattern |
|---|---|
| Document metadata | Relational database or strongly consistent key-value store. |
| Operation log | Append-friendly store partitioned by document ID. |
| Snapshots | Object storage or document database keyed by document ID and revision. |
| Presence | In-memory store with TTLs or gateway-local state. |
| Connection directory | Redis-like store, service registry, or partitioned in-memory service with leases. |

The operation log should support ordered reads by document and idempotent
writes. If the client retries operation `a17`, the service should return the
existing result rather than applying the same edit twice:

```text
(documentId, clientId, operationId) -> assigned revision
```

---

## 10. Presence, Cursors, and Selections

Presence is usually not part of the durable document history. A cursor update
is useful for collaboration, but it does not need to survive a server crash in
the same way as inserted text.

A presence message might look like:

```json
{
  "type": "presence",
  "documentId": "d1",
  "sessionId": "tab-8",
  "cursor": 42,
  "selection": [42, 51],
  "lastSeenRevision": 256
}
```

Presence has different rules from durable edits:

| Durable edit | Presence update |
|---|---|
| Persisted before acknowledgement. | Usually best-effort. |
| Ordered by document revision. | May be throttled, sampled, or overwritten. |
| Replayed after reconnect. | Recreated by active clients. |
| Requires edit permission. | Requires at least document visibility. |

Cursor positions also need transformation. If user B's cursor is after a range
where user A inserts text, B's displayed cursor should move. Many editors
transform selections using the same operation stream used for document edits.

---

## 11. WebSocket Connections and Reconnection

WebSockets are a good fit for collaborative editing because the server often
needs to push accepted operations and presence updates without waiting for the
browser to poll.

The WebSocket connection is still a disposable transport path. It can last for
hours, but it can also disappear because a laptop sleeps, a phone changes from
Wi-Fi to mobile data, a NAT mapping expires, a load balancer closes an idle
connection, or a gateway is deployed.

The server maintains liveness with heartbeats:

```text
gateway sends ping or application heartbeat
client responds with pong or heartbeat_ack
gateway closes after missed deadlines
```

When the client reconnects, it should not ask for "whatever is current" without
context. It should resume from the last durable document revision it processed:

```json
{
  "type": "resume",
  "documentId": "d1",
  "sessionId": "tab-8",
  "lastAppliedRevision": 256,
  "pendingClientOperations": ["a19", "a20"]
}
```

The server responds with:

```text
acknowledged pending operations
rejected or unknown pending operations
accepted operations after revision 256
current presence snapshot
```

![Reconnect creates a new WebSocket and resumes from the last applied revision](/assets/img/collabdoc/reconnect-replay.svg)

If the client changed IP address, the old TCP connection cannot be moved to the
new network path. The client creates a new connection, authenticates again, and
resumes application state. From the document's point of view, continuity comes
from `sessionId`, operation IDs, and revision replay, not from the old socket.

---

## 12. Authentication and Authorization

Authentication identifies the user. Authorization decides what that user may do
to a specific document.

The system checks permissions at several points:

| Moment | Required check |
|---|---|
| Load snapshot | User can read the document. |
| Open WebSocket session | User can join this document session. |
| Submit edit | User can edit or suggest at this revision. |
| Add comment | User can comment. |
| Subscribe to presence | User can see collaborators. |
| Permission changes | Existing sessions may need to be downgraded or closed. |

Long-lived connections need current authorization. If user A loses edit access
while their WebSocket remains open, the next edit must be rejected even though
the connection authenticated successfully an hour ago.

Common patterns:

| Pattern | Why it matters |
|---|---|
| Authenticate the WebSocket handshake with a secure cookie or short-lived connection ticket. | The gateway can reject unauthorized sessions before accepting long-lived work. |
| Attach a server-side session identity to the connection object. | Later operations can be checked without trusting client-supplied user IDs. |
| Authorize every document operation against current policy or a bounded policy snapshot. | Long-lived sockets do not become permanent permission grants. |
| Notify, downgrade, or disconnect sessions when document permissions change. | Existing editors stop acting on stale privileges. |
| Make operation submission idempotent. | Retries with the same operation ID do not duplicate edits. |

Authentication belongs to the session. Authorization belongs to each protected
action.

---

## 13. Scaling the System

The system scales along several dimensions.

### Many Connections

WebSocket gateways hold many mostly idle connections. A gateway usually uses
non-blocking I/O and event loops rather than one thread per connection. Each
connection has bounded input buffers, output queues, heartbeat timers, and
subscription state.

Backpressure matters. If one client cannot receive updates quickly, the gateway
must not keep unlimited messages in memory. It can disconnect the client,
coalesce presence updates, or rely on durable replay for document edits.

### Many Documents

Documents can be partitioned by `documentId`:

```text
hash(documentId) -> collaboration shard
```

One active document should normally have one authoritative operation sequencer
at a time. That sequencer can be a collaboration service instance, an actor, a
partition in a stream, or a lease holder. The important property is that
accepted revisions for one document are assigned in one order.

Popular documents create fanout pressure. If 20,000 viewers watch one document,
the service should publish one accepted operation to each gateway with local
subscribers, then let those gateways fan out locally.

```text
collaboration service
    -> gateway A has 800 viewers
    -> gateway B has 1200 viewers
    -> gateway C has 500 viewers
```

![A hot document is ordered once and then fanned out by gateways](/assets/img/collabdoc/scaling-fanout.svg)

Sending one message per viewer through the central service wastes work and can
make one popular document affect unrelated documents.

### Many Regions

Multi-region collaboration is harder than read-only document serving. The
system must choose where a document is actively sequenced.

Common choices:

| Model | Behavior |
|---|---|
| Single active region per document | Easier ordering and conflict handling; remote users pay extra latency. |
| Region near document owner | Good for teams clustered around one geography. |
| Dynamic document leader | Can move active sequencing, but migration needs careful fencing. |
| Multi-leader CRDT | Better offline and regional autonomy; more metadata and merge complexity. |

For an OT design, a single active sequencer per document is the simpler default.
The global system can still route WebSockets to nearby gateways; those gateways
forward edits to the document's current sequencer.

---

## 14. Failure Handling

Failures are normal. The design should state what survives, what is retried,
and what is reconstructed.

| Failure | Expected behavior |
|---|---|
| Browser tab closes | Gateway removes session and presence; durable edits already acknowledged remain in the log. |
| Client network changes | Old socket fails; client reconnects, authenticates, and resumes from last applied revision. |
| WebSocket gateway crashes | Live connections disappear; clients reconnect to other gateways; presence is recreated; durable operations come from log. |
| Collaboration sequencer crashes | A new owner recovers from operation log and latest committed revision before accepting edits. |
| Pub/sub delay | Gateways may lag; clients catch up using revision replay. |
| Snapshot writer fails | Operation log remains authoritative; future snapshot can retry. |
| Storage write fails | Edit is not acknowledged as durable; client retries with same operation ID. |
| Permission changes | Existing sessions receive updated capability, rejected commands, or forced disconnect. |

### Fencing the Document Owner

If one document has a current sequencer, failover must prevent two sequencers
from accepting competing revisions. A lease or term can fence old owners:

```text
owner term 41 accepts revisions 900..940
owner term 42 takes over after failure
late write from term 41 is rejected
```

Every accepted operation is written with the owner term and next revision. The
storage layer rejects stale terms or duplicate revisions.

### Reconnect Storms

A gateway restart can cause thousands of clients to reconnect at once. Clients
use exponential backoff with jitter. Gateways bound concurrent handshakes,
authentication calls, document catch-up reads, and subscription registration.

The system should be tested by killing a gateway, restarting a collaboration
shard, delaying the pub/sub stream, and forcing clients to change networks.

---

## 15. Capacity and Backpressure

Capacity is more than the number of open documents. The workload has live
connection cost, document-ordering cost, storage cost, and recovery cost.

| Dimension | Why it matters |
|---|---|
| Concurrent WebSocket connections | Drives gateway memory, file descriptors, heartbeats, and load-balancer state. |
| Active editing sessions per document | Drives conflict rate, transform work, and per-document coordination. |
| Operation rate and size | Drives validation, transformation, log writes, publish volume, and bandwidth. |
| Fanout recipients per operation | Turns one accepted edit into many gateway deliveries. |
| Queued output per connection and gateway | Slow clients can convert fanout into memory pressure. |
| Operation-log write throughput | Accepted edits cannot be acknowledged durably without this path. |
| Catch-up reads after reconnect | Gateway restarts and network changes can create replay bursts. |
| Snapshot size and frequency | Controls open latency and replay length. |
| Presence update rate | Cursor movement can dominate message volume if not throttled. |
| Auth and authorization lookup rate | Reconnect storms and permission checks can overload dependencies. |

For a rough fanout estimate:

```text
outbound messages per second ~= accepted operations per second * subscribed sessions
```

If one document receives `20` accepted operations per second and has `5,000`
subscribed sessions:

```text
20 * 5,000 = 100,000 outbound operation deliveries per second
```

Presence can be even noisier because cursor movement produces frequent updates.
Presence should be throttled, coalesced, and treated as best-effort. Durable
document edits should be persisted and replayable.

Backpressure policy should be data-specific:

| Data type | Backpressure policy |
|---|---|
| Text operation | Persist and replay; disconnect slow clients if queues exceed limits. |
| Cursor update | Drop old values and send the latest. |
| Typing indicator | Drop freely; it expires. |
| Comment creation | Persist and acknowledge only after durable write. |
| Large paste | Bound size, chunk if needed, or reject with clear error. |

---

## 16. Observability

Useful metrics should preserve the stages of the system:

| Stage | Useful signals |
|---|---|
| Open and connect | Open document latency, WebSocket connection count by gateway, reconnect rate, resume success rate. |
| Editing | Active editors and viewers by document, operation submit latency, transform latency, conflict rate. |
| Persistence | Operation-log append latency, snapshot age, replay length, duplicate operation rate. |
| Delivery | Publish-to-gateway latency, gateway output queue bytes, client acknowledgement lag. |
| Presence | Presence update rate, coalescing rate, drop rate, stale presence count. |
| Authorization | Permission rejection rate, permission-change propagation latency. |
| Recovery | Operation replay count after reconnect, sequencer failover count and duration. |

Useful logs include:

```text
documentId
userId or anonymized principal
sessionId
operationId
baseRevision
assignedRevision
sequencer term
gatewayId
latency by stage
result: accepted | transformed | rejected | duplicate | stale_base
```

The most useful incident view follows one operation end to end:

```text
client creates op
    -> gateway receives it
    -> collaboration service orders it
    -> operation log persists it
    -> pub/sub delivers it
    -> gateways fan out
    -> clients acknowledge applied revision
```

A fleet-wide average can hide one hot document, one slow gateway, or one
partition with high transform latency.

---

## 17. When Collaborative Editing Is the Wrong Tool

Collaborative editing infrastructure is expensive compared with ordinary save
and reload flows. Use it when concurrent work is central to the product.

| Need | Simpler approach |
|---|---|
| Rare edits by one user at a time | Lock document while editing or use last-write-wins with version checks. |
| Forms with independent fields | Field-level optimistic concurrency may be enough. |
| Read-mostly document with occasional comments | Ordinary HTTP plus comment refresh or notifications. |
| Presence-only experience | WebSocket presence without real-time document mutation. |
| Offline-first peer collaboration | Consider CRDTs and local-first storage rather than central OT. |

The design should match the collaboration semantics the product actually needs.

---

## 18. Complete Mental Model

A collaborative editor combines several separate mechanisms:

```text
local editor
    -> optimistic operation
    -> WebSocket gateway
    -> document sequencer
    -> transform or merge
    -> durable operation log
    -> publish accepted revision
    -> gateway fanout
    -> client apply and acknowledge
```

The durable path decides what the document is. The live path decides who sees
changes quickly. The recovery path reconstructs state after disconnection.

The most important design boundaries are:

| Boundary | Practical meaning |
|---|---|
| WebSocket connection versus session | The socket is transport; durable session state must survive reconnect. |
| Client arrival order versus document revision | Arrival order is incidental; accepted revision order defines the document. |
| Presence versus document edits | Presence is ephemeral; edits are durable and replayable. |
| Authentication versus authorization | Authentication identifies the session; authorization protects each action. |
| Retry versus duplicate edit | Operation IDs make repeated submissions idempotent. |
| Snapshot versus history | Snapshots accelerate load; the operation log defines accepted history. |
| Document owner versus global fleet | One active sequencer per document simplifies OT while the fleet scales across documents. |
| Reconnect versus recovery | Reconnect creates transport; revision replay reconstructs document continuity. |
| Slow client versus system health | Bounded queues and data-specific backpressure prevent one client from harming the session. |

After reading the post, the practical design question should be clear:

```text
For each document, who orders edits?
For each operation, how is intent preserved?
For each client, how is local optimism reconciled with server authority?
For each disconnect, what state lets the session resume?
For each failure, which component owns recovery?
```

---

## References

1. C. A. Ellis and S. J. Gibbs, "Concurrency Control in Groupware Systems"
2. C. Sun and C. Ellis, "Operational Transformation in Real-Time Group Editors"
3. M. Shapiro et al., "Conflict-Free Replicated Data Types"
