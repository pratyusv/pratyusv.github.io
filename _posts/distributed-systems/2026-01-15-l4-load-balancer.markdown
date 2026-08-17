---
layout: single
comments: true
title: "Inside Load Balancers: Connection Tracking, Scheduling, Health Checks, and Failover"
date: 2026-01-15 00:00:00-0000
description: "A bottom-up examination of L4 and L7 load balancers, including TCP proxying, NAT, connection tracking, scheduling, health checks, retries, draining, overload, and high availability."
tags: [load-balancing, distributed-systems, networking, tcp, reverse-proxy, system-design]
categories: ['Distributed Systems Components']
---

# 1. Introduction

## Start With One Server

A service begins with a simple request path:

~~~text
client -> application server
~~~

The client resolves an address, opens a connection, sends a request, and receives a response. The server owns the entire interaction. There is no routing decision after DNS, no backend pool, and no intermediate component deciding whether the server is healthy.

That model stops being sufficient when the service needs more throughput than one server can provide, maintenance without an outage, or recovery from a server failure. Adding servers creates a new problem:

> Given several possible servers, which one should receive each unit of traffic, and how should that decision change as load and health change?

A load balancer answers that question.

## Distribution Is Only Half the Problem

It is tempting to define a load balancer as a component that spreads requests evenly across servers. That definition omits the difficult parts.

A production load balancer also has to:

- preserve the mapping of packets belonging to an existing connection,
- stop assigning new work to failed or draining servers,
- react to topology changes without destabilizing the fleet,
- bound queues, connection state, and retries,
- preserve the client identity that backends need,
- remain available when a load-balancer instance fails,
- expose enough information to separate backend latency from proxy latency.

Even distribution is not always the goal. Two HTTP requests can consume radically different amounts of CPU. Two TCP connections can remain open for milliseconds or days. A server with the fewest connections may still be doing the most work.

The more useful definition is:

> A load balancer assigns packets, connections, requests, or application sessions to eligible backends while preserving the correctness required by that unit of traffic.

## The Load Balancer Becomes Part of the System

Adding a load balancer removes the direct dependency on one application server, but inserts a new component into the request path:

~~~text
client -> load balancer -> backend
~~~

Every request may now depend on the load balancer's CPU, memory, network capacity, configuration, health view, timeout policy, and failure behavior. The load balancer can isolate backend failures, but a bad retry policy can amplify them. It can hide backend addresses, but an unavailable virtual IP can hide the entire healthy fleet.

This article develops the machinery from the connection upward. It begins with a small TCP proxy, separates the data plane from the control plane, and then extends the model through L7 routing, health checking, retries, overload control, and a highly available load-balancing tier.

---

# 2. A Precise System Model

## Packet, Connection, Request, and Session

Four units are commonly conflated:

~~~text
packet != connection != request != session
~~~

A **packet** is one network-layer transmission. A TCP byte stream is carried across many packets.

A **connection** is transport state identified by endpoints and protocol. One TCP connection may carry one request, many sequential HTTP/1.1 requests, or many concurrent HTTP/2 streams.

A **request** is an application-protocol operation such as an HTTP request or RPC. An L7 proxy can understand this unit; a generic L4 forwarder cannot.

A **session** is application state spanning one or more requests or connections. A login session or shopping cart is not automatically the same thing as a TCP connection.

The selection unit determines what the load balancer must remember:

| Selection unit | Typical mechanism | Required stability |
|---|---|---|
| Packet | ECMP or stateless packet steering | Packets in one flow must normally converge |
| Connection | L4 load balancing | One backend for the connection lifetime |
| Request or stream | L7 proxying | Backend may change between requests |
| Session | Cookie or application-key affinity | Backend remains stable across connections |

## Listener, VIP, Pool, and Backend

Clients connect to a listener, commonly represented by a virtual IP and port:

~~~text
VIP = 198.51.100.10:443
~~~

The listener is backed by a pool:

~~~text
payments:
  10.0.1.11:8443
  10.0.1.12:8443
  10.0.1.13:8443
~~~

Each address identifies a backend endpoint. A backend is **eligible** only if configuration, discovery, administrative state, and health policy all allow new work to be assigned to it.

The selection operation can be written abstractly as:

~~~text
backend = select(traffic_key, eligible_backends, observed_load, policy)
~~~

The selection policy may use no state, local state, or externally reported state. The result is only as current as those inputs.

## Four Useful Properties

A load-balancing design can be evaluated through four properties.

**Flow correctness:** traffic belonging to a stateful connection is not accidentally sent to different backends.

**Availability:** a failed backend or proxy does not unnecessarily make the whole service unavailable.

**Balance:** offered work is distributed in proportion to useful backend capacity rather than merely by request count.

**Stability:** adding, removing, or temporarily ejecting a backend does not move more affinity-bound traffic than necessary.

These properties can conflict. Strong affinity improves locality but can preserve a hot spot. Fast failure detection reduces the time spent sending to a dead server but increases sensitivity to transient packet loss. Synchronizing connection state can improve failover continuity but adds coordination and write traffic to the load-balancer tier.

---

# 3. Where Load Balancing Happens

## More Than One Layer

Large systems rarely have exactly one load-balancing decision:

<div>
    <center>{% include figure.html path="assets/img/load-balancers/request_path.svg" alt="A request passing from global traffic steering through regional L4 and L7 load balancers to an application backend" caption="Different layers choose a region, a transport endpoint, an application route, and finally a backend." %}</center>
</div>

A typical path may contain:

~~~text
DNS or anycast
  -> regional L4 load balancer
  -> L7 reverse proxy
  -> application backend
~~~

The first layer chooses a region or point of presence. The L4 layer distributes transport connections at high packet rates. The L7 layer terminates a protocol and applies application-aware policy. The last selection may happen in a client library or service-mesh sidecar rather than a centralized proxy.

## Data Plane and Control Plane

The **data plane** processes live traffic. It parses the minimum necessary state, selects a backend, forwards bytes or requests, and applies timeouts. Its latency is visible to users.

