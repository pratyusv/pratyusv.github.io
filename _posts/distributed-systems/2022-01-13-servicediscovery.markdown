---
layout: single
comments: true
title: "Inside Service Discovery: Registries, Health, Routing, and Stale Endpoints"
date: 2022-01-13 00:00:00-0000
description: "A connected request journey from static addresses to DNS, quorum-replicated registries, client-side and server-side discovery, leases, readiness, locality, failover, and recovery."
tags: [service-discovery, dns, registries, load-balancing, health-checks, distributed-systems]
categories: ['Distributed Systems Components']
---

# 1. The Address That Stopped Being True

Orchard's checkout service calls the payment service at a configured address:

~~~text
PAYMENT_URL=http://10.4.7.21:8443
~~~

This is simple because one name means one process. Then `payments-1` is
restarted on another machine and receives `10.4.9.83`. Checkout continues
calling the old address. During the next deployment, three payment instances
exist briefly; afterward only two should receive traffic. A static address
cannot describe that changing set.

![A deployment makes a previously correct service address stale](/assets/img/service-discovery/story-overview.svg)

Hard-coding all three addresses does not solve the problem. Instances scale,
move, become ready at different times, drain before shutdown, and fail without
updating every caller simultaneously.

The application wants to call a stable logical name:

~~~text
payments.production
~~~

Service discovery maintains the changing answer:

~~~text
payments.production
  -> 10.4.9.83:8443
  -> 10.4.9.91:8443
~~~

---

# 2. What Service Discovery Is

**Service discovery** maps a logical service identity to a current set of
network endpoints and enough metadata to choose among them.

~~~text
resolve(service, caller_context) -> endpoint set + version + policy metadata
~~~

An endpoint is normally an IP address and port, but the useful record may also
contain zone, protocol, weight, deployment revision, health state, and
capabilities.

![A logical service name resolves to versioned endpoint records](/assets/img/service-discovery/discovery-contract.svg)

Discovery is not the same as load balancing or health checking:

| Mechanism | Question |
|---|---|
| Discovery | Which endpoints may serve this logical service? |
| Health checking | Is this endpoint currently usable under a defined test? |
| Load balancing | Which eligible endpoint should receive this request? |
| Routing | How do packets or requests reach the selected endpoint? |

The mechanisms compose. Discovery supplies candidates; health and policy make
them eligible; a balancer selects one; the network carries the call.

---

# 3. One Server Does Not Need a Registry

If payment truly runs on one stable machine, configuration or ordinary DNS may
be enough:

~~~text
checkout -> payments.example.internal -> one server
~~~

A registry becomes useful when endpoint membership changes faster than callers
can be redeployed, when several instances share one service identity, or when
routing must consider locality and health.

This is a recurring distributed-systems boundary:

> Discovery does not make one server more reliable. It makes a changing fleet
> addressable as one logical service.

The cost is another control plane whose stale or incorrect output can misroute
every caller. Use the simplest mechanism whose update speed and failure model
match the deployment.

---

# 4. The Registry Stores Desired Membership

A service registry maintains records such as:

~~~json
{
  "service": "payments.production",
  "instance": "payments-7f9c",
  "address": "10.4.9.83",
  "port": 8443,
  "zone": "eu-west-1a",
  "revision": "payments-v42",
  "weight": 100,
  "state": "READY"
}
~~~

![Instances publish records while callers consume a service view](/assets/img/service-discovery/registry-model.svg)

The registry is not usually on the application data path. It carries small,
strongly controlled membership metadata. Callers, proxies, or load balancers
cache that metadata and send requests directly to service instances.

This control-plane/data-plane split is essential. A registry lookup on every
business request would make registry latency and availability part of every
call. Cached endpoint snapshots keep ordinary traffic flowing during short
registry outages.

---

# 5. The Registry Is Itself a Replicated System

One registry process would turn every deployment and membership change into a
single point of failure. Production registries therefore expose one logical
namespace through several replicas placed in different failure domains.

