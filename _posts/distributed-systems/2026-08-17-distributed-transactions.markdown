---
layout: single
comments: true
title: "Inside Distributed Transactions: 2PC, Sagas, Outbox, Idempotency, and Recovery"
date: 2026-08-17 03:00:00+0100
description: "A connected checkout story explaining atomic commit, replicated 2PC coordinators, uncertain outcomes, recoverable saga orchestration, outbox, inbox deduplication, and recovery."
tags: [distributed-transactions, two-phase-commit, sagas, outbox, idempotency, databases, distributed-systems]
categories: ['Distributed Systems Components']
---

# 1. One Checkout Across Three Services

Customer `C7` submits checkout request `chk-9f2` for order `ord-742`:

~~~text
item       camera-31
quantity   1
amount     £899
~~~

The checkout service must coordinate three independently durable systems:

~~~text
OrderDB       create ord-742
InventoryDB   reserve one camera-31
PaymentDB     authorize £899
~~~

The business invariant sounds atomic:

> Either the order, reservation, and payment authorization all succeed, or the
> system should behave as though the checkout did not succeed.

But no single database transaction covers all three owners. A process can crash
after reserving stock but before calling payment. A reply can disappear after a
charge succeeds. A broker can deliver the same event twice. A refund can fail
hours after an order is cancelled.

![The complete checkout setting](/assets/img/distributed-transactions/story-overview.svg)

We will run this same checkout through two families of design:

1. **Two-phase commit (2PC)** keeps participant transactions prepared until one
   atomic commit decision is durable.
2. A **saga** commits each service locally and uses later transactions to move
   forward or compensate.

Then we will connect both to idempotency keys, transactional outbox, inbox
deduplication, uncertain client outcomes, and reconciliation.

---

# 2. A Local Transaction Has One Commit Boundary

Inside one database, `BEGIN` and `COMMIT` give the storage engine one authority
over logging, locks, visibility, and recovery:

~~~sql
BEGIN;
INSERT INTO orders(id, status) VALUES ('ord-742', 'CONFIRMED');
UPDATE inventory SET available = available - 1 WHERE sku = 'camera-31';
INSERT INTO payments(order_id, amount) VALUES ('ord-742', 89900);
COMMIT;
~~~

If all tables live in one transactional database, this is often the simplest
correct design. Splitting them into services creates independent commit logs and
failure domains.

![One database has one atomic commit point; three databases do not](/assets/img/distributed-transactions/local-vs-distributed.svg)

A distributed transaction must therefore answer:

- who decides the global outcome;
- what each participant durably records before replying;
- what happens if messages or processes disappear;
- how long locks and reservations remain;
- how a recovering component discovers the outcome;
- what a timed-out caller may safely retry.

---

# 3. The Double-Write Problem Appears Immediately

Suppose checkout first commits the order and then publishes `OrderCreated`:

~~~text
1. COMMIT order row
2. publish message
~~~

A crash between the steps leaves a real order with no event. Reversing them is
not safe either: the event can be delivered while the order transaction later
rolls back.

![Both orders of a database write and message publish have a crash gap](/assets/img/distributed-transactions/double-write.svg)

Retries do not close the gap. Retrying publish can duplicate a message; retrying
the database write can duplicate a business action. The solution must create
one durable boundary or make duplicate delivery harmless.

This is the core shape of distributed transactions: several individually valid
actions must represent one business outcome despite partial completion.

---

# 4. First State the Invariants

Before selecting a protocol, define what must never be observed:

~~~text
I1: confirmed order => inventory reservation exists
I2: confirmed order => payment authorization exists
I3: one checkout key => at most one logical order
I4: reservation quantity never drives available stock below zero
I5: every terminal failure eventually releases temporary resources
~~~

![Checkout invariants and the states that would violate them](/assets/img/distributed-transactions/invariants.svg)

Not every invariant requires instantaneous atomicity. `I4` must hold inside the
inventory write. `I1` and `I2` might be temporarily false while an order is
`PENDING`, provided clients do not treat it as confirmed. `I5` is an eventual
recovery property.

This separation tells us whether we need a synchronous atomic commit or can use
explicit intermediate states and compensation.

---

# 5. Two-Phase Commit Creates One Global Decision

2PC has one **coordinator** and several **participants**. Each participant owns
a local transaction. The coordinator uses transaction ID `tx-742`.

