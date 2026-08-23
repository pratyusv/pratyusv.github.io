---
layout: single
comments: true
title: "Inside a Load Balancer: Connections, Scheduling, Health, and Failover"
date: 2026-01-15 00:00:00-0000
description: "A first-principles guide to load balancing: stable service addresses, L4 flows, L7 requests, backend eligibility, scheduling, retries, overload, draining, and highly available proxy fleets."
tags: [load-balancing, distributed-systems, networking, tcp, reverse-proxy, system-design]
categories: ['Distributed Systems Components']
---

# 1. Why Load Balancing Exists

A service often begins with one application server:

```text
client -> application server
```

The client resolves the server's address, opens a connection, sends work, and
receives a response. There is no backend pool and no component choosing among
servers.

That design stops being sufficient when one server cannot handle the required
traffic, maintenance must happen without an outage, or a machine can fail. The
service adds replicas:

```text
backend B1
backend B2
backend B3
```

Replicas create two immediate questions. Which address should clients use, and
which replica should receive each new unit of work?

A **load balancer** gives clients a stable service endpoint and selects an
eligible **backend** behind it. The public endpoint can remain unchanged while
backends are added, removed, replaced, or temporarily taken out of service.

```text
                    -> backend B1
client -> balancer  -> backend B2
                    -> backend B3
```

The word “balance” can be misleading. Equal request counts are not necessarily
equal work. One request may finish in a millisecond while another streams for
an hour. One connection can remain idle while another consumes an entire CPU
core or large amounts of bandwidth.

A more precise definition is:

> A load balancer assigns packets, transport flows, protocol requests, or
> application sessions to eligible backends while preserving the correctness
> required by that unit of traffic.

That responsibility includes more than choosing a name from a list. A
production load balancer must:

- keep packets from one stateful flow on a compatible path;
- stop assigning new work to failed or draining backends;
- preserve deadlines and bound retries;
- prevent slow backends from exhausting proxy memory;
- shed work when the remaining fleet has no capacity;
- remain reachable when a load-balancer instance fails;
- expose whether delay occurred in the proxy, its queue, or the backend.

Adding the balancer also inserts another dependency:

```text
client -> load balancer -> backend
```

Every request can now depend on its network reachability, connection state,
CPU, memory, backend view, and failure policy. A load balancer can isolate a
backend failure, but a bad retry or health policy can amplify the same failure.

> **What to remember:** A load balancer provides a stable service path over a
> changing backend fleet. It distributes only the work it understands, using
> incomplete and time-delayed information about backend health and load.

---

# 2. One Payment Request Through the System

Consider a client calling:

```http
GET /payments/42 HTTP/1.1
Host: api.example.com
```

The production path contains several independent selection decisions:

```text
client
    -> DNS or Anycast chooses a region
    -> regional L4 balancer chooses an L7 proxy
    -> L7 proxy matches the payment route
    -> scheduler chooses a payment backend
```

<div>
    <center>{% include figure.html path="assets/img/load-balancers/request_path.svg" alt="A request passing from global traffic steering through regional L4 and L7 load balancers to an application backend" caption="Different layers choose a region, a transport flow owner, an application route, and finally an eligible backend." %}</center>
</div>

Suppose DNS returns regional virtual address `198.51.100.10:443`. A **virtual
IP**, or **VIP**, is a service address presented by the load-balancer tier
rather than the permanent address of one application server. A **listener** is
the configured protocol and port accepting traffic for that address.

The payment route has a backend **pool**:

```text
payments pool
    B1 = 10.0.1.11:8443
    B2 = 10.0.1.12:8443
    B3 = 10.0.1.13:8443
```

For this request, the path might be:

1. an Equal-Cost Multipath (**ECMP**) router hashes the client's flow onto L4 instance `L4-A`;
2. `L4-A` selects L7 proxy `P2` and preserves that flow mapping;
3. `P2` accepts or terminates the client connection and, when configured,
   completes TLS;
4. `P2` parses `GET /payments/42` and chooses the payment route;
5. its current pool view says `B1` and `B2` may receive new work, while `B3`
   is currently ejected;
6. the scheduler selects `B2`;
7. `P2` acquires or opens a backend connection, forwards the request, and
   relays the response.

The state is distributed across layers:

```text
router          flow -> L4-A
L4-A            client flow -> P2
P2              client protocol, route, deadline, retry state
P2 pool         reusable backend connections
control plane   configured and eligible endpoint snapshot
B2              application execution and response state
```

No one layer possesses the entire interaction. A failure claim must therefore
name the state it preserves: VIP reachability, new connections, existing L4
flows, terminated TCP connections, in-flight HTTP requests, or application
sessions. Those are different guarantees.

> **What to remember:** A client sees one service endpoint, but several layers
> can make different selections. Each layer owns only the state required for
> its selection unit.

---

# 3. The Core Operation: Learn, Filter, Select, and Remember

The central load-balancing operation can be reduced to six steps:

```text
learn the available backends
    -> filter them into an eligible pool
    -> select one when a new unit of work begins
    -> remember the choice for its required lifetime
    -> reuse the choice for related traffic
    -> forget it when that lifetime ends
```

This answers two different questions that are easy to combine accidentally:

```text
Which load-balancer instance receives the connection?
Which application backend does that instance select?
```

The network can choose load-balancer instance `LB-A`, while `LB-A` separately
chooses application server `B2`. Those decisions can use different keys,
algorithms, and state.

<div>
    <center>{% include figure.html path="assets/img/load-balancers/decision_memory.svg" alt="A load balancer learning backend membership, filtering eligible servers, selecting a backend, and remembering that choice for related traffic while a new connection starts a new decision" caption="Selection happens at a boundary such as a new flow or request. Related traffic reuses the recorded choice; a later connection can make a new choice." %}</center>
</div>

## How Does the Load Balancer Know Which Servers Exist?

The balancer does not discover a suitable server by searching the network when
a client packet arrives. Its control plane already has a configured route and
a current backend snapshot.

That information may originate from:

- a static configuration file;
- DNS records;
- a Kubernetes or another orchestration API;
- a service registry populated as application instances start and stop;
- a management system pushing endpoints and weights to the proxy fleet.

For the payment route, a control-plane update might describe:

```text
route: payments
configured endpoints: B1, B2, B3

B1  discovered, healthy, accepting new work
B2  discovered, healthy, accepting new work
B3  discovered, unhealthy
```

The set known to exist is not yet the set that may receive traffic. Health,
administrative drain state, locality, and policy produce the eligible pool:

```text
known pool:     B1, B2, B3
eligible pool:  B1, B2
```

Each load-balancer instance normally keeps a local, versioned copy of this
snapshot. It can therefore select a backend without asking the registry or a
database during every request. Two instances may briefly have different
snapshot versions while an update propagates, but both continue making local
decisions from complete snapshots.