![A registry cluster replicates one ordered membership history](/assets/img/service-discovery/registry-ha.svg)

A common design has one elected leader for writes and a replicated log:

1. a controller sends `REGISTER payments-7f9c, generation 42` to any registry
   endpoint;
2. a follower forwards the command to the current leader;
3. the leader appends it at log index 18;
4. a quorum persists the entry;
5. the leader commits version 18 and acknowledges the registration;
6. replicas apply the same version and watchers eventually receive it.

The acknowledgement boundary matters. If the registry replies before the
record is quorum committed, a leader crash can erase membership that the
controller believes exists. Quorum commit preserves acknowledged state through
a minority failure, but the registry must stop accepting changes when it cannot
reach a quorum.

Reads have an explicit freshness choice. A leader or quorum-confirmed read can
provide current committed membership at higher latency. A local follower read
is faster and more available but may return version 17 while version 18 is
already committed. Discovery clients already tolerate bounded staleness, so
many systems combine cached reads with version checks rather than requiring a
linearizable read for every lookup.

## 5.1 Registry Leader Failure

Suppose the leader commits version 18 and crashes before delivering every
watch notification.

![Registry failover preserves committed state but clients still resynchronize](/assets/img/service-discovery/registry-failover.svg)

Safe recovery is:

1. a quorum elects a leader whose log contains every committed version;
2. the new leader publishes a greater term;
3. the old leader is fenced if it later reconnects;
4. writers retry idempotently with the same instance generation;
5. watchers reconnect with their last observed version;
6. a watcher that cannot resume from that version fetches a complete snapshot.

Consensus protects registry state; it does not guarantee delivery of every
notification. The versioned snapshot and watch-resynchronization protocol close
that second gap.

## 5.2 A Partition Separates Authority from Cached Availability

During a registry partition, only the quorum side may commit registrations,
renew leases, or change policy. The minority side must not create a competing
membership history. Callers on either side may continue using a bounded
last-known-good snapshot, but that is data-plane continuity—not evidence that
the registry is writable.

Lease behavior must use the committed registry timeline. A minority replica
must not independently extend leases and later merge them, because both sides
could claim different live generations for the same instance. When quorum is
lost globally:

- existing callers may temporarily use cached endpoints;
- instances may keep serving requests if application policy permits;
- no membership mutation can be acknowledged safely;
- cached views expire according to their stated maximum age;
- recovery first establishes one authoritative log, then rebuilds watches.

The registry is therefore a replicated control plane with quorum availability;
the service fleet can have a different, usually longer, cached-data-plane
availability window.

---

# 6. Registration Can Be Direct or Delegated

In **self-registration**, the service process creates and renews its own record:

~~~text
payments process -> register -> registry
payments process -> heartbeat/renew -> registry
~~~

The process knows when its application is ready, but it now contains registry
credentials and lifecycle code.

In **third-party registration**, an orchestrator, node agent, or controller
observes the workload and writes registry state:

~~~text
orchestrator -> observes instance -> registry
~~~

![Self-registration and controller registration place responsibility differently](/assets/img/service-discovery/registration-models.svg)

Neither model is inherently correct. The important invariant is that published
membership corresponds to endpoints authorized and ready to serve—not merely
processes that once existed.

Registration should be idempotent and generation-aware. A restarted process
must not accidentally renew an older incarnation's record with the same
instance name.

---

# 7. Leases Remove Dead Registrations Eventually

If an instance crashes, it cannot send an explicit deregistration. A registry
can bind membership to a lease:

~~~text
register payments-7f9c with lease L42
renew L42 every 10 seconds
expire after 30 seconds without renewal
~~~

![A lease converts silent failure into eventual membership removal](/assets/img/service-discovery/lease-lifecycle.svg)

Lease expiry is a failure-detector decision, not proof that the process is
dead. A paused or partitioned process may still accept traffic from clients
that can reach it. Conversely, an overloaded registry path can expire many
healthy instances at once.

The timeout balances:

- shorter stale-endpoint windows;
- tolerance for pauses, packet loss, and registry latency;
- heartbeat traffic and write load;
- risk of synchronized mass expiry.

Jitter renewals and preserve instance generations so an old delayed renewal
cannot resurrect removed membership.

---

# 8. Liveness, Readiness, and Eligibility Are Different

A running process is not automatically safe for new traffic.

| State | Meaning |
|---|---|
| Live | Process should not yet be restarted |
| Ready | Process can accept new requests |
| Healthy | A particular check currently succeeds |
| Eligible | Policy permits this caller to select the endpoint |
| Draining | Existing work may finish; new work should stop |

![Process liveness and request eligibility change through deployment](/assets/img/service-discovery/readiness-lifecycle.svg)

`payments-v42` may be live while loading keys, unready until dependencies are
usable, ready during normal serving, and draining before shutdown. Publishing
it too early creates deployment failures; removing it too late creates calls to
a process that can no longer finish them.

A shallow TCP check proves only that something accepted a connection. A deep
check can detect dependency failure but may eject every service instance when
one shared dependency fails, making an outage worse. Health policy must state
which failures should remove an endpoint and which should trigger degraded
behavior elsewhere.

---

# 9. DNS Is the Simplest Distributed Registry Interface

DNS can map a service name to several addresses:

~~~text
payments.production. 30 IN A 10.4.9.83
payments.production. 30 IN A 10.4.9.91
~~~

SRV records can additionally carry ports, priorities, and weights. DNS is
widely supported, highly cacheable, and operationally familiar.

![DNS publication, recursive caching, and client resolution form a discovery path](/assets/img/service-discovery/dns-path.svg)

Its caching model creates limitations:

- resolvers and applications may retain answers until TTL expiry;
- some clients use only one returned address;
- record order is not a complete load-balancing policy;
- rapid health changes may propagate more slowly than desired;
- negative answers can also be cached.

A 30-second TTL is not a promise that every client removes an endpoint in
exactly 30 seconds. Discovery design must tolerate overlapping old and new
views.

---

# 10. Client-Side Discovery Routes Directly

With client-side discovery, the checkout client library obtains an endpoint
snapshot and selects a payment instance:

~~~text
checkout library -> registry/DNS -> endpoint snapshot
checkout library -> selected payment instance
~~~

![A client-side balancer caches discovery state and calls instances directly](/assets/img/service-discovery/client-side.svg)

Advantages:

- no mandatory proxy hop;
- selection can use request keys and caller locality;
- data-plane capacity grows with clients and servers.

Costs:

- every language runtime needs correct discovery and balancing behavior;
- stale caches and retry policies vary by client version;
- registry credentials or a local discovery agent are required;
- rolling out policy changes may require library upgrades.

The library must not choose randomly from a list that includes unready or
incompatible revisions merely because the registry returned them.

---

# 11. Server-Side Discovery Centralizes Routing

With server-side discovery, callers use one stable virtual address. A load
balancer or proxy consumes registry state and chooses a backend:

~~~text
checkout -> stable payment VIP/proxy -> payment instance
                         ^
                         |
                  registry snapshot
~~~

![A proxy consumes discovery metadata on behalf of simpler clients](/assets/img/service-discovery/server-side.svg)

This centralizes policy, security, retries, and observability while keeping
clients simple. It adds a network hop and requires the proxy tier itself to be
available and scalable.

A sidecar or node-local proxy is a hybrid: the application calls a stable local
address, while the proxy on each node performs client-side selection from a
shared control plane.

---

# 12. Discovery State Is Always Versioned and Eventually Stale

At time `t1`, checkout holds endpoint version 17:

~~~text
v17 = {payments-1, payments-2, payments-3}
~~~

The registry removes `payments-2` and publishes version 18. Until checkout
refreshes, it may still call the removed endpoint.

![Registry and caller views overlap during every membership transition](/assets/img/service-discovery/stale-snapshot.svg)

A robust caller treats discovery as a versioned snapshot:

- use the latest complete version, not a partially updated list;
- refresh after explicit stale-route errors;
- apply additions only after readiness;
- retain a last-known-good snapshot during a short control-plane outage;
- bound how long that snapshot remains acceptable;
- stop retrying an endpoint that failed locally before global removal arrives.

No discovery mechanism can make every caller observe membership change at one
instant. The system must remain safe while versions overlap.

---

# 13. Watches Reduce Polling but Do Not Eliminate Re-Reads

A registry may let clients watch a service key. A notification means relevant
state changed; the client should fetch or apply the next version.

Notifications can be delayed, coalesced, duplicated, or lost across a
disconnect. The safe pattern is:

~~~text
read snapshot at version V
establish/watch changes after V
on gap or reconnect -> read a fresh complete snapshot
~~~

![A watch notification triggers versioned re-synchronization](/assets/img/service-discovery/watch-resync.svg)

Thousands of clients reconnecting simultaneously can create a thundering herd.
Use jitter, streaming fanout tiers, node-local agents, or DNS caching so one
registry recovery does not cause every application process to poll at once.

---

# 14. Selection Must Consider Locality and Capacity

An endpoint set is not necessarily a set of equal choices. A policy might
prefer:

1. healthy instances in the caller's zone;
2. healthy instances in another zone in the same region;
3. another region only under an explicit failover policy.

Weights can reflect unequal instance capacity or a canary percentage.
Least-request or latency-aware selection can adapt to work, while deterministic
hashing can preserve cache or session locality.

![Discovery metadata narrows candidates before load balancing chooses one](/assets/img/service-discovery/locality-selection.svg)

Locality improves latency and limits cross-zone traffic, but strict locality
can overload one zone while spare capacity exists elsewhere. Define spillover
thresholds and reserve failover headroom rather than assuming locality and
balance always agree.

---

# 15. Retries Cross the Discovery Boundary

Checkout selects `payments-2`, but the connection fails. It refreshes discovery
and retries `payments-3`.

For a read, that may be straightforward. For `AuthorizePayment`, the first
attempt may have committed before its reply was lost. Discovery can find
another endpoint; it cannot decide whether repeating the business action is
safe.

Use:

- stable idempotency keys;
- per-attempt and overall deadlines;
- bounded retry count and jitter;
- retry budgets during fleet failure;
- protocols that expose stale endpoint or draining responses;
- application status lookup for uncertain outcomes.

Service discovery repairs **where** to send. It does not repair ambiguous
operation semantics.

---

# 16. Bootstrap and Security Form a Trust Root

To discover everything else, a process must first find DNS, a local agent, a
registry, or a proxy. That bootstrap address is intentionally simpler and more
stable than ordinary service membership.

The discovery system also controls where sensitive traffic goes. Protect:

- who may register an instance under a service identity;
- who may read internal topology;
- how endpoint identity is authenticated with TLS;
- which controller may change weights or revisions;
- audit records for registration, removal, and policy change.

An attacker who registers `payments.production -> attacker` has bypassed many
application controls. Discovery metadata is security-sensitive control-plane
state.

---

# 17. Multi-Region Discovery Is a Failure Policy

A global service may publish regional endpoint sets. During a London failure,
callers can move to Dublin only if:

- Dublin has reserved compute and dependency capacity;
- data and authorization semantics permit cross-region service;
- routing changes faster than client deadlines;
- retry storms do not multiply traffic;
- the failed region cannot continue conflicting protected writes.

![Regional discovery needs capacity and data semantics, not only another address](/assets/img/service-discovery/regional-failover.svg)

For stateful services, discovery must follow ownership. It must not route a
write to a healthy server that is no longer the leader or shard owner. Health
answers "is it running?"; metadata generation answers "is it still
authoritative?"

---

# 18. Failure Matrix