## Phase 1: Prepare

The coordinator sends `PREPARE(tx-742)` to OrderDB, InventoryDB, and PaymentDB.
Each participant:

1. validates the requested mutation;
2. acquires the locks or reservations needed to commit later;
3. writes its tentative changes and `PREPARED tx-742` record durably;
4. promises not to abort unilaterally;
5. replies `YES`—or records abort and replies `NO`.

## Phase 2: Decide

If every vote is `YES`, the coordinator durably records `COMMIT tx-742` before
sending `COMMIT` to participants. If any vote is `NO`, it records and sends
`ABORT`.

![Successful two-phase commit for ord-742](/assets/img/distributed-transactions/two-phase-commit.svg)

The durable coordinator decision is the global commit point. Participants may
receive it at different times, but they must converge on that one outcome.

---

# 6. Prepared Means Durable but Not Yet Visible

After voting `YES`, InventoryDB has crossed an important boundary:

~~~text
ACTIVE -> PREPARED -> COMMITTED
                  \-> ABORTED
~~~

The reservation and enough redo/undo information survive restart. Locks remain
held so another transaction cannot consume the same camera and make the promised
commit impossible.

![Participant states and durable records](/assets/img/distributed-transactions/participant-state.svg)

A prepared transaction is deliberately uncomfortable. It consumes resources
but cannot finish locally. PostgreSQL, for example, stores prepared state on
disk for later `COMMIT PREPARED` or `ROLLBACK PREPARED`, and warns that prepared
transactions continue holding locks until an external manager resolves them.

Preparation converts "I might commit" into "I can commit if instructed." That
promise is what makes the global decision safe—and what makes coordinator loss
dangerous.

---

# 7. Any `NO` Vote Aborts Everyone

Assume PaymentDB rejects the card during prepare:

~~~text
OrderDB       YES
InventoryDB   YES
PaymentDB     NO
~~~

The coordinator records `ABORT tx-742` and broadcasts it. The first two
participants roll back tentative work and release locks. PaymentDB is already
aborted.

![A single NO vote drives the global abort path](/assets/img/distributed-transactions/two-phase-abort.svg)

A timeout before every participant prepares is normally treated as failure to
prepare, so the coordinator can choose abort. After all participants have voted
yes and the commit decision is durable, timeout cannot change that decision.

---

# 8. Coordinator Failure Exposes 2PC's Blocking Point

Now all three participants vote `YES`, but the coordinator crashes before any
participant receives a decision.

Each participant sees:

~~~text
PREPARED tx-742
decision = unknown
~~~

It cannot safely commit: the coordinator might have recorded abort. It cannot
safely abort: the coordinator might have recorded commit and told another
participant. It must learn the durable decision or remain prepared.

![Prepared participants block while the coordinator decision is unavailable](/assets/img/distributed-transactions/coordinator-failure.svg)

This is why classical 2PC is called a blocking protocol. Timeouts detect lack of
progress; they do not manufacture knowledge of the decision.

Production systems reduce the risk by replicating coordinator state, recovering
it quickly, limiting transaction duration, and alerting on old prepared work.
Those measures improve availability; they do not change the logical uncertainty
of a participant that knows only its own `YES` vote.

---

# 9. Replicate the Coordinator Without Creating Two Deciders

“Replicate coordinator state” needs a concrete authority model. Running three
coordinator processes that write independent logs would be worse than running
one: two processes might drive different outcomes for the same transaction.

A production coordinator exposes a stateless API tier over a partitioned,
consensus-backed decision log. Transaction ID `tx-742` routes to one log shard.
That shard has one elected leader and replicas in different failure domains.

![A replicated coordinator preserves one durable decision history](/assets/img/distributed-transactions/coordinator-ha.svg)

The leader records state transitions such as:

~~~text
BEGIN tx-742
VOTE OrderDB YES
VOTE InventoryDB YES
VOTE PaymentDB YES
DECISION COMMIT
~~~

`COMMIT` becomes authoritative only after the configured quorum commits it.
The coordinator may then acknowledge the client and repeatedly deliver the
decision to participants. Followers preserve the history for failover; they do
not independently choose outcomes.

Coordinator terms fence stale leaders. Participants accept a decision only
when it is valid for the transaction and coordinator epoch, and the durable
transaction ID makes repeated delivery idempotent. A participant must never
infer a new outcome merely because a different coordinator replica contacted
it.