## When Does It Choose a Server?

A backend is selected only when a **new selection unit** begins. What counts as
new depends on the layer:

- an L4 balancer commonly selects when the first packet of a TCP connection or
  UDP pseudo-flow arrives;
- a full TCP proxy selects while establishing its backend-side connection;
- an HTTP proxy can select for each request or multiplexed protocol stream;
- a session-affinity policy can constrain several new connections to the same
  backend.

An L4 lookup therefore has two paths:

```text
packet arrives
    -> existing flow mapping? yes -> reuse its backend
                             no  -> select from eligible pool and store mapping
```

It would be incorrect to run round robin for every TCP packet. Consecutive
packets could go to different servers, none of which owns the complete TCP
connection. The scheduling algorithm runs at the start of the connection; the
connection table handles subsequent packets.

At L7, the safe boundary can be smaller. An HTTP/1.1 keep-alive connection may
carry several sequential requests, and an HTTP/2 connection may carry several
concurrent streams. A proxy that understands those protocols can make a new
backend choice at each request or stream boundary even though the client TCP
connection remains attached to the same proxy.

## On What Basis Is the Server Selected?

Selection has two stages:

```text
candidates = filter(pool_snapshot, health, drain, locality, policy)
backend    = schedule(candidates, traffic_key, observed_load)
```

The filter decides who is allowed to receive new work. The scheduler decides
which allowed backend receives this unit of work.

The scheduler may use:

- a rotating counter for round robin;
- configured capacity weights;
- active connection or outstanding-request counts;
- recent latency or endpoint-reported load;
- locality, such as preferring the client's region or the proxy's zone;
- a stable hash of a cookie, tenant, or other affinity key.

This is what “load” means in load balancing: an estimate chosen by the policy.
The balancer cannot see future cost. Evenly distributing ten requests does not
balance the system if one request performs a large report and the other nine
read a cached value. Algorithms choose among imperfect signals; they do not
measure an objective quantity called load.

The selected backend record contains a reachable address and port, not only a
name. If `B2` means `10.0.1.12:8443`, a full proxy opens or reuses a socket to
that address. A NAT balancer instead rewrites the packet's destination to that
address. DSR and tunnelling use their own forwarding operations. “Choose B2”
therefore becomes a concrete network action determined by the forwarding mode.

## What Does It Keep Track Of?

The required memory depends on what the balancer terminates or forwards:

| Component | State it keeps | Typical lifetime |
|---|---|---|
| Router or ECMP layer | Hash configuration or an optional flow assignment | Network flow |
| L4 NAT balancer | Client flow to backend mapping and reverse translation | TCP connection or UDP idle timeout |
| Full TCP proxy | Client socket, backend socket, buffers, deadlines, selected backend | Proxied connection |
| L7 proxy | Parsed request/stream, route, selected endpoint, timeout and retry state | Request or stream |
| Backend connection pool | Reusable proxy-to-backend connections and capacity | Longer than one request |
| Affinity mechanism | Cookie, stable hash input, or session-to-backend mapping | Configured affinity lifetime |
| Application | Login, cart, payment, or other business state | Application-defined session |

Remembering does not always require a table entry. A stateless hash can
recompute the same choice from the same flow or affinity key, as long as every
instance uses compatible membership and hash configuration. Stateful NAT,
full proxying, active counters, and retry handling do require mutable records.

The first six rows are routing or transport state. The last row is application
state. A load balancer may preserve a routing preference for a session, but it
does not automatically own the user's authenticated login, shopping cart, or
payment transaction.

For an L4 NAT flow, a simplified record might be:

```text
(TCP, client IP, client port, VIP, 443)
    -> backend B2
    -> translated addresses and ports
    -> last packet time
```

For a full proxy, the record is not merely `client -> B2`. It includes two live
kernel sockets and the userspace buffers joining them:

```text
client socket <-> proxy connection object <-> B2 socket
```

That difference explains why copying a NAT mapping to a standby can sometimes
preserve packet forwarding, while copying the name `B2` cannot reconstruct a
TCP connection terminated by a failed proxy.

## Does the Load Balancer Maintain the Client Session?

It depends on what “session” means.

The balancer maintains the network and protocol state that it owns. A full TCP
proxy maintains sockets and buffers for the client connection. An HTTP proxy
maintains enough state to match requests with responses. Both release that
state when the relevant connection or request ends.

Application session state is different. A login, cart, or payment workflow is
normally stored in the application, a shared database or cache, or a protected
client token. The load balancer does not learn that state merely by forwarding
the connection.

When stickiness is enabled, the balancer maintains or interprets only the
routing association—for example, `affinity cookie 7f2a -> B2`. That association
says where to send the next request; it is not the contents of the user's cart.
A self-contained signed cookie or stable hash can make this routing choice
consistent across balancer instances without a shared session table. A mutable
affinity table must instead be shared, replicated, or allowed to disappear on
failure.

## Does the Same Client Reach the Same Balancer and Server?

Not necessarily. Consider two connections opened by the same laptop:

```text
connection 1: Alice -> LB-A -> B2
connection 2: Alice -> LB-B -> B1
```

This is normal. A new TCP connection usually has a new source port, so its
network flow key changes. ECMP may consequently send it to another
load-balancer instance. That instance has its own scheduler state and can
select another backend.

The rules are:

- packets belonging to connection 1 must continue through compatible flow
  state and normally remain attached to `LB-A` and `B2`;
- a new connection from Alice is a new selection and can reach `LB-B` and
  `B1`;
- several HTTP requests on one client connection remain attached to the same
  L7 proxy, but that proxy may select different backends per request;
- Alice returns to `B2` across connections only when an explicit affinity
  policy requires it.

The source IP alone is a poor definition of Alice. Thousands of users may
share one public IP through NAT, and Alice's address may change when her laptop
moves from Wi-Fi to a mobile hotspot. A new address and source port create a
new flow identity; neither TCP nor the load balancer inherently knows that the
new connection belongs to the same person.

Cross-connection affinity needs a more durable application-visible key. An L7
proxy can set an integrity-protected cookie, hash a tenant or account ID, or
consult a shared affinity table. All load-balancer instances must interpret
that mechanism consistently if a later connection may reach any one of them.

Often the more resilient design stores durable session data in a shared store
or a client token so any healthy backend can serve the next request. Affinity
can still improve cache locality, but it is then an optimization rather than
the only place the session can survive.

## The Payment Connection as One Complete Loop

The earlier payment example now has a precise sequence:

1. configuration and discovery tell `P2` that `B1`, `B2`, and `B3` belong to
   the payments pool;
2. health and drain state produce an eligible snapshot containing `B1` and
   `B2`;
3. a new request reaches `P2`, creating a new L7 selection boundary;
4. the scheduler chooses `B2` using its configured algorithm and current local
   signals;
5. `P2` records `B2` in the request state and obtains a backend connection
   associated with `B2`;