The **control plane** supplies configuration and eligibility information:

- listeners and routes,
- discovered backend endpoints,
- backend weights,
- certificates,
- active health results,
- administrative drain state.

The common forwarding path should not synchronously query DNS, a service registry, or a configuration database. Instead, the control plane publishes a snapshot that the data plane can read locally.

This separation is the first important performance rule: topology changes may involve distributed coordination, but ordinary backend selection should be a local operation.

---

# 4. Build a Small TCP Proxy

## The Full-Proxy Shape

The easiest L4 load balancer to reason about is a full TCP proxy. It owns two independent connections:

~~~text
client <-- TCP connection A --> proxy <-- TCP connection B --> backend
~~~

The client does not have a TCP connection to the backend. The proxy terminates the client-side connection, opens a separate backend-side connection, and copies the byte stream in both directions.

<div>
    <center>{% include figure.html path="assets/img/load-balancers/connection_state.svg" alt="A full proxy holding separate client-side and backend-side TCP sockets plus a userspace connection object" caption="A full proxy owns two TCP connections. The userspace object joins their buffers, deadlines, and selected backend." %}</center>
</div>

This matters during failure. If the backend connection is reset, the client connection does not magically move to another backend. At L4, the proxy does not know whether replaying arbitrary bytes on a new connection would be valid.

## A Deliberately Blocking Version

A minimal teaching implementation can accept one client, connect to one backend, and relay bytes:

~~~cpp
void relayOneDirection(int source, int destination) {
    std::array<std::byte, 16 * 1024> buffer;

    for (;;) {
        const ssize_t received =
            ::recv(source, buffer.data(), buffer.size(), 0);

        if (received == 0) {
            ::shutdown(destination, SHUT_WR);
            return;
        }

        if (received < 0) {
            if (errno == EINTR)
                continue;
            throw std::system_error(errno, std::generic_category());
        }

        size_t written = 0;
        while (written < static_cast<size_t>(received)) {
            const ssize_t n = ::send(
                destination,
                buffer.data() + written,
                received - written,
                MSG_NOSIGNAL);

            if (n < 0) {
                if (errno == EINTR)
                    continue;
                throw std::system_error(errno, std::generic_category());
            }
            written += static_cast<size_t>(n);
        }
    }
}
~~~

Two relay loops are required because TCP is full duplex:

~~~cpp
void proxyConnection(int client_fd, const sockaddr* backend) {
    const int backend_fd = connectBlocking(backend);

    std::jthread upstream([&] {
        relayOneDirection(client_fd, backend_fd);
    });

    relayOneDirection(backend_fd, client_fd);
}
~~~

This code exposes the semantics, but it is not a production implementation. A blocked receive consumes a thread. Slow writes stall progress. Error paths need coordinated cleanup. There are no connection limits, deadlines, health checks, or graceful shutdown.

## The Connection Object

A scalable proxy moves both sockets into non-blocking mode and records their state explicitly:

~~~cpp
enum class Phase {
    ConnectingToBackend,
    Forwarding,
    ClientHalfClosed,
    BackendHalfClosed,
    Closing
};

struct Endpoint {
    int fd{-1};
    std::vector<std::byte> pending_output;
    bool read_closed{false};
    bool write_closed{false};
};

struct ProxiedConnection {
    uint64_t id;
    Endpoint client;
    Endpoint backend;
    BackendId selected_backend;
    Phase phase{Phase::ConnectingToBackend};
    TimePoint last_activity;
};
~~~

An event loop watches both descriptors. Readiness does not mean an entire message can be read or written; it means an operation can make progress without sleeping.

~~~cpp
void onReadable(Endpoint& from, Endpoint& to) {
    std::array<std::byte, 16 * 1024> buffer;

    for (;;) {
        const ssize_t n =
            ::recv(from.fd, buffer.data(), buffer.size(), 0);

        if (n > 0) {
            to.pending_output.insert(
                to.pending_output.end(),
                buffer.begin(),
                buffer.begin() + n);
            continue;
        }

        if (n == 0) {
            from.read_closed = true;
            requestHalfCloseAfterFlush(to);
            return;
        }

        if (errno == EINTR)
            continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK)
            return;

        failConnection();
        return;
    }
}
~~~

The corresponding write handler drains only as much as the kernel accepts. The proxy must retain the remainder and request writable notifications.

## Backpressure Crosses the Proxy

Suppose the backend reads slowly:

~~~text
backend receive buffer fills
-> proxy backend send buffer fills
-> proxy userspace queue grows
-> proxy stops reading from client
-> client send buffer fills
-> client write slows
~~~

TCP eventually propagates pressure, but the proxy must bound its userspace queue while that happens. Unlimited buffering converts one slow backend into proxy-wide memory exhaustion.

**Invariant:** every queue, connection table, pending-connect set, and retry loop in the load balancer needs a limit.

---

# 5. Flow Identity and Connection Tracking

## The Five-Tuple

A TCP or UDP flow is commonly identified by:

~~~text
(source IP, source port, destination IP, destination port, protocol)
~~~

In C++:

~~~cpp
struct FlowKey {
    IpAddress source_ip;
    IpAddress destination_ip;
    uint16_t source_port;
    uint16_t destination_port;
    uint8_t protocol;

    bool operator==(const FlowKey&) const = default;
};
~~~

The source port is essential. Thousands of connections can originate from the same client address, and many clients may share one public address through NAT.

## First Packet Versus Later Packets

A stateful packet load balancer treats the first packet differently:

~~~cpp
BackendId routePacket(const Packet& packet) {
    FlowKey key = fiveTuple(packet);

    if (auto entry = connection_table.find(key);
        entry != connection_table.end()) {
        entry->second.last_seen = Clock::now();
        return entry->second.backend;
    }

    BackendId selected = scheduler.select(key, active_pool());
    connection_table.emplace(
        key,
        ConntrackEntry{
            .backend = selected,
            .last_seen = Clock::now(),
            .tcp_state = TcpState::SynSeen
        });
    return selected;
}
~~~