## 9.1 Coordinator Failover Is Deterministic Log Recovery

![Coordinator failover chooses recovery from the last committed record](/assets/img/distributed-transactions/coordinator-failover.svg)

After a leader crash, the new leader reads the committed prefix:

- no durable global decision: it may durably choose `ABORT` according to the
  protocol's recovery rule, then notify prepared participants;
- durable `COMMIT`: it can only resend `COMMIT`;
- durable `ABORT`: it can only resend `ABORT`;
- decision delivered to only some participants: delivery resumes until every
  participant converges.

If the coordinator log loses quorum, it cannot safely choose or reveal a new
decision. Prepared participants remain blocked and retain their locks. This is
the honest availability boundary: consensus makes the coordinator state
survive minority failure, but 2PC still cannot finish without access to the
authoritative decision.

The service can scale by partitioning transaction IDs across several decision
log groups. Each transaction still belongs to exactly one ordered group. Moving
a live transaction between groups would require an explicit epoch handoff; a
simple hash-map change must not create two coordinators for it.

This removes a physical single point of failure while retaining one logical
decider per transaction.

---

# 10. Recovery Is a Log-Interpretation Algorithm

After restart, each role consults durable state:

| Durable record | Safe recovery action |
|---|---|
| participant has no prepare | abort local work |
| participant has `PREPARED`, no decision | query coordinator; wait if unavailable |
| participant has `COMMIT` | redo/finish commit and acknowledge |
| participant has `ABORT` | undo/finish abort and acknowledge |
| coordinator has no decision | choose/record abort if commit was never durable |
| coordinator has `COMMIT` or `ABORT` | resend that decision until acknowledged |

![Coordinator and participant recovery from durable logs](/assets/img/distributed-transactions/two-phase-recovery.svg)

Messages are repeatable. Receiving `COMMIT tx-742` twice must still produce one
commit. The stable transaction ID makes decision delivery idempotent.

The coordinator cannot delete its decision record merely because it sent all
messages once. It retains enough state until every participant can recover the
outcome or a higher-level retention rule safely takes over.

---

# 11. Atomic Commit Is Not Distributed Isolation

2PC decides **commit versus abort**. It does not by itself determine how two
concurrent distributed transactions interleave.

Suppose `tx-742` and `tx-743` both read the last camera as available before
either reserves it. Atomic commit alone does not prevent both from voting yes.
InventoryDB must enforce the stock invariant using a conditional update, lock,
serializable transaction, or another concurrency-control mechanism.

![Atomicity and isolation solve different dimensions](/assets/img/distributed-transactions/atomicity-vs-isolation.svg)

End-to-end serializability requires compatible concurrency control across the
participating operations—often distributed two-phase locking, timestamp
ordering, or a database that implements the whole abstraction. Do not infer
serializable execution merely from the presence of two phases in 2PC.

---

# 12. 2PC Is Not Consensus

The protocols solve related but different problems:

| Property | Classical 2PC | Consensus |
|---|---|---|
| question | may every participant commit? | which proposed value is chosen? |
| decision condition | unanimous prepare votes | quorum under protocol rules |
| distinguished role | coordinator | leader/proposer can change |
| coordinator/participant failure | can block prepared work | progress with configured quorum |
| usual output | commit or abort | arbitrary agreed log/value |

![Two-phase commit compared with quorum consensus](/assets/img/distributed-transactions/commit-vs-consensus.svg)

Consensus can replicate the coordinator decision or implement a non-blocking
commit design such as Paxos Commit. It does not remove the business rule that
every transaction participant must be able to commit before a global commit is
valid.

Similarly, running a Raft cluster for each database protects each participant's
local state; it does not atomically join three independent Raft logs. Another
protocol still connects them.

---

# 13. The Client Timeout Has an Unknown Outcome

The coordinator records commit and every participant applies it, but the HTTP
response to `C7` is lost. The client sees a timeout.

~~~text
timeout != abort
timeout != commit
timeout = outcome unknown to caller
~~~

![A lost success response creates an uncertain client outcome](/assets/img/distributed-transactions/uncertain-outcome.svg)

Blindly issuing a new checkout can charge twice. The API needs a stable
idempotency key and an outcome lookup:

~~~http
POST /checkouts
Idempotency-Key: chk-9f2
~~~

The retry asks for the same logical operation. The service returns the stored
result for `chk-9f2` or reports that it remains in progress.

---

# 14. Idempotency Is a Durable State Machine

An idempotency table should scope the key and bind it to the request:

~~~text
(tenant, key)       atlas, chk-9f2
request_hash        sha256(camera-31|1|89900)
state               IN_PROGRESS | SUCCEEDED | FAILED_RETRYABLE | FAILED_FINAL
resource_id         ord-742
response            201 + response body
expires_at          retry-horizon boundary
~~~

![Concurrent retries coordinate through one idempotency record](/assets/img/distributed-transactions/idempotency-state.svg)

The first request atomically inserts `IN_PROGRESS`. Concurrent duplicates with
the same payload wait, poll, or receive a retryable response. Reusing the key
with a different request hash is rejected. Success stores the response before
the caller is acknowledged.

A compact C++ decision sketch is:

~~~cpp
auto claim = keys.claim(tenant, key, requestHash);
if (claim.payloadMismatch()) return conflict();
if (claim.succeeded())       return claim.savedResponse();
if (!claim.isOwner())        return stillProcessing();

auto result = runCheckout("ord-742");
keys.complete(key, result);  // durable before reply
return result;
~~~

The key's retention must exceed every possible client and proxy retry horizon.
Deleting it early turns an old retry into a new business operation.

---

# 15. When Holding Distributed Locks Is Too Expensive

2PC works well when participants support preparation, transactions are short,
the coordinator is reliable, and atomic visibility is worth reduced
availability.

It fits poorly when:

- a workflow lasts minutes or days;
- a participant is an external payment or email API;
- services use heterogeneous stores without prepare support;
- holding locks would block unrelated work;
- regional partitions must not freeze all checkouts;
- the business can represent pending and compensating states.

For those workflows, a saga makes partial progress explicit.

---

# 16. A Saga Is a Sequence of Local Transactions

Atlas redesigns checkout as:

~~~text
T1 CreateOrder(PENDING)
T2 ReserveInventory
T3 AuthorizePayment
T4 ConfirmOrder
~~~

Every step commits locally. A durable saga record tracks which step is next.
Other services can observe intermediate state, so `PENDING` must not be mistaken
for `CONFIRMED`.

![The forward path of the checkout saga](/assets/img/distributed-transactions/saga-forward.svg)

The saga trades isolation and instantaneous atomicity for availability and
long-lived workflow support. Its correctness comes from explicit states,
idempotent steps, durable messaging, retry, and compensating transactions.

---

# 17. Compensation Is a New Business Action, Not Time Travel

If payment authorization fails after inventory reservation, the saga runs:

~~~text
C2 ReleaseInventory
C1 CancelOrder
~~~

![Saga failure runs compensations in reverse dependency order](/assets/img/distributed-transactions/saga-compensation.svg)

Compensation does not erase history. Another reader may have observed the
pending order. Payment capture might require a refund with its own failure
modes and fees. An email cannot be unsent; a correction can only follow it.

Design steps by reversibility:

- **compensatable**: reserve inventory, authorize rather than capture payment;
- **pivot**: the point after which the workflow should only move forward;
- **retryable**: post-pivot actions such as fulfilment notification.

Prefer authorization before irreversible capture, temporary reservation before
shipment, and delayed external side effects until the workflow crosses its
pivot.

---

# 18. Orchestration and Choreography Move Control Differently

An **orchestrator** durably records saga state and commands each step. The path
is easy to inspect, retry, and compensate, but the orchestrator becomes an
important workflow dependency.

In **choreography**, services react to events: `OrderCreated` triggers inventory,
`InventoryReserved` triggers payment, and so on. There is no central caller,
but the workflow becomes distributed across subscriptions and can be difficult
to understand globally.

![Saga orchestration compared with event choreography](/assets/img/distributed-transactions/orchestration-vs-choreography.svg)

Choreography does not remove coordination; it encodes coordination in events,
consumer state, and contracts. For a workflow with many branches, deadlines,
and compensations, explicit orchestration is usually easier to operate.

## 18.1 The Saga Orchestrator Must Also Survive Failure

Do not keep the workflow cursor only in one orchestrator process. Run several
workers over a replicated workflow store. A worker claims `ord-742` using a
versioned compare-and-swap or lease, records every transition durably, and sends
commands with stable step IDs.