6. response bytes are matched to the same in-flight request and returned on
   the correct client connection;
7. after completion, request state is released, while the backend connection
   may remain in `B2`'s pool for reuse;
8. the next request can select `B1`, unless an affinity rule deliberately
   constrains it to `B2`.

> **What to remember:** Load balancing is not repeated guesswork for every
> packet. The balancer learns a pool, filters it, selects at a defined boundary,
> and remembers that choice for exactly as long as correctness or policy
> requires.

---

# 4. What Exactly Is Being Balanced?

Several units are commonly conflated:

```text
packet != flow != connection != request != session
```

A **packet** is one network-layer transmission. A TCP byte stream spans many
packets.

A **flow** is a sequence of packets treated as belonging together. For TCP,
the flow corresponds to a transport connection. For connectionless UDP, a
load balancer usually creates a temporary pseudo-flow from packet headers.

A **request** is an application-protocol operation such as one HTTP request or
RPC. One transport connection can carry one request, many sequential requests,
or many concurrent streams.

A **session** is application state that can outlive a connection: a login,
shopping cart, game, or database session. TCP does not automatically preserve
that identity after reconnection.

| Selection unit | Typical mechanism | Stability required |
|---|---|---|
| Packet or flow | ECMP, NAT, packet steering | Related packets normally converge on compatible state |
| TCP connection | L4 load balancing | One backend path for the connection lifetime |
| HTTP request or protocol stream | L7 proxying | Backend may change at a request boundary |
| Application session | Cookie or application-key affinity | Selection can persist across connections |

## Layer 4 Sees Transport Information

A **Layer 4**, or **L4**, balancer works mainly with IP addresses, ports, and
transport protocol. It can distribute TCP connections or UDP flows without
understanding an HTTP path such as `/payments/42`.

For ordinary TCP forwarding:

```text
one client TCP connection -> one selected backend path
```

If TLS passes through unchanged, the L4 balancer sees encrypted application
bytes. It can route using transport identity and sometimes limited handshake
metadata, but it cannot parse the encrypted HTTP request body or path.

## Layer 7 Understands an Application Protocol

A **Layer 7**, or **L7**, proxy terminates or decodes a protocol such as HTTP,
gRPC, or Redis. It can select using hostname, path, header, method, or another
validated protocol field.

```text
HTTP/1.1 connection
    request 1 -> B1
    request 2 -> B3

HTTP/2 connection
    stream 1 -> B1
    stream 3 -> B2
    stream 5 -> B3
```

<div>
    <center>{% include figure.html path="assets/img/load-balancers/l4_l7_selection.svg" alt="Backend selection at connection level for L4, request level for HTTP 1.1, and stream level for HTTP 2" caption="Protocol knowledge changes where a safe selection boundary exists. Connection, request, and stream have different retry and affinity rules." %}</center>
</div>

An L7 HTTPS proxy normally terminates client TLS before parsing HTTP. It can
then use plaintext or a second, independent TLS connection to the backend:

```text
client == TLS A ==> proxy == TLS B ==> backend
```

The two TLS sessions have separate keys and peer identities. Client-to-proxy
encryption says nothing by itself about proxy-to-backend encryption.

A WebSocket begins as an HTTP request, so an L7 proxy can choose a route during
the opening handshake. After upgrade, it becomes a long-lived bidirectional
stream normally attached to that chosen backend.

QUIC uses UDP but has its own connection identity and migration semantics. A
packet-only balancer can use a flow mapping or QUIC connection ID-aware
steering; an HTTP/3 proxy terminates QUIC and can select at the request-stream
layer.

> **What to remember:** The selected unit determines everything that follows:
> the available routing information, how long affinity must last, what can be
> retried, and which state is lost on failure.

---

# 5. L4 Flow Identity and Connection Tracking

A TCP or UDP flow is commonly keyed by:

```text
(protocol, source IP, source port, destination IP, destination port)
```

The source port matters. One client can open many connections to the same VIP,
and thousands of users can share one public address through NAT.

```cpp
struct FlowKey {
    IpAddress sourceIp;
    IpAddress destinationIp;
    std::uint16_t sourcePort;
    std::uint16_t destinationPort;
    std::uint8_t protocol;

    bool operator==(const FlowKey&) const = default;
};
```

For a stateful packet balancer, the first packet and later packets take
different paths:

```text
first TCP SYN
    -> flow-table miss
    -> select eligible backend
    -> install forward and reverse mapping

later packet
    -> flow-table hit
    -> reuse the existing mapping
```

Selecting independently for every packet could send one TCP sequence space to
several backends. Each backend would see only part of a connection it never
established.

An abbreviated mapping operation is:

```cpp
BackendId routeNewOrExistingFlow(const Packet& packet) {
    FlowKey key = fiveTuple(packet);

    if (auto entry = connectionTable.find(key);
        entry != connectionTable.end()) {
        entry->second.lastSeen = Clock::now();
        return entry->second.backend;
    }

    BackendId backend = scheduler.select(key, eligiblePool());
    installBidirectionalMapping(key, backend, Clock::now());
    return backend;
}
```

The reverse mapping is essential when addresses are translated: backend reply
packets must be restored to the VIP/client identity expected by the client.

Mappings also need a lifetime. TCP flags and timers help expire state after a
close or long inactivity. UDP has no SYN, FIN, or transport-level connection
state, so its pseudo-flow normally expires after an idle timeout. A timeout
that is too short breaks valid idle flows; one that is too long fills the table
with dead state.

Connection-table capacity can dominate a balancer serving millions of mostly
idle flows. Requests per second alone does not describe that load.

> **What to remember:** Scheduling chooses a backend only when a new selection
> unit begins. Connection tracking preserves that choice for later packets and
> removes it only when its lifetime ends.

---

# 6. L4 Forwarding Modes Own Different State

“L4 load balancer” describes the selection layer, not one fixed data path.
Four common forwarding shapes make different address, performance, and failure
tradeoffs.

<div>
    <center>{% include figure.html path="assets/img/load-balancers/forwarding_modes.svg" alt="Comparison of full proxy, NAT, direct server return, and IP tunnel forwarding paths" caption="The forwarding mode determines which addresses change, where transport state lives, and whether responses cross the load balancer." %}</center>
</div>

## Full TCP Proxy

A full proxy owns two independent connections:

```text
client <-- TCP A --> proxy <-- TCP B --> backend
```

The client has a TCP connection to the proxy, not to the backend. The proxy can
use independent buffers and timeouts on each side and can pass the original
client address using a trusted metadata protocol.

<div>
    <center>{% include figure.html path="assets/img/load-balancers/connection_state.svg" alt="A full proxy holding separate client-side and backend-side TCP sockets plus a userspace connection object" caption="A full proxy joins two TCP connections with userspace buffers, deadlines, and one selected backend." %}</center>
