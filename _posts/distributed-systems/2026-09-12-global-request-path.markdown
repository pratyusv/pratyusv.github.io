---
layout: single
comments: true
title: "Inside a Global Request Path: Edge, Backbone, Load Balancing, and Backend Routing"
date: 2026-09-12 00:00:00+0100
description: "A first-principles walkthrough of how a request moves from a client to a global edge, across private backbone infrastructure, through regional L4 and L7 load balancing, and finally to an application backend."
tags: [networking, load-balancing, edge-computing, http, distributed-systems, system-design]
categories: ['Distributed Systems Components']
---

## 1. Why the Request Path Matters

An application request often looks simple from the client:

```http
GET /payments/42 HTTP/1.1
Host: api.example.com
```

The client sends one request to one hostname and receives one response. The
system behind that hostname is usually many layers: edge locations, backbone
links, regional ingress, Layer 4 load balancers, Layer 7 proxies, backend
services, and databases.

The useful mental model is a sequence of selection decisions:

```text
client
    -> global routing chooses an edge or region
    -> edge proxy accepts and classifies the request
    -> private backbone carries dynamic traffic inward
    -> regional L4 load balancer chooses an L7 proxy
    -> L7 proxy matches the application route
    -> scheduler chooses an eligible backend
    -> backend executes the request
```

<div>
    <center>{% include figure.html path="assets/img/global-request-path/packets-flow.jpeg" alt="A packet flow from a client through edge routing, backbone transit, regional load balancing, proxies, and backend services" caption="A global request path is a sequence of scoped routing and forwarding decisions rather than one direct hop to an application server." %}</center>
</div>

Each step owns different state. Global routing owns reachability to an entry
point. The edge owns client-facing transport and cache policy. The L4 tier owns
flow-to-proxy assignment. The L7 proxy owns HTTP routing, security policy,
timeouts, retries, and backend selection. The backend owns business execution.

That separation matters during failures. Keeping the public address reachable
is not the same as preserving a TCP connection, an in-flight HTTP request, or a
payment workflow.

> **What to remember:** The request moves through routing, transport,
> protocol, and application decisions. No one layer owns the whole interaction.

---

## 2. The Simple Path and the Global Path

A single-region service has a shorter path:

```text
client -> regional load balancer -> application backend
```

<div>
    <center>{% include figure.html path="assets/img/global-request-path/simple-path.svg" alt="A simple single-region path from client to regional load balancer to application backend" caption="A single-region path has fewer routing decisions: the client reaches one regional entry point, which chooses an application backend." %}</center>
</div>

The client resolves `api.example.com`, opens a connection, and reaches
infrastructure in one region.

The model breaks down as the service becomes global. A user in Sydney should
not always cross the public Internet to a server in Virginia. Static assets
should be reusable near users. A regional outage should not make the only
service address disappear.

A global path therefore adds an edge layer:

```text
client
    -> nearby edge point of presence
    -> private or controlled transit
    -> regional application infrastructure
```

The edge is not the application backend. It is a geographically distributed
front door. It can terminate connections, enforce policies, serve cacheable
content, and forward dynamic requests inward.

For `GET /payments/42`, the response is usually dynamic. The edge classifies
the request and forwards it toward the payments service.

---

## 3. Global Routing: Reaching a Nearby Entry Point

The first decision is where the client enters the provider network.

A global service usually does not publish one address tied to one data center.
The same service name or address may be reachable from many locations. Two
common mechanisms are DNS-based steering and Anycast.

With DNS-based steering, the DNS answer can vary by requester location,
latency, health, or policy:

```text
api.example.com -> 198.51.100.10
```

With **Anycast**, multiple points of presence advertise the same IP prefix
using BGP, the Border Gateway Protocol. Internet routing carries the packet
toward one of those sites, often a nearby one.

```text
same VIP advertised in London
same VIP advertised in Singapore
same VIP advertised in Virginia
```

<div>
    <center>{% include figure.html path="assets/img/global-request-path/global-routing.svg" alt="DNS or Anycast selecting one nearby edge point of presence from several available edge locations" caption="Global routing gets the request to an entry point. It does not choose the final application backend." %}</center>