The normal path is:

~~~text
first SYN:
  lookup miss -> select backend -> install mapping

later packet:
  lookup hit -> reuse backend
~~~

Selecting independently for every packet could send packets from one TCP connection to different backends. Neither backend would have the complete sequence space or socket state.

## Connection State Has a Lifetime

Entries cannot remain forever. The load balancer observes enough TCP flags and timing to expire state after closure or inactivity. UDP has no handshake or FIN, so a UDP mapping is usually a time-bounded pseudo-flow.

Timeout selection is a tradeoff:

- too short, and an idle but valid flow loses its mapping;
- too long, and dead flows consume table capacity;
- inconsistent across redundant load balancers, and failover changes behavior.

The connection table is often a primary capacity dimension. Maximum requests per second alone does not describe a proxy that serves millions of mostly idle connections.

---

# 6. L4 Forwarding Modes

## Four Different Data Paths

The phrase “L4 load balancer” does not imply one forwarding mechanism.

<div>
    <center>{% include figure.html path="assets/img/load-balancers/forwarding_modes.svg" alt="Comparison of full proxy, NAT, direct server return, and IP tunnel forwarding paths" caption="The forwarding mode determines which addresses change, where connection state lives, and whether responses cross the load balancer." %}</center>
</div>

## Full Proxy

The full proxy owns client-side and backend-side sockets. It can apply independent timeouts, buffer data, preserve the client address through the PROXY protocol, and terminate TLS if configured to do so.

Both directions cross the proxy:

~~~text
client -> proxy -> backend
client <- proxy <- backend
~~~

The cost is that every byte is processed through the proxy's socket and userspace path unless specialized acceleration is used.

## Destination and Source NAT

In a NAT design, clients address the VIP. The load balancer rewrites the destination to the selected backend. It may also rewrite the source so the backend's reply returns through the load balancer.

<div>
    <center>{% include figure.html path="assets/img/load-balancers/nat_rewrite.svg" alt="Packet headers before and after destination and source address translation through a load balancer" caption="NAT preserves one logical connection by applying a reversible translation in both directions." %}</center>
</div>

For example:

~~~text
client -> VIP
src = 203.0.113.8:51024
dst = 198.51.100.10:443

load balancer -> backend
src = 10.0.0.5:43001
dst = 10.0.1.17:8443
~~~

The reverse packet must be translated consistently:

~~~text
backend -> load balancer
src = 10.0.1.17:8443
dst = 10.0.0.5:43001

VIP -> client
src = 198.51.100.10:443
dst = 203.0.113.8:51024
~~~

SNAT makes the return path easy to enforce but hides the original client address from the backend unless it is communicated another way. It also consumes source-port space for translated connections.

## Direct Server Return

With Direct Server Return, the load balancer handles inbound selection but the backend sends the response directly to the client:

~~~text
request:  client -> load balancer -> backend
response: client <- backend
~~~

The backend must accept traffic for the VIP without claiming it incorrectly on the local network. The response must use the VIP as its source so the client sees the endpoint it contacted.

DSR removes response bandwidth from the load balancer, which is valuable when responses are much larger than requests. The cost is asymmetric routing, specialized host configuration, and reduced visibility into the response path.

## IP Tunnelling

Tunnelling encapsulates the original packet inside an outer IP packet addressed to the backend. The backend decapsulates it, sees the original destination VIP, and can reply directly.

Tunnelling allows backends to live beyond the load balancer's local Layer 2 network, but introduces encapsulation overhead and maximum-transmission-unit considerations.

## The Mode Changes the Failure Model

In a full proxy, a proxy crash destroys its local sockets. In a NAT or packet-forwarding design, another instance might preserve a connection only if traffic reaches it and it has compatible mapping state or can reproduce selection statelessly. In DSR, response traffic may continue to bypass the failed director, but new inbound packets still require a working path to a director.

Forwarding mode is therefore not only a performance choice. It determines state ownership and failover semantics.

---

# 7. Scheduling Algorithms

## The Scheduler Sees a Partial World

Define one interface:

~~~cpp
class Scheduler {
public:
    virtual Backend* select(
        std::span<Backend* const> eligible,
        const FlowKey& flow) = 0;

    virtual ~Scheduler() = default;
};
~~~

Each scheduler differs in the information it uses and the state it must maintain.

## Round Robin

Round robin assumes eligible backends have similar capacity and work has similar cost:

~~~cpp
class RoundRobin final : public Scheduler {
public:
    Backend* select(
        std::span<Backend* const> backends,
        const FlowKey&) override {

        const uint64_t position =
            next_.fetch_add(1, std::memory_order_relaxed);
        return backends[position % backends.size()];
    }

private:
    std::atomic<uint64_t> next_{0};
};
~~~

It distributes selections, not resource consumption. Ten fast requests and ten large streaming requests are equal to the counter.

## Weighted Least Connections

If connection duration varies, current connection count contains useful load information:

~~~text
score_i = active_connections_i / weight_i
select the backend with the lowest score
~~~

A comparison can avoid floating point:

~~~cpp
Backend* leastConnections(
    std::span<Backend* const> backends) {

    return *std::min_element(
        backends.begin(),
        backends.end(),
        [](const Backend* a, const Backend* b) {
            const uint64_t a_connections =
                a->active_connections.load();
            const uint64_t b_connections =
                b->active_connections.load();

            return a_connections * b->weight <
                   b_connections * a->weight;
        });
}
~~~

The load signal is still incomplete. An idle WebSocket counts as one connection, as does a connection streaming hundreds of megabits. In an L7 proxy, outstanding requests, EWMA latency, or endpoint-reported utilization may be better signals.

## Power of Two Choices

Scanning every backend becomes expensive in a large or highly sharded pool. A useful compromise is:

~~~text
choose two eligible backends at random
-> compare their load
-> select the less loaded one
~~~