</div>

Both request and response bytes traverse the proxy. A proxy crash loses the
kernel TCP sockets it terminated; another process cannot reconstruct those
live sequence spaces from the backend list alone.

## Destination and Source NAT

In a NAT design, the client addresses the VIP. The balancer rewrites the
destination to the selected backend and often rewrites the source so the reply
is forced back through the balancer.

```text
client -> VIP
src = 203.0.113.8:51024
dst = 198.51.100.10:443

balancer -> backend
src = 10.0.0.5:43001
dst = 10.0.1.17:8443
```

The reply needs the inverse translation:

```text
backend -> balancer
src = 10.0.1.17:8443
dst = 10.0.0.5:43001

VIP -> client
src = 198.51.100.10:443
dst = 203.0.113.8:51024
```

<div>
    <center>{% include figure.html path="assets/img/load-balancers/nat_rewrite.svg" alt="Packet headers before and after destination and source address translation through a load balancer" caption="NAT preserves one logical flow by applying a reversible translation in both directions." %}</center>
</div>

Source NAT simplifies the return path but hides the original client address
from the backend and consumes translated source-port space.

## Direct Server Return

With **Direct Server Return**, or **DSR**, the balancer selects the inbound path
but the backend sends response packets directly to the client:

```text
request:  client -> balancer -> backend
response: client <----------- backend
```

The backend must accept the VIP without incorrectly advertising ownership on
its local network, and the reply must use the address the client contacted.
DSR removes high-volume response traffic from the balancer but creates an
asymmetric path and reduces response visibility at the balancer.

## IP Tunnelling

Tunnelling wraps the original packet in an outer packet addressed to the
backend. After decapsulation, the backend sees the original destination VIP and
can reply directly. This allows backends beyond the balancer's local Layer 2
network, at the cost of encapsulation overhead and maximum transmission unit
(**MTU**) planning. The MTU is the largest packet a network link can carry
without fragmentation; adding an outer tunnel header leaves less room for the
original packet.

The forwarding mode determines what can survive failure. Full proxies own
transport endpoints. NAT systems own translations. DSR and tunnel directors
may not see replies. A standby can preserve an existing flow only when traffic
reaches it and it has—or can reproduce—the exact state its mode requires.

> **What to remember:** Performance and failure semantics come from the data
> path, not from the label “L4.” Always draw both request and response paths.

---

# 7. Backend Eligibility Comes Before Scheduling

A scheduler cannot safely choose from every address it has ever heard about.
It chooses from the current **eligible pool**.

Eligibility combines independent inputs:

```text
configured for this route
AND present in discovery
AND administratively accepting new work
AND sufficiently healthy
AND within locality and policy constraints
```

## Discovery Describes Intended Membership

Endpoints can come from static configuration, DNS, an orchestrator, a service
registry, or a dynamic discovery stream. Discovery answers:

> Which backend instances are intended to exist for this service?

It does not prove that an endpoint is reachable from this proxy or capable of
serving useful work now.

## Health Is Evidence, Not Truth

Health can be observed at increasing depth:

```text
TCP port accepts
    -> TLS handshake completes
    -> protocol check succeeds
    -> application check succeeds
    -> real requests finish within deadline
```

Deeper checks provide stronger evidence but cost more and can depend on shared
downstream services. Thousands of proxies repeatedly probing a database-backed
health endpoint can create significant production load.

**Active health checking** sends synthetic probes, so it can detect failure
without user traffic. **Passive health checking**, often called outlier
detection, observes real connection failures, resets, timeouts, latency, or
responses. Passive evidence sees the actual path but must distinguish a broken
backend from a request-specific error or proxy-local network problem.

## Health Needs Thresholds and Recovery

One lost probe should not flap a backend in and out of service. A useful health
state machine includes failure thresholds, success thresholds, and gradual
recovery:

<div>
    <center>{% include figure.html path="assets/img/load-balancers/health_states.svg" alt="Backend health state machine from healthy through suspect, unhealthy, and recovering, with administrative draining shown separately" caption="Thresholds reduce flapping, while slow start prevents a recovered backend from receiving its full share immediately. Draining is an administrative state, not a probe result." %}</center>
</div>

Observed health and administrative eligibility should remain separate:

```cpp
enum class ObservedHealth {
    Healthy,
    Suspect,
    Unhealthy,
    Recovering
};

struct BackendState {
    ObservedHealth health{ObservedHealth::Healthy};
    bool acceptingNewWork{true};  // false while draining
    std::uint32_t consecutiveFailures{0};
    std::uint32_t consecutiveSuccesses{0};
};
```

The important transitions are complete in both directions:

```text
Healthy --failure--> Suspect --threshold--> Unhealthy
Suspect --success-------------------------> Healthy
Unhealthy --success threshold------------> Recovering
Recovering --slow start completes---------> Healthy
any state --administrative drain----------> no new selections
```

A recovering backend may have cold caches, empty connection pools, or delayed
dependency initialization. **Slow start** ramps its effective weight from a
small value to the configured value instead of sending a full share
immediately.

## Publish Complete Pool Snapshots

The **data plane** processes live client traffic and performs local selection.
The **control plane** receives configuration, discovery, health, and drain
updates, then publishes the state that the data plane uses.

The data plane should not synchronously query discovery or a configuration
database. The control plane combines membership, health, policy, and drain state,
then publishes an immutable version:

```cpp
struct PoolSnapshot {
    std::uint64_t version;
    std::vector<std::shared_ptr<Backend>> eligible;
};

std::atomic<std::shared_ptr<const PoolSnapshot>> activePool;
```

Readers see either the old complete snapshot or the new complete snapshot.
Existing flows retain their chosen backend; new selections use the latest
eligible pool.

<div>
    <center>{% include figure.html path="assets/img/load-balancers/control_data_plane.svg" alt="Service discovery, active health checks, passive errors, and administrative state producing a versioned backend snapshot for the data plane" caption="The control path publishes complete eligibility snapshots. The forwarding path selects locally rather than querying discovery for every request." %}</center>
</div>

Different balancer instances can temporarily hold different versions. That is
usually safer than putting global consensus into every selection, provided
snapshot age is bounded and locally observed failures can suppress an endpoint
quickly.

If no backend is eligible, policy must explicitly fail fast, use a lower-priority
pool, or enter a carefully bounded degraded mode. Sending to known unhealthy
servers can recover some requests or intensify a cascading failure.

> **What to remember:** Discovery says which backends should exist. Health says
> what this balancer has observed. Administrative state says whether new work
> is allowed. Scheduling begins only after those inputs produce an eligible
> pool.

---

# 8. Scheduling Chooses Which Imperfection to Tolerate

The abstract selection is:

```text
backend = select(traffic_key, eligible_pool, observed_load, policy)
```

No scheduler sees future work. Most see only approximate local counters or
delayed backend measurements.

