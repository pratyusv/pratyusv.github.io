---
layout: single
comments: true
title: "CAP Theorem: What a Network Partition Actually Forces You to Choose"
date: 2026-01-02 00:00:00-0000
description: "A precise, example-driven explanation of CAP: linearizability, availability, partitions, quorum behavior, operation scope, recovery, and the limits of CP/AP labels."
tags: [cap-theorem, consistency, availability, network-partitions, distributed-systems]
categories: ['Distributed Systems Components']
---

# 1. One Seat, Two Regions

Atlas Tickets sells the final seat for concert `C7`. Its inventory record is
replicated in London and Dublin:

~~~text
concert C7, seat A1, remaining = 1
~~~

Under normal conditions, the replicas communicate. Then the network link
between the regions stops delivering messages. Clients can still reach each
region, but London and Dublin cannot exchange inventory state.

At 09:00:00, buyer `B1` asks London to reserve the seat. At the same moment,
buyer `B2` asks Dublin.

![Two reachable replicas cannot communicate while selling one remaining seat](/assets/img/cap-theorem/story-overview.svg)

Each region must now choose what to do without knowing what the other region is
doing:

- refuse or delay some operations until coordination is possible;
- answer from local state and risk conflicting outcomes.

That is the situation CAP describes. It is not a shopping exercise in which an
architect casually selects two attractive letters. It is an impossibility
result about what a replicated read/write system can guarantee **while a
network partition prevents required communication**.

---

# 2. Begin With One Copy

One database copy has one obvious authority:

~~~text
client -> database -> seat A1 state
~~~

There is no replica disagreement. The database can order reservations and
return the result. But the one copy is also one availability and geographic
boundary.

Replication adds failure tolerance and local access:

~~~text
London clients -> London replica
Dublin clients -> Dublin replica
~~~

It also adds a new correctness question: when replicas cannot communicate,
may both continue answering as authorities for the same data?

CAP applies because we chose to keep multiple network-separated copies that
must present one logical read/write object. The theorem is not primarily about
databases being slow or fast. It is about indistinguishable network states and
the guarantees possible in them.

---

# 3. The Three Terms Need Precise Meanings

The letters are easy to memorize and easy to misdefine.

![Consistency, availability, and partition describe distinct parts of the contract](/assets/img/cap-theorem/definitions.svg)

## Consistency Means One-Copy Behavior

In CAP, consistency is normally **linearizability**, not merely "replicas
eventually match" and not the `C` in database ACID.

Linearizability requires each completed operation to appear as though it took
effect atomically at one instant between invocation and response. If a write of
`remaining = 0` completes before a later read begins, that later read cannot
return `remaining = 1`.

~~~text
write(remaining = 0) completes
                         read() begins -> must not return 1
~~~

Concurrent operations may be ordered either way, but the result must be
explainable by one order that respects real-time completion.

![A later read must observe a write that already completed](/assets/img/cap-theorem/linearizability.svg)

## Availability Means Every Non-Failing Recipient Responds

CAP availability is stronger than a monthly uptime percentage. Every request
received by a non-failing node must eventually receive a response. The node
cannot wait forever for a partitioned peer.

A service that returns successful responses for 99.99% of the month may still
sacrifice CAP availability for one key during a partition. Conversely, a node
that responds quickly with stale data may satisfy availability while violating
linearizability.

## A Partition Is Lost or Unbounded Communication

A network partition means some messages between nodes are lost or delayed long
enough that the algorithm cannot rely on their arrival. Both nodes may be alive
and serving clients. A timeout cannot prove whether the peer crashed, the
network failed, or the reply is merely slow.

`P` is therefore not a feature like compression that can be switched off. If
the deployment contains network-separated replicas, the algorithm must have
behavior for communication failure. The forced choice is what happens to
consistency and availability during that condition.

---

# 4. The Indistinguishable Timeline Creates the Impossibility

London receives `reserve(A1)` but cannot contact Dublin. From London's point of
view, several worlds look identical:

~~~text
world 1: Dublin is down
world 2: the link is broken
world 3: Dublin concurrently reserved A1
world 4: London's messages and replies are only very slow
~~~

If London waits for Dublin, it may preserve one-copy correctness but does not
complete the request during an unbounded partition. If London responds from
local state, Dublin can independently make the same choice, and both can sell
the seat.

![The same missing message can represent several different remote states](/assets/img/cap-theorem/partition-timeline.svg)

No timeout value removes the ambiguity. A longer timeout changes user
experience and the probability of a false suspicion; it does not produce new
information about the unreachable replica.

For an asynchronous network with a partition, the system cannot guarantee both
linearizable one-copy behavior and a response from every reachable replica for
every request.

---