~~~cpp
Backend* powerOfTwo(
    std::span<Backend* const> backends,
    Random& random) {

    Backend* a = backends[random.index(backends.size())];
    Backend* b = backends[random.index(backends.size())];

    return normalizedLoad(*a) <= normalizedLoad(*b) ? a : b;
}
~~~

Two-choice selection uses bounded work while avoiding much of the skew of one random choice. The tradeoff is that each load-balancer instance sees only its local counters unless load is shared.

## Hashing and Bounded Disruption

Affinity can be expressed with rendezvous hashing:

~~~text
selected = arg max over b in eligible:
           hash(traffic_key, backend_id_b)
~~~

~~~cpp
Backend* rendezvous(
    std::string_view key,
    std::span<Backend* const> backends) {

    Backend* selected = nullptr;
    uint64_t best_score = 0;

    for (Backend* backend : backends) {
        const uint64_t score =
            stableHash(key, backend->stable_id);

        if (selected == nullptr || score > best_score) {
            selected = backend;
            best_score = score;
        }
    }
    return selected;
}
~~~

Unlike:

~~~text
hash(key) % backend_count
~~~

rendezvous hashing does not remap most keys merely because the backend count changes. When one of four equally weighted backends disappears, approximately the keys assigned to that backend need to move; keys owned by surviving backends normally stay in place.

<div>
    <center>{% include figure.html path="assets/img/load-balancers/scheduling.svg" alt="Flows distributed with round robin, least connections, and rendezvous hashing before and after a backend failure" caption="Algorithms optimize different properties: even selections, current load, or stable affinity under membership change." %}</center>
</div>

## There Is No Universally Best Algorithm

| Algorithm | State | Strength | Common failure mode |
|---|---|---|---|
| Round robin | Counter | Cheap, predictable distribution | Ignores work and connection duration |
| Least connections | Per-backend counters | Adapts to long-lived connections | Connection count is an imperfect load signal |
| Power of two | Counters for sampled endpoints | Good balance with bounded selection work | Local observations may be stale |
| Latency-aware | Rolling latency statistics | Reacts to observed service time | Feedback can oscillate or punish cold endpoints |
| Rendezvous hash | Stable backend identity | Affinity with bounded disruption | Hot keys remain hot |

**Operational consequence:** changing the algorithm changes which imbalance is tolerated; it does not eliminate imbalance.

---

# 8. L7 Reverse Proxying

## Terminating the Application Protocol

An L7 proxy understands the protocol above TCP:

~~~text
client TCP/TLS/HTTP
        terminates at proxy
proxy parses request
        selects route and backend
proxy uses or creates backend connection
~~~

The proxy can route:

~~~cpp
Route matchRoute(const HttpRequest& request) {
    if (request.host() == "api.example.com" &&
        request.path().starts_with("/payments/")) {
        return route_table.at("payments");
    }

    if (request.headers().contains("x-canary")) {
        return route_table.at("canary");
    }

    return route_table.at("default");
}
~~~

Production proxies use carefully validated HTTP implementations. The fragment illustrates where routing happens after parsing; it is not an invitation to implement an HTTP parser from string splitting.

## The Selection Unit Changes by Protocol

<div>
    <center>{% include figure.html path="assets/img/load-balancers/l4_l7_selection.svg" alt="Backend selection at connection level for L4, request level for HTTP 1.1, and stream level for HTTP 2" caption="L4 normally selects once per transport connection; L7 can select per request or concurrent protocol stream." %}</center>
</div>

For a generic L4 proxy:

~~~text
one accepted TCP connection -> one selected backend
~~~

For HTTP/1.1:

~~~text
one client connection
  request 1 -> backend A
  request 2 -> backend B
~~~

For HTTP/2:

~~~text
one client connection
  stream 1 -> backend A
  stream 3 -> backend B
  stream 5 -> backend A
~~~

For WebSocket, the L7 proxy parses the HTTP upgrade and may apply route or authentication policy. After a successful upgrade, the connection becomes a long-lived bidirectional stream attached to the selected backend.

## Backend Connection Pools

An L7 proxy does not normally create a new backend TCP connection for every request. It keeps pools keyed by backend and transport options:

~~~cpp
UpstreamConnection& acquire(const PoolKey& key) {
    if (auto* reusable = idle_connections.tryPop(key))
        return *reusable;

    if (open_connections[key] >= limits[key].maximum)
        throw UpstreamPoolSaturated{};

    return openConnection(key);
}
~~~

Pooling amortizes handshakes and TLS setup. It also means frontend and backend connection counts are not equal. HTTP/2 can multiplex many requests on one backend connection, while a large HTTP/1.1 workload may require many connections to avoid head-of-line waiting.

## TLS Termination

When the proxy terminates TLS, it owns certificate selection, protocol negotiation, cipher policy, handshake capacity, and client-certificate validation when mTLS is used.

The backend side may be plaintext on a trusted network or may use a second TLS connection:

~~~text
client == TLS A ==> proxy == TLS B ==> backend
~~~

These are independent security associations. A secure client-to-proxy connection says nothing by itself about proxy-to-backend encryption or backend identity.

## Preserving Client Identity

A full proxy's backend connection originates from the proxy, so the backend naturally sees the proxy address. Client identity can be forwarded at L7 through trusted headers or at connection setup through a protocol such as PROXY protocol.

The trust boundary is essential. A backend must not accept a client-supplied forwarding header as authoritative unless a trusted proxy strips or overwrites it.

---

# 9. Discovery and Backend-Pool Snapshots

## Where Backends Come From

Backend membership may come from static configuration, DNS results, an orchestrator API, a service registry, or a dynamic discovery stream.

Discovery answers “which endpoints are intended to exist?” Health checking answers “which known endpoints should receive traffic from this load balancer now?” They are related but not identical.

## Publish Complete Snapshots

The data plane should not observe half an update. One useful process-local model is an immutable snapshot:

~~~cpp
struct PoolSnapshot {
    uint64_t version;
    std::vector<std::shared_ptr<Backend>> eligible;
};

std::atomic<std::shared_ptr<const PoolSnapshot>> active_pool;