## Round Robin

Round robin rotates through eligible endpoints:

```cpp
Backend& roundRobin(
    std::span<Backend* const> backends,
    std::atomic<std::uint64_t>& next) {

    const auto position =
        next.fetch_add(1, std::memory_order_relaxed);
    return *backends[position % backends.size()];
}
```

It is cheap and predictable when backends and work are similar. It distributes
selections, not CPU time, bytes, or completion latency.

## Weighted Least Connections

Connection duration makes active count a useful—but incomplete—load signal:

```text
score_i = active_connections_i / configured_weight_i
select the lowest score
```

An idle WebSocket and a connection streaming hundreds of megabits both count
as one. For L7 work, outstanding requests, queue depth, endpoint-reported
utilization, or an exponentially weighted moving average (**EWMA**) of latency
may be better approximations. EWMA gives recent measurements more influence
while retaining some history, so it reacts without following every brief
spike.

## Power of Two Choices

Scanning a very large pool for every selection is expensive. A bounded
alternative samples two eligible endpoints and chooses the less loaded one:

```cpp
Backend& powerOfTwo(
    std::span<Backend* const> backends,
    Random& random) {

    Backend& a = *backends[random.index(backends.size())];
    Backend& b = *backends[random.index(backends.size())];
    return normalizedLoad(a) <= normalizedLoad(b) ? a : b;
}
```

It avoids much of the skew of one random choice while keeping selection work
constant.

## Rendezvous Hashing

Affinity can choose the backend with the greatest stable score:

```text
selected = arg max over b in eligible:
           hash(affinity_key, stable_backend_id_b)
```

Unlike `hash(key) % backend_count`, rendezvous hashing does not remap most keys
when membership changes. Removing one equally weighted backend mainly moves the
keys previously assigned to it.

Hashing balances keys, not their popularity. One celebrity account or dominant
tenant can remain a hot spot.

<div>
    <center>{% include figure.html path="assets/img/load-balancers/scheduling.svg" alt="Flows distributed with round robin, least connections, and rendezvous hashing before and after a backend failure" caption="Algorithms optimize different properties: even selections, observed load, or stable affinity under membership change." %}</center>
</div>

| Algorithm | Useful property | Information required | Typical weakness |
|---|---|---|---|
| Round robin | Cheap even selection counts | Counter | Ignores work size and duration |
| Weighted least connections | Adapts to long connections | Active counters and weights | Connection count is not actual load |
| Power of two | Good balance with bounded work | Sampled load | Each proxy's observations can differ |
| Latency-aware | Reacts to service time | Rolling statistics | Feedback can oscillate or punish cold nodes |
| Rendezvous hash | Stable affinity | Stable key and endpoint IDs | Hot keys stay hot |

Locality policy can first prefer an availability zone or region, then schedule
within it. Failing over too early increases network cost; failing over too late
can overload a damaged locality. Capacity and health thresholds must match that
policy.

> **What to remember:** A scheduling algorithm does not create balance. It
> chooses a backend using a particular approximation of work and a particular
> tolerance for movement or skew.

---

# 9. L7 Proxying Adds Routing and Connection Pools

An L7 proxy parses a validated request before choosing a route and endpoint:

```cpp
Route matchRoute(const HttpRequest& request) {
    if (request.host() == "api.example.com" &&
        request.path().starts_with("/payments/")) {
        return routeTable.at("payments");
    }

    if (request.headers().contains("x-canary")) {
        return routeTable.at("canary");
    }

    return routeTable.at("default");
}
```

Real proxies use hardened protocol implementations. Ambiguous framing,
authority, path, or header interpretation must be rejected or normalized
before routing. A proxy and backend parsing the same request differently is a
security boundary failure.

After endpoint selection, the proxy normally uses a backend connection pool
instead of creating a TCP connection per request:

```text
request selects B2
    -> find reusable B2 connection with stream capacity
    -> otherwise open one within the pool limit
    -> otherwise queue briefly or reject
```

Frontend and backend connection counts therefore differ. HTTP/1.1 often needs
several upstream connections for concurrency. HTTP/2 and HTTP/3 can multiplex
many request streams on one upstream connection, subject to stream limits and
head-of-line or loss behavior at their transport layer.

## Preserve Client Identity Through a Trust Boundary

A full proxy's backend connection originates from the proxy, so the backend
naturally sees the proxy address. Original address or authenticated identity
can be passed through trusted L7 headers or a connection preface such as PROXY
protocol.

The backend must trust those fields only from known proxies that remove or
overwrite client-supplied versions. Otherwise a client can forge its apparent
source or identity.

> **What to remember:** L7 routing selects after parsing a protocol unit. The
> selected request then competes for capacity in that endpoint's backend
> connection pool.

---

# 10. Affinity Is Stronger Than Ordinary Load Balancing

Once an L4 balancer chooses a backend path for a TCP connection, preserving
that mapping is required for transport correctness. This **connection
affinity** is automatic and ends with the connection.

**Application-session affinity**, often called persistence or stickiness, can
span several requests or connections. It needs an application-level key.

## Why Source-IP Hashing Is Coarse

Source-IP hashing requires no cookie:

```text
backend = hash(client_ip) over eligible_backends
```

It performs poorly when many clients share one NAT address, mobile clients
change networks, IPv6 privacy addresses rotate, or one organization's address
range dominates traffic. An IP address is a network locator, not a durable
user-session identity.

## Cookie or Application-Key Affinity

An L7 proxy can set a cookie or hash a stable application key:

```http
Set-Cookie: LB_AFFINITY=backend-17
```

The value should be integrity-protected or opaque so clients cannot select
arbitrary internal endpoints. The proxy must also decide what happens when the
named backend fails or drains. Affinity cannot override unavailability forever.

Strong stickiness can preserve backend-local caches or state, but it also
preserves hot spots and complicates failover. Moving durable session state out
of one backend often makes the pool easier to balance, at the cost of a shared
state dependency or client-token semantics.

Useful mitigations for hot affinity keys include bounded-load hashing,
read-only replication, finer-grained keys, tenant quotas, and deliberately
breaking affinity when an endpoint crosses an overload threshold.

> **What to remember:** Connection affinity preserves a transport flow.
> Session affinity preserves an application choice. Neither guarantees even
> resource consumption.

---

# 11. Backpressure and Overload Decide Who Waits

A load balancer is also a collection of queues, even when configuration never
uses that word:

```text
accepted connections
pending TLS handshakes
requests waiting for a backend connection
bytes waiting for a slow client or backend
retries waiting for another attempt
```

## Backpressure Crosses a Full Proxy

If a backend reads slowly:

```text
backend receive buffer fills
    -> proxy backend send path stops accepting bytes
    -> proxy userspace output queue grows
    -> proxy pauses reads from the client
    -> client send buffer fills
    -> client write slows
```