</div>

The **VIP**, or virtual IP, is the stable service address presented to clients.
It is not the permanent address of one application server. It represents an
entry point into a load-balancing or edge tier.

Global routing does not understand the application request. BGP does not know
that `/payments/42` is a payments API. DNS does not know whether backend `B2`
is overloaded. This layer moves traffic to an entry point; later layers perform
protocol and application routing.

> **What to remember:** Global routing chooses a network entry point, not the
> final application backend.

---

## 4. Edge Termination and Request Classification

After global routing, the request reaches an edge point of presence, or PoP.
The edge runs proxy software such as Envoy, NGINX, Apache Traffic Server,
HAProxy, or an internal equivalent. Some platforms also run small sandboxed
edge functions.

The edge commonly performs several jobs:

- accepting the client TCP or QUIC connection;
- terminating TLS when policy allows it;
- applying HTTP routing and security rules;
- checking local cache state for cacheable content;
- forwarding dynamic traffic toward regional infrastructure.

For a static object, the request may end at the edge:

```text
GET /assets/logo.png
    -> edge cache hit
    -> response returned from the PoP
```

For the payment request, the edge normally forwards the request:

```text
GET /payments/42
    -> dynamic API route
    -> forward toward payments region
```

<div>
    <center>{% include figure.html path="assets/img/global-request-path/edge-classification.svg" alt="An edge proxy classifying a request as either a cache hit or a dynamic route forwarded inward" caption="The edge is a decision point: cacheable content can stop here, while dynamic APIs continue inward." %}</center>
</div>

The edge follows configuration distributed by a control plane:

```yaml
virtual_host: api.example.com
routes:
  - match:
      prefix: "/assets/"
    action:
      type: "cache_lookup"
      ttl: "86400s"

  - match:
      prefix: "/payments/"
    action:
      type: "forward"
      upstream: "payments-regional-ingress"
```

The proxy matches the `Host` header and path prefix against this local
configuration. Cacheable routes can use local RAM or SSD storage. Dynamic
routes are forwarded to the configured upstream.

The internals can be complex, but the request-path role is simple: execute the
current policy snapshot quickly and safely.

> **What to remember:** The edge can answer cacheable requests locally.
> Dynamic requests continue toward regional services.

---

## 5. Backbone Transit: Moving Dynamic Traffic Inward

When the edge cannot complete the request locally, it forwards the request
toward the regional infrastructure that owns the service. Large providers often
use private backbone links or controlled interconnects for this leg.

The path may look like:

```text
client
    -> nearby edge PoP
    -> provider backbone
    -> regional data-center ingress
```

<div>
    <center>{% include figure.html path="assets/img/global-request-path/backbone-transit.svg" alt="Dynamic traffic moving from an edge point of presence across provider backbone transit to regional ingress" caption="Backbone transit carries the dynamic request from the edge toward the service region." %}</center>
</div>

This leg carries traffic toward regional ingress. The edge-to-region decision
can depend on geography, service ownership, capacity, compliance boundaries,
failure state, and control-plane policy.

---

## 6. Regional Ingress and Layer 4 Load Balancing

At regional ingress, traffic reaches a Layer 4 load-balancing tier. The L4 tier
works mostly with transport information: IP addresses, ports, and protocol. It
does not parse `/payments/42` or inspect application headers.

A common flow key is the five-tuple:

```text
(source IP, source port, destination IP, destination port, protocol)
```

The L4 balancer receives packets, but it does not make an independent
scheduling decision for every packet. It first derives a **flow key** from the
packet headers. For TCP, packets from the same connection usually have the same
five-tuple, so the balancer can treat them as one flow.

A stateful L4 balancer may keep a per-flow table:

```text
flow key                                      selected target
----------------------------------------------------------------
TCP 1.2.3.4:51000 -> 198.51.100.10:443        L7 proxy P2
TCP 1.2.3.4:51001 -> 198.51.100.10:443        L7 proxy P1
```

The first packet creates the mapping. Later packets with the same flow key
reuse it:

```text
packet arrives
    -> compute five-tuple
    -> existing flow mapping? yes -> reuse selected proxy
                              no  -> choose proxy and store mapping
```

Some L4 designs avoid a mutable table and use deterministic hashing instead:

```text
hash(five_tuple) -> L7 proxy P2
```

As long as the proxy set is stable, packets with the same five-tuple compute
the same target.

<div>
    <center>{% include figure.html path="assets/img/global-request-path/regional-l4.svg" alt="Regional Layer 4 load balancing using a flow key to select L7 proxy P2" caption="The L4 tier uses transport identity to keep one flow on a compatible proxy path." %}</center>
</div>

Consistent hashing or lookup-table designs limit movement when proxy membership
changes. If one proxy is removed, only part of the traffic should move.

The key rule is flow preservation:

```text
first packet of connection -> select P2
later packets              -> continue to P2
```

Round-robin per packet would break TCP. Packets from one connection could land
on different proxies, none of which has the full connection state. The
important rule is: packets arrive individually, but related packets are grouped
into a flow before choosing where they go.

This tier often sits after the edge and after backbone transit, at the door of
the regional data center:

```text
edge PoP
    -> backbone transit
    -> regional L4 load balancer
    -> regional L7 proxy fleet
```

The placement can vary by provider, but the role is stable: spread transport
flows across a proxy fleet while preserving flow state.

> **What to remember:** The regional L4 tier usually chooses a proxy, not the
> final application container.

---

## 7. Layer 7 Proxying and Application Routing (API Gateway)

The L7 proxy understands the application protocol. For HTTP, it can parse the
host, path, method, and headers. **L7 proxy is the API Gateway**

```http
GET /payments/42 HTTP/1.1
Host: api.example.com
Authorization: Bearer ...
```

This is where the request becomes an application route:

```text
host api.example.com
path /payments/42
    -> payments service
```

The L7 proxy can apply policies that an L4 balancer cannot see:

- route by hostname, path, method, or header;
- validate authentication metadata;
- enforce rate limits;
- apply request and response timeouts;
- attach tracing headers;
- choose retry behavior;
- select a backend pool for the matched service.

The proxy keeps a local, versioned snapshot of service discovery and
configuration. It does not ask a central database for every request:

```text
parse request
    -> match route
    -> read local pool snapshot
    -> choose eligible backend
```

For the payments route, the snapshot might contain:

```text
payments pool
    B1 = 10.0.1.11:8443  healthy, accepting new work
    B2 = 10.0.1.12:8443  healthy, accepting new work
    B3 = 10.0.1.13:8443  unhealthy
```

The proxy filters the pool before scheduling. A backend that is unhealthy,
draining, overloaded, or circuit-broken should not receive new work.

```text
known pool:     B1, B2, B3
eligible pool:  B1, B2
selected:       B2
```

Policy:

```yaml
virtual_host: api.example.com
routes:
  - match:
      path_prefix: "/payments/"
      methods: ["GET", "POST"]
    policy:
      require_auth: true
      rate_limit:
        key: "user_id"
        requests_per_minute: 120
      timeout_ms: 800
      retries:
        attempts: 1
        retry_on: ["connect-failure", "reset", "gateway-error"]
      circuit_breaker:
        max_pending_requests: 500
    upstream:
      cluster: "payments"
      load_balancing: "least_requests"
      locality: "same_zone_preferred"

  - match:
      path_prefix: "/assets/"
      methods: ["GET"]
    policy:
      require_auth: false
      cache:
        ttl_seconds: 86400
    upstream:
      cluster: "static-assets"
```

For `GET /payments/42`, the proxy can now make decisions that an L4 balancer
cannot make:

```text
Host header matches api.example.com
Path matches /payments/
Method is GET
Authentication is required
Rate limit key comes from the authenticated user
Timeout is 800 ms
Backend cluster is payments
Load-balancing policy is least requests
```

<div>
    <center>{% include figure.html path="assets/img/global-request-path/l7-routing.svg" alt="Layer 7 proxy parsing an HTTP request, matching the payments route, filtering backend health, and selecting backend B2" caption="The L7 proxy understands HTTP, so it can match routes and choose from an eligible backend pool." %}</center>