void publish(std::shared_ptr<const PoolSnapshot> next) {
    active_pool.store(
        std::move(next),
        std::memory_order_release);
}

std::shared_ptr<const PoolSnapshot> currentPool() {
    return active_pool.load(std::memory_order_acquire);
}
~~~

Readers either see the old complete version or the new complete version. Existing connections retain references to the backend they already selected; new selections use the latest snapshot.

<div>
    <center>{% include figure.html path="assets/img/load-balancers/control_data_plane.svg" alt="Service discovery, active health checks, passive errors, and administrative state producing a versioned backend snapshot for the data plane" caption="The control plane publishes eligibility snapshots; the forwarding path selects locally without synchronously querying discovery." %}</center>
</div>

## Eventual Convergence

Different load-balancer instances can briefly hold different pool versions:

~~~text
LB-A: pool version 104
LB-B: pool version 103
LB-C: pool version 104
~~~

This is normally acceptable if changes are monotonic enough, versions are observable, stale snapshots have bounded lifetimes, and active health checks can locally suppress a failed endpoint.

Requiring global consensus before every endpoint selection would put control-plane availability and latency into the request path. Most load balancers instead accept temporary membership disagreement and make selection locally.

---

# 10. Health Checking and Failure Detection

## Alive Is Not the Same as Useful

Health can be tested at several depths:

~~~text
ICMP responds
-> TCP port accepts
-> TLS handshake completes
-> HTTP endpoint returns success
-> application can reach critical dependencies
-> real requests complete within their deadlines
~~~

A deeper check gives stronger evidence but costs more and can couple the backend's health to dependencies. If every proxy probes an expensive database-dependent endpoint frequently, the health system can create meaningful production load.

## A State Machine, Not a Boolean

Use thresholds to prevent a single lost probe from flapping an endpoint:

~~~cpp
enum class HealthState {
    Healthy,
    Suspect,
    Unhealthy,
    Recovering,
    Draining
};

struct HealthTracker {
    HealthState state{HealthState::Healthy};
    uint32_t consecutive_failures{0};
    uint32_t consecutive_successes{0};
    TimePoint state_since;
};

void recordProbe(
    HealthTracker& tracker,
    bool succeeded,
    const HealthPolicy& policy) {

    if (succeeded) {
        tracker.consecutive_failures = 0;
        ++tracker.consecutive_successes;

        if (tracker.state == HealthState::Unhealthy &&
            tracker.consecutive_successes >=
                policy.successes_to_restore) {
            transition(tracker, HealthState::Recovering);
        }
        return;
    }

    tracker.consecutive_successes = 0;
    ++tracker.consecutive_failures;

    if (tracker.consecutive_failures >=
        policy.failures_to_eject) {
        transition(tracker, HealthState::Unhealthy);
    } else if (tracker.state == HealthState::Healthy) {
        transition(tracker, HealthState::Suspect);
    }
}
~~~

<div>
    <center>{% include figure.html path="assets/img/load-balancers/health_states.svg" alt="Backend health state machine from healthy through suspect, unhealthy, recovering, and draining" caption="Failure and recovery thresholds reduce flapping; slow start prevents a recovered backend from receiving its full share immediately." %}</center>
</div>

## Active and Passive Signals

**Active health checking** generates probes. It can detect a failed backend even when no user traffic is arriving.

**Passive health checking** observes real outcomes such as connect failures, resets, timeouts, or application errors. It sees the real traffic path but must distinguish backend failures from request-specific errors and local network problems.

Combining them is stronger than treating either as absolute truth:

~~~text
active probes -> baseline reachability
real traffic  -> path-specific failure evidence
discovery     -> intended membership
admin state   -> deliberate draining
~~~

## Slow Start

A backend recovering from failure may have cold caches, empty connection pools, just-in-time compilation, or delayed dependency initialization. Returning it immediately at full weight can make it fail again.

A slow-start policy increases effective weight over time:

~~~text
effective_weight =
    configured_weight * recovery_progress

recovery_progress in [0, 1]
~~~

## The All-Unhealthy Decision

If every backend is marked unhealthy, the proxy must choose a policy:

- fail closed and return an error,
- send traffic to a degraded or previously unhealthy subset,
- route to a lower-priority pool or region.

There is no universally safe answer. Sending to unhealthy backends may allow some successes, but it may also intensify a cascading failure. Failing quickly protects the backend but guarantees failure for that request.

**Tradeoff:** a health check is an input to availability policy, not proof of application correctness.

---

# 11. Affinity and Persistence

## Connection Affinity Is Automatic at L4

Once an L4 load balancer selects a backend for a TCP connection, connection correctness requires that selection to remain stable. This is not the same as application-session stickiness.

A browser may open several TCP connections, reconnect after an idle timeout, or use a different address. Those connections can reach different backends even though they belong to one logged-in user.

## Source-IP Hashing

Source-IP hashing is attractive because it requires no application cookie:

~~~text
backend = hash(client_ip) over eligible_backends
~~~

It performs poorly when:

- many clients share one NAT address,
- mobile clients change networks,
- IPv6 privacy addresses rotate,
- one customer's address range dominates traffic.

It should be treated as coarse affinity, not a durable session identity.

## Cookie or Key Affinity

An L7 proxy can assign a cookie or hash a stable application key:

~~~text
Set-Cookie: LB_AFFINITY=backend-17
~~~

The proxy must decide what happens when that backend drains or fails. Strong stickiness cannot override unavailability indefinitely.

Affinity is often a sign that backend-local state matters. Moving state to a shared store or encoding it in a client token can make the application tier easier to balance, but that introduces different consistency and dependency tradeoffs.

## Affinity Versus Balance

A celebrity account, popular cache key, or dominant tenant can hash to one backend. Hashing distributes keys; it does not distribute demand.

Useful mitigations include:

- more granular keys,
- bounded-load consistent hashing,
- replication of hot read-only state,
- per-tenant quotas,
- breaking affinity when an endpoint crosses an overload threshold.

---

# 12. Timeouts, Retries, and Ambiguous Outcomes