TCP eventually propagates transport pressure, but the proxy must bound its own
buffers while that happens. Unlimited buffering converts one slow backend into
proxy-wide memory exhaustion.

Every connection table, pending-connect set, request queue, output buffer, and
retry path needs both a limit and a policy for reaching it.

## Latency Multiplies Concurrency

Little's Law relates average in-flight work `L`, arrival rate `lambda`, and
average time in the system `W`:

```text
L = lambda * W
```

At 20,000 requests per second and 50 ms average latency:

```text
L = 20,000 * 0.050 = 1,000 in-flight requests
```

If backend latency rises to 500 ms at the same arrival rate:

```text
L = 20,000 * 0.500 = 10,000 in-flight requests
```

Traffic did not increase, but memory, request concurrency, pool pressure, and
timeout exposure increased tenfold.

## Bound the Waiting Room

Useful limits include:

- accepted and established connections;
- concurrent TLS and QUIC handshakes;
- pending backend connects;
- upstream connections and concurrent streams per endpoint;
- pending requests and their maximum age;
- buffered bytes per connection and process;
- per-route and per-tenant concurrency;
- retries as a fraction of original traffic.

Once useful capacity is exhausted, failing quickly can be safer than accepting
work that cannot finish before its deadline. **Load shedding** deliberately
rejects a bounded part of offered traffic to protect work already admitted.

## Failure Detection Does Not Create Capacity

If four equal backends normally run at 80% utilization, losing one asks the
remaining three to absorb:

```text
4 * 0.80 / 3 = 1.067
```

or approximately 107% of one backend's capacity. Health checking can remove the
failed endpoint perfectly and still cause the other three to fail.

> **What to remember:** A balancer chooses where pressure accumulates. Bounded
> queues and admission policy determine whether overload remains contained or
> becomes a fleet-wide collapse.

---

# 12. Timeouts and Retries Operate Under One Deadline

A request can have several timers:

- client handshake timeout;
- backend connect and TLS timeout;
- time to first response byte;
- per-attempt timeout;
- overall request deadline;
- idle connection timeout;
- maximum stream duration.

They must fit inside one budget. If two seconds of a three-second client
deadline have elapsed, three new two-second attempts are impossible.

```text
overall deadline
    -> time already spent
    -> budget for current attempt
    -> optional budget for a bounded retry
```

An L7 proxy can make a retry decision at a request boundary. A generic L4 proxy
cannot safely replay an arbitrary partial byte stream on a new backend
connection because it does not know the application operation or how much the
backend already processed.

## A Missing Response Creates an Ambiguous Outcome

Consider:

```text
proxy sends POST /charge
    -> backend commits the charge
    -> response is lost
    -> proxy observes a timeout
```

The timeout does not prove the operation failed. Retrying could charge twice.
Safe retry policy depends on method semantics, application idempotency keys,
whether the request body can be replayed, and how much deadline remains.

Automatic retries are strongest when:

- the operation is idempotent or deduplicated by a stable idempotency key;
- no response has been committed to the client;
- the body is buffered or reproducibly replayable;
- a different eligible endpoint exists;
- the overall deadline still has useful budget;
- attempts and aggregate retry traffic are bounded.

## Retry Amplification

<div>
    <center>{% include figure.html path="assets/img/load-balancers/retry_amplification.svg" alt="A backend slowdown causing timeouts, retries, additional work, and a cascading feedback loop" caption="A retry is new offered load. Budgets and admission control prevent recovery traffic from becoming the incident." %}</center>
</div>

The unstable loop is:

```text
backend slows
    -> requests time out
    -> proxies retry
    -> backend fleet receives more work
    -> queues and latency grow
    -> more requests time out
```

A **retry budget** limits retries relative to original traffic. Randomized
delay reduces synchronized attempts. Circuit breakers stop new work from
accumulating behind a dependency with no remaining capacity.

Metrics must preserve original failures even when a retry eventually succeeds;
otherwise retries can hide the incident while consuming the headroom needed to
recover.

> **What to remember:** A timeout is not proof that an operation did nothing.
> A retry is another request with additional load and potentially ambiguous
> side effects.

---

# 13. Draining and Failure Affect New and Existing Work Differently

Removing a backend for deployment should not resemble a crash:

```text
mark backend not accepting new work
    -> publish an eligible snapshot without it
    -> preserve existing connections and requests
    -> wait for completion or deadline
    -> notify long-lived clients when possible
    -> close remaining work
    -> remove endpoint state
```

The backend object cannot disappear as soon as discovery removes it. Existing
connections and in-flight requests still hold references to their selected
endpoint.

Short HTTP requests may drain in seconds. WebSockets, database sessions,
long-polling requests, and streaming RPCs can remain open for hours. A finite
policy needs a grace period, protocol-specific notice where possible, a hard
deadline, and randomized client reconnect behavior.

## Unexpected Backend Failure

When a backend crashes:

- new selections should stop after local evidence reaches policy threshold;
- existing TCP connections reset or time out;
- safe L7 requests may retry within their original deadline;
- arbitrary L4 byte streams cannot migrate;
- discovery and health views converge afterward.

Choosing a replacement for new work is much easier than preserving existing
transport or application execution state.

## The Load-Balancer Tier Must Also Be Redundant

Once several backends sit behind one proxy, that proxy is the next failure
domain.

<div>
    <center>{% include figure.html path="assets/img/load-balancers/load_balancer_ha.svg" alt="Active passive virtual IP failover, active active ECMP distribution, and anycast regional load balancing" caption="Restoring a path to the VIP is different from preserving state owned by a failed load-balancer instance." %}</center>
</div>

### Active/Passive

One instance owns or advertises the VIP while another waits. On failure, the
standby takes over and network state converges. New connections can recover
once the VIP is reachable. Existing full-proxy connections usually cannot,
because their client and backend TCP sockets belonged to the failed kernel.

### Active/Active

ECMP can distribute flows across several instances. Routers commonly hash
packet headers so one flow converges on one next hop. Membership changes can
alter paths, flow sizes remain uneven, and each stateful connection still has
one actual owner.

### Anycast Across Sites

Several regions can advertise the same service address. Routing selects a
reachable, policy-preferred site. A route change can move new connections, but
Internet routing is not an application-session migration protocol.

### State Synchronization Has a Precise Limit

Packet-forwarding balancers can replicate NAT or connection mappings to a
standby. Replication has delay, ordering, bandwidth, and fencing requirements.
A copied mapping may preserve forwarding but cannot recreate terminated
userspace TCP/TLS sockets.

“Highly available” should therefore be qualified:

```text
VIP becomes reachable again
new connections succeed
existing packet mappings survive
terminated TCP connections survive
in-flight requests are retried
application sessions resume
```

Each line is a stronger and different guarantee.

> **What to remember:** Topology changes first affect eligibility for new work.
> Existing flows survive only when their exact owner and state survive or the
> application has an explicit reconnect and retry mechanism.