![Replicated saga workers recover one durable workflow state machine](/assets/img/distributed-transactions/saga-orchestrator-ha.svg)

If a worker crashes after sending `AuthorizePayment(pay-88)` but before
recording the response, its replacement sees an uncertain step. It retries the
same command ID or queries Payment; it does not invent a new payment attempt.
The recipient's inbox/idempotency record converts that repeated command into
one local effect.

The workflow-store version fences two workers that temporarily believe they
own the same saga. Only one may advance `PAYMENT_PENDING@v6` to a later state.
If the store loses quorum, orchestration pauses; already committed service
effects remain and recovery resumes from the last durable workflow version.

Choreography distributes this availability problem among the broker and every
consumer rather than eliminating it. Consumer offsets, inbox records, and local
outbox state become the recovery history.

---

# 19. Sagas Have Concurrency Anomalies

While `ord-742` is pending, another workflow can observe or modify the same
inventory. Compensation can overwrite a newer state if it assumes nothing
happened in between.

Mitigations include:

- semantic locks such as `reservation_status=PENDING`;
- version checks on every transition;
- commutative operations such as increment/decrement with unique reservation ID;
- rereading state before compensation;
- escrow or bounded counters for scarce resources;
- keeping tentative data invisible to ordinary reads;
- rejecting transitions not valid from the current state.

![Versioned saga transitions reject stale forward and compensation writes](/assets/img/distributed-transactions/saga-concurrency.svg)

A compensation should target the effect created by `reservation r-742`, not
blindly add one unit to a counter. Identity makes reversal precise.

---

# 20. The Transactional Outbox Closes the Database/Message Gap

When OrderDB creates the pending order, it inserts the event in the same local
transaction:

~~~sql
BEGIN;
INSERT INTO orders(id, status) VALUES ('ord-742', 'PENDING');
INSERT INTO outbox(event_id, aggregate_id, event_type, payload)
VALUES ('evt-901', 'ord-742', 'OrderCreated', '{"sku":"camera-31"}');
COMMIT;
~~~

![Business row and outbox event share one local commit](/assets/img/distributed-transactions/transactional-outbox.svg)

Now either both rows exist or neither does. A relay polls committed outbox rows
or change-data-capture tails the database log and publishes them to the broker.
The service never has to atomically commit its database and broker directly.

The outbox solves atomic **recording** of publication intent. It does not by
itself guarantee one broker delivery.

---

# 21. The Outbox Relay Can Publish Twice

The relay publishes `evt-901`, the broker accepts it, and the relay crashes
before recording progress. After restart it publishes `evt-901` again.

![Relay crash after publish produces duplicate delivery](/assets/img/distributed-transactions/outbox-relay-race.svg)

Changing the order recreates the opposite loss: marking sent before publish can
lose the event. Therefore the normal outbox contract is **at-least-once
delivery**, with stable event IDs and idempotent consumers.

Ordering also needs a defined scope. Using `aggregate_id=ord-742` as the broker
partition key preserves order for one order when the broker supports it. It
does not create a total order across every order.

---

# 22. An Inbox Makes Consumer Effects Idempotent

Inventory consumes `evt-901` in one local transaction:

~~~sql
BEGIN;
INSERT INTO inbox(consumer, event_id)
VALUES ('inventory', 'evt-901') ON CONFLICT DO NOTHING;
-- Continue only if the INSERT created a row.
INSERT INTO reservations(id, order_id, sku)
VALUES ('r-742', 'ord-742', 'camera-31');
COMMIT;
~~~

![Inbox deduplication and business mutation share one transaction](/assets/img/distributed-transactions/inbox-deduplication.svg)

If the broker redelivers, the unique inbox key proves that this consumer already
applied the event. The broker acknowledgement occurs after the local commit; a
crash before acknowledgement causes a harmless redelivery.

Deduplication retention must cover the broker's redelivery and replay horizon.
Deleting inbox history while old messages can return re-enables duplicates.

---

# 23. Exactly Once Always Has a Boundary

A broker may provide exactly-once processing for records inside its own
transactional domain. That does not automatically include:

- a payment provider;
- an email service;
- a database outside the broker transaction;
- a client that timed out after success;
- a human-visible physical shipment.