# 5. Consistency During the Partition: Reject or Wait

Suppose Atlas chooses the seat invariant over regional write availability.
Only the side containing a valid quorum or leader may complete reservations.
The other side rejects, redirects, or waits.

~~~text
London: has authority -> reserve A1 -> success
Dublin: no authority  -> unavailable / timeout
~~~

![A consistency-preserving side continues while the minority refuses writes](/assets/img/cap-theorem/cp-path.svg)

This is commonly described as **CP behavior**. The important statement is not
the label but the operation-level contract:

> During loss of quorum, reservations for seat A1 do not complete on the
> minority side.

Reads also need a policy. A minority replica can refuse a linearizable read,
serve an explicitly stale read, or serve data under a weaker bounded-staleness
contract. A system is not uniformly "CP" merely because one write path uses a
quorum.

---

# 6. Availability During the Partition: Accept Divergence

Suppose Atlas instead requires both regions to accept requests. London and
Dublin each decrement their local copy and respond successfully.

~~~text
London: A1 -> B1
Dublin: A1 -> B2
~~~

![Both sides remain responsive but create outcomes that cannot both be the one-copy result](/assets/img/cap-theorem/ap-path.svg)

This is commonly described as **AP behavior**. It preserves response
availability but gives up linearizability for that operation. Recovery now
requires domain semantics:

- choose one reservation and cancel the other;
- allocate a different equivalent seat;
- represent the operation as a tentative hold until global confirmation;
- design the object so concurrent updates merge safely.

"Eventually consistent" does not explain which outcome is correct. The data
type or business process must define how divergent states merge and how users
experience compensation.

Some values, such as grow-only sets or independently accumulated counters, can
merge more naturally than exclusive seat ownership. CAP behavior should be
chosen per invariant, not by copying one label across an entire company.

---

# 7. Why “Pick Two of Three” Is Misleading

The familiar triangle suggests three symmetric products:

~~~text
CA, CP, or AP
~~~

That picture hides the conditional nature of the theorem.

![CAP is a partition-time branch, not a symmetric choose-two menu](/assets/img/cap-theorem/not-a-triangle.svg)

When communication is healthy, a replicated system can be both linearizable
and responsive. When required messages cannot cross a partition, it must either
withhold some responses or allow behavior that cannot be explained by one
linearizable copy.

The useful question is therefore:

> For this operation and invariant, what does each reachable side do when it
> cannot communicate with the authority required to preserve the guarantee?

A single-node database is not an interesting "CA system" in this framework. It
avoids replica partition behavior by having one copy, but losing access to that
copy still loses service. Likewise, declaring that a network is reliable does
not make partitions impossible; it only makes the exceptional branch rarer.

---

# 8. Normal Operation Still Has Latency Tradeoffs

CAP describes the partitioned case. Most requests occur while communication is
working, where systems still choose among latency, consistency, durability, and
cost.

![Healthy communication and partitioned communication expose different choices](/assets/img/cap-theorem/normal-vs-partition.svg)

The PACELC mnemonic extends the discussion:

~~~text
if Partition: choose Availability or Consistency
Else:          choose Latency or Consistency
~~~

PACELC is a design lens, not a replacement theorem. A cross-region quorum can
provide a strong order while healthy but adds wide-area latency. A local read
can be fast but stale. A local write acknowledged before remote replication can
be fast yet vulnerable to regional data loss.

![PACELC separates partition behavior from the normal latency tradeoff](/assets/img/cap-theorem/pacelc.svg)

Those choices must name their durability and failure assumptions rather than
being compressed into `CP` or `AP`.

---

# 9. Quorums Help Only Under Their Assumptions

For `N = 3` replicas, a majority quorum requires two responses. If a partition
separates one replica from two, the pair can continue and the singleton cannot
complete quorum operations.

~~~text
partition A: replicas L1 + L2 -> quorum available
partition B: replica D1      -> quorum unavailable
~~~