## A Timeout Is a Budget

Several different timers exist:

- client-to-proxy handshake timeout,
- backend connect timeout,
- TLS handshake timeout,
- time to first response byte,
- per-try timeout,
- overall request deadline,
- idle connection timeout,
- maximum stream duration.

These timers should form one budget. Three retries with a two-second timeout do not satisfy a three-second client deadline after two seconds have already been spent.

~~~cpp
Result forwardWithRetry(
    Request& request,
    Deadline deadline,
    RetryPolicy policy) {

    std::unordered_set<BackendId> attempted;

    for (uint32_t attempt = 0;
         attempt < policy.maximum_attempts;
         ++attempt) {

        if (deadline.expired())
            return Result::timeout();

        Backend& backend =
            selectExcluding(attempted, currentPool());
        attempted.insert(backend.id);

        Result result =
            sendAttempt(request, backend, deadline);

        if (result.ok() ||
            !policy.retryable(request, result)) {
            return result;
        }
    }

    return Result::attemptsExhausted();
}
~~~

## The Ambiguous Write

Consider:

~~~text
proxy sends POST /charge
-> backend commits charge
-> response is lost
-> proxy observes timeout
~~~

The proxy cannot infer from the missing response that the operation did not execute. Retrying may perform it twice. HTTP method semantics, request-specific idempotency keys, and knowledge of whether the body can be replayed all matter.

Automatic retries are safest when:

- the operation is idempotent,
- the body is buffered or otherwise replayable,
- the overall deadline has budget,
- a different healthy endpoint is available,
- the retry count is bounded.

## Retry Amplification

<div>
    <center>{% include figure.html path="assets/img/load-balancers/retry_amplification.svg" alt="A backend slowdown causing timeouts, retries, additional work, and a cascading feedback loop" caption="Retries consume capacity. A retry budget and admission control are required to keep recovery traffic from becoming the incident." %}</center>
</div>

The unstable loop is:

~~~text
backend slows
-> requests time out
-> proxy retries
-> backend fleet receives more work
-> queues grow
-> more requests time out
~~~

A retry budget limits retries to a fraction of original traffic. Jitter reduces synchronization. Circuit breakers prevent new attempts from accumulating behind a backend that has no remaining capacity.

**Operational consequence:** a retry is a new unit of offered load even when it is invisible to the original caller.

---

# 13. Draining and Topology Change

## Planned Removal

Deployment and maintenance should not look like crashes:

~~~text
mark backend draining
-> remove from new selections
-> preserve existing connections
-> wait for completion or deadline
-> close remaining connections
-> remove endpoint
~~~

The implementation separates eligibility for new work from ownership of old work:

~~~cpp
void beginDrain(Backend& backend) {
    backend.accepting_new_work.store(false);
    backend.drain_deadline = Clock::now() + drain_timeout;
    publishSnapshotWithout(backend.id);
}
~~~

Existing connection objects still hold their backend reference. Deleting the backend object as soon as it leaves discovery would create both correctness and memory-safety problems.

## Long-Lived Connections

Short HTTP requests may drain in seconds. WebSockets, database sessions, long polling, and streaming RPCs may remain open for hours.

A finite drain policy needs:

- a grace period,
- protocol-specific notification where possible,
- a hard deadline,
- reconnect backoff on the client side.

Without jitter, terminating thousands of connections together can create a reconnection storm against the remaining fleet.

## Unexpected Failure

When a backend crashes:

- new selections should stop,
- existing TCP connections usually reset or time out,
- safe L7 requests may be retried,
- arbitrary L4 byte streams cannot be migrated,
- health and discovery state converge afterward.

This is one of the most important boundaries in load balancing:

> Selecting a replacement backend for new work is much easier than preserving in-flight connection state.

---

# 14. Overload and Queueing

## A Load Balancer Is Also a Queue

If all backends are busy, the proxy can reject work or hold it. Holding it creates a queue even if the configuration never uses that word.

Little's Law relates average concurrency, arrival rate, and time in the system:

~~~text
L = lambda * W

L      = average in-flight work
lambda = average arrival rate
W      = average time in the system
~~~

At 20,000 requests per second and 50 milliseconds average latency:

~~~text
L = 20,000 * 0.050 = 1,000 in-flight requests
~~~

If latency rises to 500 milliseconds at the same arrival rate:

~~~text
L = 20,000 * 0.500 = 10,000 in-flight requests
~~~

The traffic rate did not increase, but concurrency, memory, connection-pool pressure, and timeout exposure increased tenfold.

## Bound the Waiting Room

Useful limits include:

- maximum accepted connections,
- maximum connections per backend,
- maximum pending requests,
- maximum buffered bytes per connection,
- maximum buffered bytes globally,
- maximum concurrent TLS handshakes,
- maximum requests per tenant,
- maximum retries.

When a limit is reached, failing quickly can be safer than accepting work that cannot finish before its deadline.

## Load Shedding

Admission control may reject low-priority work, traffic over a tenant quota, requests whose deadlines are already too short, or new connections during resource exhaustion.

This protects work already admitted. A proxy that accepts everything and completes nothing provides worse availability than one that deliberately rejects a bounded fraction.

## The Failover Capacity Trap

If a four-backend pool normally runs each backend at 80 percent utilization, losing one backend asks the remaining three to absorb:

~~~text
4 * 0.80 / 3 = 1.067
~~~

or roughly 107 percent of one backend's capacity. Health checking correctly removes the failed backend, but the resulting load can fail the rest.

High availability requires spare capacity, not only failure detection.

---

# 15. Making the Load-Balancer Tier Highly Available

## The Next Failure Domain

Once several backends sit behind one load balancer, that load balancer becomes the next obvious point of failure.

<div>
    <center>{% include figure.html path="assets/img/load-balancers/load_balancer_ha.svg" alt="Active passive virtual IP failover, active active ECMP distribution, and anycast regional load balancing" caption="Restoring a path to the VIP is different from preserving the connection state that existed on a failed load-balancer instance." %}</center>
</div>