![Exactly-once boundaries across database, broker, and external effects](/assets/img/distributed-transactions/exactly-once-boundaries.svg)

End-to-end systems usually combine:

~~~text
at-least-once transport
+ stable operation/event identity
+ atomic local deduplication and effect
+ reconciliation for external ambiguity
= effectively-once business outcome within a stated horizon
~~~

The claim must name its scope, retention, and failure assumptions.

---

# 24. Reconciliation Repairs What Protocols Cannot Prove

Some outcomes remain ambiguous. Payment may accept authorization `pay-88` while
its response is lost. The saga must not immediately issue a second authorization.
It queries by idempotency key, waits for webhook evidence, or moves to
`PAYMENT_UNKNOWN` for reconciliation.

![Reconciliation compares intended and observed external state](/assets/img/distributed-transactions/reconciliation.svg)

A reconciler repeatedly compares:

~~~text
orders expected CONFIRMED
reservations actually ACTIVE
payment authorizations actually SUCCEEDED
outbox events actually published/consumed
~~~

It repairs missing work, triggers compensation, or raises an operator case.
Reconciliation is not an admission of protocol failure; it is the recovery loop
for dependencies that cannot join the same atomic boundary.

---

# 25. Choosing the Transaction Model

| Requirement | Prefer |
|---|---|
| short operations across prepare-capable databases | 2PC |
| no partial visibility is acceptable | 2PC plus suitable isolation |
| long workflow or human step | saga |
| heterogeneous services/external APIs | saga + idempotency + reconciliation |
| database update must publish an event | transactional outbox |
| broker may redeliver | inbox/idempotent consumer |
| client may retry after timeout | API idempotency record |

These mechanisms compose. A saga step can use a local database transaction and
outbox. Its consumer can use an inbox. A 2PC coordinator API still needs an
idempotency key for client uncertainty. Consensus can replicate the coordinator.

---

# 26. Capacity and Operational Signals

For 2PC, monitor:

- active, prepared, committed, and aborted transaction counts;
- prepare and decision latency distributions;
- age of oldest prepared transaction;
- locks, rows, and connections held by prepared work;
- coordinator term, quorum health, commit index, and replica lag;
- coordinator election duration and transactions blocked by lost quorum;
- coordinator log durability and recovery time;
- participant vote timeouts and decision-redelivery backlog.

For sagas and messaging, monitor:

- workflows by state and age;
- workflow-store quorum health, claim conflicts, and worker failovers;
- step attempts, timeouts, and retry delay;
- compensation rate, age, and terminal failures;
- outbox oldest-unpublished age and row count;
- broker consumer lag and redelivery rate;
- inbox duplicate rate and retention;
- idempotency keys stuck `IN_PROGRESS`;
- reconciliation mismatch count and repair latency.

Little's Law makes lock pressure concrete. If checkout rate is `λ` and the
average prepared interval is `W`, the average prepared population is:

~~~text
prepared_transactions ~= λ * W
~~~

At 2,000 checkouts/s, increasing prepared time from 50 ms to 5 s grows average
prepared work from 100 to 10,000 transactions—along with locks and log state.

---

# 27. Failure Scenarios

## Participant Crashes Before `YES`

It has made no durable promise. The coordinator can abort after timeout.

## Participant Crashes After `YES`

It recovers `PREPARED`, asks for the decision, and blocks if no decision source
is available. It must not guess.

## Coordinator Crashes After Durable Commit

Recovery reads `COMMIT` and resends it. A participant that did not receive the
first message eventually commits.

## Coordinator Loses Quorum

No replica may choose a new decision. Prepared participants remain blocked
until the committed decision history is available again.

## Old Coordinator Leader Returns

Its term is stale, so the decision log and participants reject new commands
from it. It cannot create a second outcome for `tx-742`.

## Client Retries After a Lost Reply

`chk-9f2` returns the stored result for `ord-742`. A different payload with that
key is rejected.

## Saga Command Is Delivered Twice

The participant deduplicates command/event ID or applies a transition that is
idempotent from current state.

## Saga Worker Fails After Sending a Command

A replacement claims the durable workflow version and retries the same step ID
or queries its outcome. The participant deduplicates the repeated command.

## Compensation Fails

The saga remains in `COMPENSATING`, retries with backoff, and escalates after a
bounded policy. It must not report fully cancelled while stock remains reserved.