---

# 14. Optional Implementation Deep Dive: A Full TCP Proxy

The system model does not require a particular implementation. This section
shows why a production full proxy uses explicit non-blocking state instead of
one blocking thread per relay direction. It can be skipped without changing
the load-balancing decisions above.

The simplest conceptual relay is:

```text
read client bytes -> write them to backend
read backend bytes -> write them to client
```

Using two blocking threads demonstrates TCP's full-duplex behavior, but cleanup
is subtle. One direction can fail while the other sleeps indefinitely; an
uncaught exception in a worker thread can terminate the process; and requesting
thread cancellation does not interrupt an arbitrary blocking `recv()` by
itself. Production code coordinates socket shutdown and ownership explicitly.

The C++ fragments below expose those state contracts. They are connected pieces
of a proxy design, not a complete server with listener setup, TLS, backend
selection, logging, and every error path.

## One Object Joins Two Independent Sockets

```cpp
enum class Phase {
    ConnectingToBackend,
    Forwarding,
    ClientHalfClosed,
    BackendHalfClosed,
    Closing
};

struct Endpoint {
    int fd{-1};
    std::deque<PendingWrite> output;
    std::size_t queuedBytes{0};
    bool readPaused{false};
    bool readClosed{false};
    bool writeClosed{false};
};

struct ProxiedConnection {
    std::uint64_t id;
    Endpoint client;
    Endpoint backend;
    BackendId selectedBackend;
    Phase phase{Phase::ConnectingToBackend};
    TimePoint connectDeadline;
    TimePoint idleDeadline;
};
```

An event loop watches both descriptors. **Readiness** means a non-blocking
operation may make progress; it does not promise a complete application
message or that every queued byte can be written.

On Linux, **epoll** is the kernel interface commonly used for this watch list.
The proxy registers each socket descriptor and the events it currently cares
about, such as readable or writable. A call to `epoll_wait()` then returns only
descriptors whose state may permit progress:

```text
register client and backend sockets
    -> epoll_wait() returns a ready descriptor
    -> attempt non-blocking recv(), send(), accept(), or connect completion
    -> update buffers and the events of interest
    -> return to epoll_wait()
```

The proxy does not ask every connection whether it has work on each loop. The
kernel records relevant state changes and returns a batch of candidates. The
application still must call the non-blocking socket operation and handle
`EAGAIN`, because readiness can change before the operation runs and one event
does not imply that an entire request is available.

## The Read Handler Enforces a High-Water Mark

The destination queue limit is checked before reading more source bytes:

```cpp
void ProxyConnection::onReadable(
    Endpoint& from,
    Endpoint& to) {

    while (true) {
        if (to.queuedBytes >= limits_.highWaterBytes) {
            pauseReadableInterest(from);
            from.readPaused = true;
            return;
        }

        const std::size_t remaining =
            limits_.highWaterBytes - to.queuedBytes;
        const std::size_t capacity =
            std::min(readBuffer_.size(), remaining);

        const ssize_t n =
            ::recv(from.fd, readBuffer_.data(), capacity, 0);

        if (n > 0) {
            enqueueCopy(
                to,
                readBuffer_.data(),
                static_cast<std::size_t>(n));
            continue;
        }

        if (n == 0) {
            from.readClosed = true;
            requestHalfCloseAfterFlush(to);
            return;
        }

        if (errno == EINTR)
            continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK)
            return;

        failConnection(ReadError{errno});
        return;
    }
}
```

The write handler advances offsets only by the number of bytes `send()`
accepts. Once the destination queue falls below a lower watermark, it can
resume readable interest on the opposite endpoint:

```text
client read -> backend output queue reaches high watermark
    -> pause client reads
backend writes -> queue falls below low watermark
    -> resume client reads
```

Using separate high and low thresholds avoids rapidly toggling read interest at
one exact byte count.

## Half-Close Is Directional

TCP is full duplex. If the client sends `FIN`, it has closed only its sending
direction. The proxy can finish flushing already-read client bytes, half-close
the backend write direction, and continue relaying a backend response. Closing
both sockets immediately can truncate valid data.

## Multi-Core Ownership

Modern NICs expose several receive queues. Receive-side scaling hashes flows so
different queues and CPUs can process traffic in parallel while preserving
flow locality.

A high-throughput proxy often aligns:

```text
NIC receive queue
    -> CPU
    -> event-loop shard
    -> connection-table shard
    -> backend connection ownership
```

```cpp
struct WorkerShard {
    EpollLoop loop;
    ConnectionMap connections;
    BackendCounters localCounters;
};
```

Each connection has one mutable owner. Configuration snapshots can be shared
immutably. Local load counters are cheap but incomplete; globally synchronized
counters are fresher but introduce contention. Approximate local information is
often preferable on the live data plane.

Implementations can use ordinary sockets, kernel facilities such as IPVS or
eBPF, or userspace packet frameworks. Moving forwarding earlier in the packet
path can reduce per-packet work, but application-aware routing still requires
protocol state somewhere.

> **What to remember:** The proxy owns two independently progressing transport
> endpoints. Explicit buffers, watermarks, half-close state, deadlines, and one
> event-loop owner make that relationship bounded and race-resistant.

---

# 15. The Proxy Is a Security Boundary

An Internet-facing proxy commonly enforces:

- TLS versions, ciphers, certificates, and client-certificate policy;
- connection, request, and tenant rate limits;
- header and body size limits;
- protocol framing and normalization;
- route authorization and backend reachability;
- trusted propagation of client identity.

A permissive route can expose an internal backend. Conflicting HTTP framing or
authority interpretation can make proxy and backend enforce different rules.
Trusting an unsanitized forwarding header lets a client forge identity.

## Resource Exhaustion Is Often About State

An attacker need not send high bandwidth if it can hold scarce state:

- incomplete TCP connections;
- slow TLS handshakes;
- slowly transmitted headers or bodies;
- many idle keep-alive connections;
- oversized or high-cardinality routing values;
- requests that occupy expensive backend slots.

Protection is staged:

```text
connection-establishment limit
    -> handshake deadline
    -> header size and time limit
    -> body and route limit
    -> backend admission and retry limit
```

One global requests-per-second limit cannot protect every resource.

When TLS terminates at the proxy, the backend authenticates its proxy-side peer,
not the original TCP peer. Client identity must travel through an authenticated,
trusted mechanism. Re-encrypting to the backend and validating backend identity
prevents the internal hop from becoming an unauthenticated plaintext boundary.

---

# 16. Capacity and Observability Must Follow the Decision Path

A load balancer can saturate on different dimensions:

- packets per second for small packets;
- bytes per second for large responses;
- new connections or TLS handshakes per second;
- concurrent connections and L7 streams;
- request parsing and policy evaluation;
- connection-table entries;
- pending requests and buffered bytes;
- metric and log cardinality.