## Active and Passive

In an active/passive design, one instance owns the VIP while another waits:

~~~text
LB-A owns VIP
LB-B standby

LB-A fails
-> LB-B claims VIP
-> neighbor or routing state converges
~~~

New connections can recover once the address is reachable. Existing connections survive only if the new instance has sufficient synchronized state and the network delivers subsequent packets consistently. A full userspace TCP proxy generally cannot reconstruct its missing kernel socket state from a simple connection-table copy.

## Active and Active

ECMP can distribute flows across several load-balancer instances. Routers commonly hash packet headers so packets for a flow converge on one next hop.

The system must plan for:

- membership changes altering ECMP paths,
- asymmetric routing,
- one instance losing local connection state,
- uneven flow sizes despite even flow counts,
- capacity after one instance disappears.

Stateless or consistent selection can reduce coordination, but stateful transport termination still creates local ownership.

## Anycast

With anycast, several sites advertise the same address. Routing chooses a nearby or policy-preferred site.

Anycast improves regional reachability and spreads attack traffic, but Internet routing is not an application-session protocol. A route change can move a client's new packets or new connection to another site. Application state and connection recovery must tolerate that.

## State Synchronization

Packet-forwarding systems may synchronize connection mappings between active and backup nodes. Synchronization itself has propagation latency, bandwidth and CPU cost, ordering questions, and incomplete state during sudden failure.

The guarantee should be stated precisely. “Highly available load balancer” may mean new connections recover quickly, not that every established TCP connection survives.

---

# 16. Multi-Core Performance

## Parallelism Starts at the NIC

Modern network interfaces expose multiple receive queues. Receive-side scaling hashes flows into queues so different CPUs can process different traffic while preserving per-flow locality.

A high-throughput proxy commonly aligns:

~~~text
NIC receive queue
-> CPU
-> event-loop shard
-> connection-table shard
-> backend connection ownership
~~~

This reduces cross-core locking and cache-line movement.

## Shard Mutable State

Instead of one global connection table:

~~~cpp
struct WorkerShard {
    EpollLoop loop;
    ConnectionMap connections;
    BackendCounters local_counters;
};
~~~

Each connection has one owner. Configuration snapshots can be shared immutably, while connection buffers and socket state remain local to the owning loop.

The tradeoff appears in scheduling. Local least-connection counters are cheap but incomplete. Globally synchronized counters are fresher but add contention. Approximate local information is often preferable on the data path.

## Different Performance Limits

A load balancer can saturate on:

- packets per second for small packets,
- bytes per second for large responses,
- new connections per second,
- concurrent connections,
- TLS handshakes per second,
- L7 parsing and policy evaluation,
- buffered bytes,
- logging or metrics cardinality.

“One million requests per second” says little without request size, protocol, connection reuse, TLS behavior, and latency distribution.

## Kernel and Kernel-Bypass Paths

Implementations may use ordinary sockets, kernel facilities such as IPVS or eBPF, or userspace packet frameworks. Moving work earlier in the packet path can reduce copies and per-packet overhead, but application-aware routing requires protocol state somewhere.

The design question remains the same at every performance level:

~~~text
what is the selection unit
where is its state stored
how is ownership preserved
what happens when that owner fails
~~~

---

# 17. Security Boundaries

## The Proxy Is Often the Trust Boundary

An Internet-facing load balancer may enforce:

- TLS policy,
- client certificate validation,
- request-size limits,
- connection and request rate limits,
- header normalization,
- protocol validation,
- access policy.

That makes configuration errors security relevant. A permissive route can expose an internal backend; trusting unsanitized forwarding headers can allow identity spoofing.

## Resource-Exhaustion Attacks

Attackers do not need high bandwidth if they can consume scarce state:

- incomplete TCP handshakes,
- slow TLS handshakes,
- slowly transmitted headers or bodies,
- many idle keep-alive connections,
- oversized headers,
- high-cardinality routing or logging values.

Defence requires limits at each stage rather than one global rate:

~~~text
handshake limit
-> established connection limit
-> header deadline and size
-> body limit
-> per-route concurrency
-> backend admission limit
~~~

## End-to-End Identity

When TLS terminates at the proxy, the backend authenticates the proxy-side connection, not the original transport peer. Client identity must be propagated through an authenticated mechanism and interpreted only from trusted proxies.

Re-encrypting to the backend and validating backend identity prevents the internal hop from silently becoming an unauthenticated plaintext boundary.

---

# 18. Observability and Capacity Planning

## Measure Each Stage

An end-to-end latency histogram is insufficient. At minimum, separate:

~~~text
queue time
backend connect time
backend TLS time
time to first byte
response transfer time
total proxy time
~~~

Important data-plane metrics include:

- accepted and active connections,
- new connections per second,
- packets and bytes per second,
- requests and responses by status,
- connection-table utilization,
- pending backend connects,
- pending requests and queue age,
- buffered bytes,
- retries and retry success,
- resets and timeouts,
- rejected or shed traffic,
- per-backend selection and completion rate,
- latency percentiles.

Important control-plane metrics include:

- discovery snapshot version and age,
- endpoint additions and removals,
- active health transitions,
- passive ejections,
- number and fraction of eligible endpoints,
- drain duration,
- configuration rejection or rollback.

## Avoid Aggregate Blindness

A pool can look healthy on average while one backend, tenant, route, worker shard, or availability zone is overloaded. Metrics need enough dimensions to locate skew, but unbounded labels can overload the monitoring system.

The most useful dashboards answer:

- Is the proxy saturated?
- Is the backend fleet saturated?
- Is one endpoint or route skewed?
- Are retries hiding original failures?
- Can the system survive losing one proxy and one backend now?

## Capacity for Failure

Capacity planning should include:

- the largest expected traffic burst,
- one load-balancer instance unavailable,
- one backend or availability zone unavailable,
- deployment overlap,
- health-check and retry traffic,
- TLS certificate rotation or cold caches,
- reconnection storms,
- observability overhead.