</div>

Scheduling policy may use round robin, weighted round robin, least requests,
power of two choices, locality, endpoint-reported load, or circuit breakers.

The proxy often uses a persistent backend connection pool:

```text
client connection -> L7 proxy -> reused connection to B2
```

Reusing warm backend connections avoids opening a new socket for every request.

---

## 8. Backend Execution and the Return Path

After selection, backend `B2` receives the request:

```text
GET /payments/42
    -> payment backend B2
```

The backend owns application execution. It parses the payment identifier, calls
databases or caches, emits logs and traces, and constructs the response.

The response returns through the path created by the request. A full L7 proxy
sees the response and sends it back on the client-facing connection. Some L4
designs let return traffic bypass part of the L4 tier, so the exact reverse
path depends on the forwarding mode.

Conceptually, the response follows the state that was established on the
request path:

```text
backend B2
    -> L7 proxy P2
    -> regional egress / backbone
    -> edge PoP
    -> client
```

<div>
    <center>{% include figure.html path="assets/img/global-request-path/return-path.svg" alt="A response returning from backend B2 through L7 proxy P2, edge point of presence, and back to the client" caption="The response follows state created during the request path instead of rediscovering the system from scratch." %}</center>
</div>

The response does not rediscover the system from scratch. It follows state
created while the request moved inward.

---

## 9. What Each Layer Knows

The request path is easier to reason about as a table of ownership:

| Layer | Main decision | State it owns |
|---|---|---|
| DNS or Anycast | Traffic entry point | Routing policy, BGP reachability, health of entry points |
| Edge proxy | Local response or forwarding decision | Client connection, TLS state, cache metadata, edge route config |
| Backbone transit | Regional ingress target | Internal routing policy and network reachability |
| L4 load balancer | L7 proxy for this flow | Flow mapping or deterministic hash state |
| L7 proxy | Service route and backend pool | HTTP parsing, route table, timeout, retry, auth, pool snapshot |
| Scheduler | Backend for this request | Eligibility, load signals, locality, weights, circuit breakers |
| Backend | Application operation | Business logic, local process state, database/cache calls |

No layer has perfect information. Global routing sees reachability, not
application load. The L4 tier sees transport identity, not HTTP paths. The L7
proxy sees application routing through a local snapshot. The backend sees the
business operation, not the whole global path.

---

## 10. Complete Walkthrough

For `GET /payments/42`, one possible path is:

1. The client resolves or routes to `api.example.com`.
2. DNS steering or Anycast brings the packet to a nearby edge PoP.
3. The edge accepts the client connection and terminates TLS when configured.
4. The edge matches `Host: api.example.com` and path `/payments/42`.
5. The request is classified as dynamic rather than cacheable.
6. The edge forwards the request across provider-controlled transit toward the
   payments region.
7. Regional ingress receives the traffic.
8. The L4 load-balancing tier hashes the flow to L7 proxy `P2`.
9. `P2` parses the HTTP request and matches the payments route.
10. `P2` reads its local payments pool snapshot.
11. `B3` is filtered out because it is unhealthy or ejected.
12. The scheduler chooses eligible backend `B2`.
13. `P2` forwards the request over a backend connection to `B2`.
14. `B2` executes the payment lookup and constructs the response.
15. The response returns through the proxy and edge path to the client.

The client experiences one HTTP request. Internally, the system answers a
series of smaller routing questions:

```text
global entry point
edge policy
region
L7 proxy
route
eligible backend
```

That is the core story of a global request path: scale comes from placing each
decision at the layer that has enough information to make it.

---

## 11. Summary

A global request path is a layered system. Global routing finds an entry point.
The edge applies cache and policy decisions. Regional L4 load balancing assigns
transport flows to proxies. L7 proxies parse the application request and select
a service route. Schedulers choose eligible backends. Backends execute the
business operation and return the response.

The central design principle is scoped responsibility: each layer should make
the decision it is equipped to make and preserve only the state required by
that decision.