| Failure | Visible risk | Containment |
|---|---|---|
| instance crashes | stale endpoint receives calls | lease expiry plus local passive ejection |
| instance pauses | false removal or late responses | conservative lease, generation, idempotency |
| registry follower crashes | reduced redundancy | continue with quorum and replace the replica |
| registry leader crashes | writes and watches pause briefly | elect committed replica, fence old term, reconnect by version |
| registry loses quorum | competing membership histories | reject mutations; callers use bounded cached snapshots |
| registry unavailable | no membership refresh | cached last-known-good snapshot with age limit |
| watch event lost | client remains on old version | version gaps and full resynchronization |
| bad health check | mass endpoint removal | staged policy and independent signals |
| DNS answer cached | calls continue to removed address | drain longer than propagation window |
| region fails | overload of surviving region | failover headroom and bounded retries |
| unauthorized registration | traffic interception | authenticated identities and write ACLs |
| old owner remains live | split-brain writes | ownership epoch or fencing at resource |

---

# 19. Operations and Observability

Measure the complete pipeline:

- registered, ready, draining, and expired instance counts;
- registry term, quorum health, commit index, and replica lag;
- leader-election duration and rejected writes while quorum is unavailable;
- registry commit and watch-delivery latency;
- endpoint snapshot version and age at callers;
- DNS TTL and observed stale-address traffic;
- selection distribution by zone, revision, and endpoint;
- connection failures before and after passive ejection;
- retry amplification during membership changes;
- readiness-to-first-traffic and drain-to-last-traffic time;
- regional spillover and remaining failover headroom.

A useful trace records:

~~~text
service=payments.production
snapshot_version=18
selected=payments-7f9c
selected_zone=eu-west-1a
selection_reason=local_ready
attempt=2
previous_failure=connect_refused
~~~

Without snapshot and selection context, a stale-discovery incident looks like
random backend failure.

---

# 20. The Complete Checkout Call

1. `payments-7f9c` starts but is not yet ready.
2. It loads credentials and dependencies.
3. The controller publishes it as ready under generation 42.
4. The registry leader commits version 18 on a quorum before acknowledging it.
5. Checkout receives the version through DNS, watch, or proxy configuration.
6. Locality policy selects `payments-7f9c`.
7. The request carries a stable idempotency key.
8. During deployment, the instance enters draining state.
9. New snapshots exclude it from new calls.
10. Existing calls finish before process shutdown.
11. If it crashes instead, passive failure detection stops local selection.
12. Lease expiry eventually removes it globally.
13. A reconnecting watcher verifies its version and obtains a full snapshot if
    it missed any event.

![Registration, propagation, selection, draining, and removal form one lifecycle](/assets/img/service-discovery/end-to-end.svg)

---

# 21. Final Mental Model

Service discovery turns a stable logical identity into a changing, versioned
set of eligible endpoints:

~~~text
service identity
  -> authenticated registration
  -> quorum-committed registry history
  -> liveness and readiness
  -> versioned registry state
  -> cached caller or proxy snapshot
  -> locality and capacity policy
  -> selected endpoint
  -> retry or refresh after failure
  -> draining and eventual removal
~~~

The registry is the control plane; application calls are the data plane.
Discovery can tell a caller where an eligible instance is believed to be. It
cannot make that belief instantaneous, prove a timed-out operation failed, add
failover capacity, or fence an old stateful owner by itself.

The core invariant is not "the registry has a list." It is:

> Every request is routed using a bounded-staleness view of authorized,
> compatible endpoints, and overlapping views remain safe during change.

---

# References

1. [RFC 1034 — Domain Names: Concepts and Facilities](https://www.rfc-editor.org/rfc/rfc1034.html)
2. [RFC 1035 — Domain Names: Implementation and Specification](https://www.rfc-editor.org/rfc/rfc1035.html)
3. [RFC 2782 — A DNS RR for Specifying the Location of Services](https://www.rfc-editor.org/rfc/rfc2782.html)
4. [ZooKeeper: Wait-free coordination for Internet-scale systems](https://www.usenix.org/legacy/event/atc10/tech/full_papers/Hunt.pdf)