![A majority quorum preserves one side's authority by making the other unavailable](/assets/img/cap-theorem/quorum-partition.svg)

The quorum is not free availability. It deliberately removes authority from
the minority. Nor does the equation `R + W > N` alone prove linearizability:
the protocol must also handle concurrent writes, versions, membership changes,
failed coordinators, and read/write ordering correctly.

Dynamic membership is especially sensitive. Two configurations must not each
form an independent majority during a transition. Consensus protocols use
joint or otherwise overlapping configuration rules so authority does not split.

---

# 10. Scope the Guarantee to Data and Operations

Real services rarely expose one consistency mode for every operation.

Atlas might choose:

| Operation | Partition behavior |
|---|---|
| reserve a uniquely numbered seat | require quorum; reject without authority |
| read the event description | serve a cached or stale copy |
| increment page-view analytics | accept locally and merge |
| update payment state | require the payment authority |
| show a personalized recommendation | serve a degraded local result |

![One product can make different partition choices for different invariants](/assets/img/cap-theorem/operation-scope.svg)

Consistency may also be scoped by key. A quorum can serialize each seat
independently without creating one global order across every concert. Stating
"the database is strongly consistent" is incomplete unless the statement says
for which operations, keys, sessions, and failure conditions.

---

# 11. Client Timeouts Create Unknown Outcomes

Suppose London commits `A1 -> B1`, but the success reply is lost. The client
sees a timeout:

~~~text
timeout != failure
timeout = outcome unknown to caller
~~~

Retrying with a new request identity can reserve twice or trigger a conflicting
business action. CAP does not solve this problem. The API still needs stable
idempotency keys, status lookup, durable result records, and reconciliation.

Availability in the theorem is a server-side property under a model. User
experience also depends on deadlines, retries, routing, and whether the caller
can discover a previously completed outcome.

---

# 12. Recovery Is Part of the Availability Choice

When the link heals, replicas exchange state. A consistency-preserving system
brings the stale side up to date before restoring authority. An
availability-preserving system must reconcile accepted divergent operations.

Recovery questions include:

- Which versions causally or totally supersede others?
- Can concurrent values merge without losing intent?
- Which business operation compensates for a rejected winner?
- When is a recovering replica safe to serve strong reads?
- How is repair throttled so it does not overload foreground traffic?

The partition policy and the repair policy form one contract. "We will resolve
it later" is incomplete until later has an algorithm, owner, and observable
completion condition.

---

# 13. A Practical Decision Procedure

For each important operation:

1. State the invariant and consistency model precisely.
2. Identify the replicas or authorities that must communicate.
3. Partition them deliberately in a test.
4. Decide which requests may still complete on each side.
5. Bound client deadlines and retry behavior.
6. Define divergent-state merge or minority catch-up.
7. Define the user-visible meaning of rejection, staleness, and compensation.
8. Measure recovery before restoring full traffic.

![A partition decision begins with an invariant and ends with recovery](/assets/img/cap-theorem/decision-guide.svg)

This procedure is more useful than assigning one two-letter label to an entire
database product. The same database can expose linearizable conditional writes,
eventually consistent reads, and local analytics counters through different
APIs and configurations.

---

# 14. Failure Scenarios to Test

## Minority Still Accepts Traffic

Verify that operations requiring quorum fail clearly rather than queueing
without bound or silently falling back to unsafe local writes.

## Old Leader Remains Reachable to Clients

Partition the old leader from its quorum but not from clients. Ensure terms,
leases, or fencing prevent it from completing protected mutations.

## Partition Heals After Divergent Writes

Confirm that merge rules retain both intents where required and that
compensation is idempotent.

## Client Reply Is Lost After Commit

Retry with the same idempotency key and verify that the original result is
returned rather than performed twice.

## Membership Changes During the Partition

Ensure old and new configurations cannot independently authorize conflicting
writes.

## Clocks Disagree

If conflict resolution uses timestamps, move clocks backward and forward. A
last-write-wins rule can discard a causally later operation when physical time
is trusted incorrectly.

---

# 15. Final Mental Model

CAP is best remembered as a forced branch in one concrete failure state:

~~~text
replicas cannot communicate
  -> preserve one-copy order by withholding some responses
  -> or answer everywhere and permit divergence
~~~

It does not say that systems may have only two desirable properties forever.
It does not classify every database with one permanent label. It does not
replace decisions about durability, isolation, latency, stale reads,
idempotency, merge semantics, or recovery.

For Atlas Tickets, the final seat cannot be safely confirmed by two isolated
authorities. The system can make one side unavailable, or it can accept
tentative outcomes that a later business process resolves. The theorem proves
that no protocol can promise both unconditional response availability and one
linearizable seat while the required messages cannot arrive.

That is the useful lesson: **during a partition, every completed operation
reveals which guarantee the system chose.**

---

# References

1. [Brewer's Conjecture and the Feasibility of Consistent, Available, Partition-Tolerant Web Services](https://users.ece.cmu.edu/~adrian/731-sp04/readings/GL-cap.pdf)
2. [Perspectives on the CAP Theorem](https://groups.csail.mit.edu/tds/papers/Gilbert/Brewer2.pdf)
3. [CAP Twelve Years Later: How the “Rules” Have Changed](https://www.infoq.com/articles/cap-twelve-years-later-how-the-rules-have-changed/)
4. [Linearizability: A Correctness Condition for Concurrent Objects](https://cs.brown.edu/~mph/HerlihyW90/p463-herlihy.pdf)