“One million requests per second” says little without message size, connection
reuse, protocol, TLS behavior, latency distribution, and failure state.

## Measure Each Stage

Separate at least:

```text
proxy queue time
backend connect time
backend TLS time
time to first response byte
response transfer time
total proxy time
```

Useful data-plane metrics include:

- accepted, active, and rejected connections;
- requests, packets, and bytes;
- flow-table occupancy and expiration;
- pending backend connects and pool saturation;
- request queue depth and oldest age;
- buffered bytes per connection, worker, and process;
- original attempts, retries, and retry outcomes;
- resets, timeouts, and shed traffic;
- selections, completions, and latency per backend and locality.

Useful control-plane metrics include:

- pool snapshot version and age;
- discovery additions and removals;
- active-health transitions and passive ejections;
- eligible fraction and all-unhealthy events;
- drain and slow-start duration;
- rejected configuration and rollback.

Fleet averages can hide one overloaded backend, route, tenant, worker shard, or
availability zone. Dimensions must be rich enough to locate skew but bounded
enough not to overload the monitoring system.

## Capacity for Failure

Planning includes the largest expected burst plus:

- one proxy instance unavailable;
- one backend or availability zone unavailable;
- deployment overlap;
- retry and health-check traffic;
- certificate rotation and cold backend caches;
- long-lived connection reconnection storms;
- observability overhead during an incident.

The target is predictable latency and recovery with spare capacity, not the
largest benchmark number under a perfectly healthy steady state.

---

# 17. The Payment Failure, End to End

Return to the established payment path:

```text
client -> L4-A -> L7 proxy P2 -> payment backend B2
```

Suppose `B2` develops a dependency problem.

1. Requests to `B2` exceed their per-attempt timeout.
2. Passive outlier detection records local timeouts.
3. A replayable `GET /payments/42` receives one bounded retry on `B3` within
   its original deadline.
4. The retry budget prevents every failure from creating another attempt.
5. `B2` crosses the ejection threshold and disappears from new local
   selections.
6. Active probes also fail, providing independent evidence.
7. Metrics retain the original timeout even if the retry succeeds.

The client may receive a response, but the incident was not free: the proxy
spent additional latency and backend capacity to hide it.

When `B2` begins passing probes, it enters `Recovering`. Slow start raises its
effective weight while real success and latency confirm that it can serve a
normal share again.

If `P2` fails during the client connection, its client-side TCP/TLS state and
backend pool disappear. Another proxy can accept a new connection, but it
cannot recreate the old one from route configuration. The client reconnects,
and only semantically safe operations are retried.

If `L4-A` fails, new flows can move to another L4 instance. Existing flows
survive only if the forwarding design and replicated state provide that exact
guarantee.

## What Load Balancing Can Provide

With correct policy and sufficient capacity, the layer can provide:

- one service endpoint over a changing backend fleet;
- local selection among eligible endpoints;
- connection, request, or stream distribution;
- isolation and draining of failed or planned-removal backends;
- bounded admission, queues, and retries;
- protocol termination and application routing;
- observability at a shared traffic boundary.

It does not automatically provide:

- exactly-once request execution;
- preservation of arbitrary TCP connections after their owner fails;
- durable application sessions;
- perfect or instantaneous health knowledge;
- equal CPU or bandwidth consumption;
- spare capacity after a failure;
- safe retries for non-idempotent work;
- globally identical backend views at every instant.

The complete model is:

```text
stable service address
    -> a new flow, connection, request, or session boundary begins
    -> filter the known pool into eligible backends
    -> select using an imperfect load signal or affinity key
    -> turn the selected endpoint into a connection or forwarding action
    -> remember and reuse the choice for related traffic
    -> expire the mapping at its defined lifetime
    -> bound queues, deadlines, and retries
    -> drain or fail over at an explicit state boundary
```

A load balancer makes a changing fleet appear as one reachable service only
when its ownership, capacity, and recovery guarantees are stated precisely.

---

# Compact Glossary

| Term | Direct meaning |
|---|---|
| Backend | Server endpoint eligible to receive selected work. |
| Pool | Set of candidate backends for a service or route. |
| VIP | Virtual service IP presented by a load-balancer tier. |
| Listener | Configured address, port, and protocol accepting traffic. |
| Selection unit | Packet, flow, connection, request, stream, or session assigned as one unit. |
| Backend snapshot | One complete local version of known endpoints and their selection metadata. |
| L4 load balancer | Balancer selecting mainly from network and transport information. |
| L7 proxy | Proxy parsing an application protocol before route or backend selection. |
| ECMP | Equal-Cost Multipath routing; commonly hashes flows across equivalent next hops. |
| Flow key | Fields used to associate packets with one forwarding decision. |
| Connection tracking | State preserving a chosen mapping for later packets in a flow. |
| Full proxy | Proxy terminating one connection and opening another to a backend. |
| NAT | Reversible rewriting of network addresses or ports. |
| DSR | Direct Server Return; backend response bypasses the inbound director. |
| Eligibility | Whether configuration, discovery, health, drain, and policy allow new work. |
| Active health check | Synthetic probe generated by the load-balancing system. |
| Passive health check | Failure evidence observed from real traffic. |
| Slow start | Gradual restoration of a recovering backend's effective weight. |
| Affinity | Policy preserving a backend choice beyond one independent selection. |
| Application session | Business state such as a login or cart that may outlive one connection. |
| Backpressure | Downstream capacity is lower than offered upstream work. |
| Load shedding | Deliberate rejection used to protect admitted work during overload. |
| Retry budget | Bound on retry attempts or retry traffic relative to original work. |
| Draining | Stop new assignments while allowing existing work a bounded completion period. |
| Data plane | Components processing and forwarding live traffic. |
| Control plane | Components distributing configuration and backend eligibility state. |

---

# References

1. IETF, [RFC 9293: Transmission Control Protocol](https://www.rfc-editor.org/rfc/rfc9293.html)
2. IETF, [RFC 9110: HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html)
3. IETF, [RFC 9000: QUIC](https://www.rfc-editor.org/rfc/rfc9000.html)
4. Linux kernel documentation, [IPVS sysctls](https://docs.kernel.org/networking/ipvs-sysctl.html)
5. Linux kernel documentation, [Scaling in the Linux Networking Stack](https://docs.kernel.org/networking/scaling.html)
6. Envoy, [Life of a Request](https://www.envoyproxy.io/docs/envoy/latest/intro/life_of_a_request.html)
7. Envoy, [Service Discovery](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/service_discovery)
8. Envoy, [Health Checking](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/health_checking)
9. Envoy, [Outlier Detection](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/outlier)
10. HAProxy, [Backends and Load-Balancing Algorithms](https://www.haproxy.com/documentation/haproxy-configuration-tutorials/proxying-essentials/configuration-basics/backends/)