The target is not maximum benchmark throughput. It is predictable latency and recovery with useful headroom.

---

# 19. End-to-End Failure Example

Consider this regional path:

~~~text
client
  -> L4 fleet
  -> L7 proxy fleet
  -> payment backends B1, B2, B3
~~~

## Establishing the Connection

1. DNS returns the regional VIP.
2. An ECMP router hashes the client flow to L4 instance **L4-A**.
3. **L4-A** installs a five-tuple mapping to proxy **P2**.
4. **P2** accepts the TCP connection and completes TLS.
5. The client sends **GET /payments/42**.
6. **P2** matches the request to the payment route.
7. The current pool snapshot contains healthy backends **B1**, **B2**, and **B3**.
8. The scheduler selects **B2**.
9. **P2** acquires a pooled connection to **B2**, forwards the request, and returns the response.

The important state is distributed:

~~~text
router:        ECMP next-hop decision
L4-A:          flow -> P2 mapping
P2:            client TLS, route, deadline, retry state
P2 pool:       backend connections
control plane: endpoint and health snapshot
B2:            application execution state
~~~

## Backend Slowdown

Now **B2** develops a dependency problem:

1. Real requests to **B2** exceed their per-try deadline.
2. Passive outlier detection records locally observed timeouts.
3. A safe **GET** is retried once on **B3**, within the original deadline.
4. The retry budget prevents every failed request from producing a retry.
5. **B2** reaches the ejection threshold.
6. The control plane publishes a new local snapshot excluding **B2** from new selections.
7. Active probes also begin failing.
8. Metrics show the original timeout rate separately from successful retries.

The client may see a successful response, but the incident is not absent. The proxy spent extra capacity and latency to hide it.

## Recovery

After **B2** recovers:

1. It passes the required number of consecutive active probes.
2. It enters **Recovering**, not immediately **Healthy** at full weight.
3. Slow start raises its effective weight gradually.
4. Passive success and latency metrics confirm useful recovery.
5. It returns to normal eligibility.

## Proxy Failure

If **P2** fails during an established client connection:

- the client-side TCP/TLS state owned by **P2** is lost,
- a different L7 proxy can accept a new connection,
- the old connection cannot be recreated merely from route configuration,
- the client must reconnect and retry only operations whose semantics permit it.

If **L4-A** fails, routing can move new flows to another L4 instance. Existing flows survive only if the forwarding design preserves or reconstructs the required mapping state.

This is why failover claims must name the unit they preserve:

~~~text
VIP reachability
new connections
existing packet flows
terminated TCP connections
in-flight HTTP requests
application sessions
~~~

Those are six different guarantees.

---

# 20. Guarantees and Tradeoffs

## What a Load Balancer Can Provide

A well-designed load-balancing layer can provide:

- a stable service address over a changing backend fleet,
- local selection among eligible endpoints,
- connection or request distribution,
- failure isolation,
- graceful backend draining,
- admission control and load shedding,
- protocol termination and routing,
- observability at a shared boundary.

## What It Does Not Automatically Provide

A load balancer does not automatically provide:

- exactly-once request execution,
- preservation of arbitrary TCP connections after proxy failure,
- durable application sessions,
- correct application health,
- even resource consumption,
- capacity after a failure,
- safe retries for non-idempotent operations,
- globally synchronized topology views.

The load balancer can move traffic away from a failed server. It cannot create spare capacity, infer an application's transaction semantics, or preserve state it never owned.

## The Central Design Questions

For any load balancer, ask:

~~~text
1. What unit is selected: packet, flow, request, stream, or session?
2. What key identifies that unit?
3. Where is the selection state stored?
4. What makes a backend eligible?
5. What signal approximates backend load?
6. What happens to existing work when membership changes?
7. Which retries are semantically safe?
8. Which state survives a load-balancer failure?
9. Where is overload rejected?
10. How is every claim observed and tested?
~~~

These questions reveal more than the name of the scheduling algorithm.

---

# 21. Conclusion

A load balancer is not merely a round-robin loop in front of several servers. It is a stateful or deliberately stateless routing system operating under changing membership, incomplete health information, finite capacity, and ambiguous failures.

At Layer 4, the central abstraction is the flow:

~~~text
first packet or accepted connection
  -> select backend
  -> preserve mapping
  -> forward bidirectionally
  -> expire state safely
~~~

At Layer 7, the proxy adds protocol semantics:

~~~text
parse request or stream
  -> match route
  -> select eligible backend
  -> acquire upstream connection
  -> enforce deadline and policy
  -> observe outcome
~~~

The control plane keeps the eligible set current through discovery, health signals, and administrative state. The data plane consumes a local snapshot. Scheduling determines which imperfection the system prefers: simplicity, observed load, locality, or bounded disruption. Timeouts and retries determine whether failure is contained or amplified. Draining and failover determine whether topology change is graceful for new work, existing connections, or both.

The most important operational lesson is that availability requires more than redirecting traffic. The remaining proxies and backends need spare capacity, bounded queues, stable health policy, and clients that reconnect without creating a storm. A load balancer can make a fleet behave like one reachable service, but only when its state ownership and failure guarantees are understood precisely.

# References

- [RFC 9293: Transmission Control Protocol](https://www.rfc-editor.org/rfc/rfc9293)
- [RFC 9110: HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110)
- [Linux kernel documentation: IPVS sysctls](https://docs.kernel.org/networking/ipvs-sysctl.html)
- [Linux kernel documentation: Scaling in the Linux Networking Stack](https://docs.kernel.org/networking/scaling.html)
- [Envoy documentation: Life of a Request](https://www.envoyproxy.io/docs/envoy/latest/intro/life_of_a_request)
- [Envoy documentation: Service Discovery](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/service_discovery)
- [Envoy documentation: Health Checking](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/health_checking)
- [Envoy documentation: Outlier Detection](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/outlier)
- [HAProxy documentation: Backends and Load-Balancing Algorithms](https://www.haproxy.com/documentation/haproxy-configuration-tutorials/proxying-essentials/configuration-basics/backends/)