## Relay Publishes and Crashes

The message is published again; the consumer's inbox prevents a second local
effect.

## Payment Outcome Is Unknown

Query by provider idempotency key and reconcile. Do not turn timeout into a
second charge or assume rollback.

---

# 28. The Whole Checkout, End to End

Atlas chooses a saga because payment cannot join database 2PC:

1. `C7` sends checkout with idempotency key `chk-9f2`.
2. checkout atomically claims the key and creates `ord-742` as `PENDING`.
3. the same transaction writes outbox event `evt-901`.
4. the relay publishes it; a crash may cause duplicate publication.
5. Inventory's inbox admits `evt-901` once and creates reservation `r-742`.
6. Inventory writes `InventoryReserved` to its own outbox.
7. the orchestrator asks Payment to authorize with stable key `pay-88`.
8. Payment succeeds, but the response is lost; reconciliation discovers success.
9. checkout transitions `ord-742` to `CONFIRMED` and saves the API response.
10. a client retry of `chk-9f2` returns that saved response.
11. duplicate events are ignored by consumer inboxes.
12. if payment had failed, the saga would release `r-742` and cancel the order.

![The complete distributed checkout lifecycle](/assets/img/distributed-transactions/end-to-end.svg)

| Risk | Containment mechanism |
|---|---|
| independent commit points | 2PC or explicit saga states |
| coordinator process fails | quorum-replicated decision log and leader recovery |
| coordinator loses quorum | block rather than create an uncommitted outcome |
| prepared participant loses coordinator | durable decision recovery; possible blocking |
| timeout hides success | stable transaction/idempotency key |
| database commit then publish crash | transactional outbox |
| relay publishes twice | stable event ID + inbox |
| saga step partially completes | idempotent retry and compensation |
| saga worker fails | replicated workflow state plus versioned worker claim |
| compensation races newer work | version/identity-checked transition |
| external API outcome unknown | provider key + reconciliation |

---

# 29. What These Mechanisms Guarantee

Classical 2PC can provide atomic commit when participants obey durable prepare
and the decision is eventually recoverable. It does not automatically provide
non-blocking progress, serializable isolation, client outcome knowledge, or
atomicity with systems that cannot prepare.

A saga can provide durable eventual completion or compensation when every step
and compensation is retryable, observable, and recoverable. It does not erase
intermediate visibility, make compensation equivalent to rollback, or remove
concurrency anomalies.

Outbox and inbox can provide atomic local recording plus idempotent local
application under their retention assumptions. They do not prevent duplicate
transport or make an external side effect transactional.

The most reliable design states each boundary instead of saying "exactly once"
without qualification.

---

# 30. Conclusion

Distributed transactions are not one protocol. They are a set of answers to
where atomicity must end and how uncertainty crosses that boundary.

2PC keeps tentative local transactions locked behind one durable global
decision. It gives strong atomicity but can block. Sagas expose intermediate
states and replace rollback with forward recovery or compensation. They improve
workflow availability but move correctness into business state machines.
Idempotency keys turn retries into lookup of one logical operation. Outbox and
inbox turn an unsafe database/broker double write into atomic local writes plus
duplicate-tolerant delivery. Reconciliation handles external systems whose
outcomes cannot be proven synchronously.

The checkout can be compressed to:

~~~text
business invariant
  -> choose atomic commit or explicit saga states
  -> one quorum-backed decision or workflow history
  -> durable operation identity
  -> local transaction + outbox
  -> at-least-once delivery + inbox
  -> idempotent forward/compensating step
  -> terminal state or reconciliation
~~~

The goal is not to eliminate every partial failure. It is to ensure every
partial failure leaves durable evidence and a safe next action.

---

# References

1. [Gray and Lamport — Consensus on Transaction Commit](https://www.microsoft.com/en-us/research/publication/consensus-on-transaction-commit/)
2. [Garcia-Molina and Salem — Sagas](https://dl.acm.org/doi/10.1145/38713.38742)
3. [PostgreSQL — PREPARE TRANSACTION](https://www.postgresql.org/docs/current/sql-prepare-transaction.html)
4. [Debezium — Outbox Event Router](https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html)
5. [RFC 9110 — Idempotent Methods](https://www.rfc-editor.org/rfc/rfc9110.html#name-idempotent-methods)
