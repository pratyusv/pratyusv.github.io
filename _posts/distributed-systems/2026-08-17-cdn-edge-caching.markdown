---
layout: single
comments: true
title: "Inside a CDN: Cache Keys, Revalidation, Purging, and Origin Protection"
date: 2026-08-17 02:00:00+0100
description: "A connected journey from an origin server to a global CDN: regional delivery, video bandwidth, request steering, cache identity, freshness, purging, origin protection, and failure."
tags: [cdn, caching, edge-computing, http, networking, distributed-systems, system-design]
categories: ['Distributed Systems Components']
---

# 1. Before the CDN: One Server Is Far from Everyone

Suppose Atlas runs its news site in one US region. A London reader requests an
image, and the request follows this path:

~~~text
London reader
  -> public Internet
  -> Atlas load balancer in the US
  -> image service
  -> object storage
  -> the same long path back to London
~~~

The Atlas deployment is the **origin**: the authoritative system that stores or
generates the response. Calling it a "normal server" hides several components,
but the important property is simple: every request reaches infrastructure that
Atlas operates in its origin region.

That architecture can be perfectly reasonable for a small audience. The
problem appears when the same 2.4 MiB image becomes popular. An origin-side
cache can avoid repeatedly running the application or reading storage. It does
not shorten the geographic path: Atlas still terminates every connection and
transmits every copy through its origin network.

The problem is therefore not that the origin is a bad server. The problem is
that reusable bytes are repeatedly produced in one place and consumed all over
the world.

---

# 2. What a CDN Is—and What It Is Not

A **content delivery network**, or **CDN**, is a geographically distributed
fleet of reverse proxies and caches placed between readers and the origin.
Those servers live in **points of presence**, or **PoPs**, near concentrations
of users and well-connected networks. A server handling a reader at a PoP is
usually called an **edge server**.

When a request reaches an edge server, it can:

1. return a fresh stored response immediately;
2. ask another CDN cache, such as a regional shield;
3. forward the request to the origin and optionally store the response;
4. bypass caching entirely when the response is private or unsafe to reuse.

The origin and CDN have different jobs:

| Property | Origin server | CDN edge |
|---|---|---|
| Authority | owns or generates the canonical response | stores a reusable copy under origin policy |
| Placement | commonly one or a few application regions | many geographically distributed PoPs |
| Requests handled | cache misses, writes, private and dynamic work | mostly reads, TLS, routing, filtering, and cached responses |
| State | application data and durable objects | disposable cached representations and metadata |
| Failure meaning | content may be unavailable or impossible to regenerate | another edge can refill from a shield or origin |

![Origin-only delivery compared with edge delivery](/assets/img/cdn-edge/origin-vs-edge.svg)

A CDN does not normally replace the origin, and it does not make every response
cacheable. It is a distributed delivery layer in front of the origin. Its
central question is: **may this stored response be safely reused for this
request, here and now?**

---

# 3. The First Request Fills the Cache; Later Requests Reuse It

Most CDNs use **pull caching**. Atlas publishes the image at its origin and
points `media.atlas.example` at the CDN, but it does not need to upload a copy
to every PoP.

The first London request is a **cold miss**:

1. the London edge has no reusable entry;
2. it forwards the request upstream;
3. the origin returns the image and cache policy;
4. the edge streams the response to the reader while storing a copy.

The next London request is a **warm hit**:

1. the edge finds the stored representation;
2. its policy says the entry is still fresh;
3. the edge returns it without contacting the origin.

![A pull CDN learns on the first request and reuses on later requests](/assets/img/cdn-edge/pull-cache-lifecycle.svg)

This first fill is sometimes loosely called **seeding**, but seeding is not a
required separate step for a pull CDN. Some operators deliberately prewarm or
push important objects before a launch. Prewarming reduces first-reader latency
but costs bandwidth and storage even in PoPs where nobody requests the object.
Pull caching pays that cost only where demand appears.

A cache hit is not permanent. The entry can expire, be evicted, or be purged.
The CDN must then revalidate it or fetch it again. The rest of this article
explains how it makes those decisions safely.

---

# 4. Why Serving from the CDN Helps

The origin web server can absolutely cache the response. Atlas could keep the
image in process memory or put Nginx, Varnish, or another reverse-proxy cache in
front of the image service:

~~~text
London reader -> long Internet path -> US origin cache -> RAM hit
~~~

That is valuable. It avoids application computation and storage I/O. But a
cache hit does not mean that no network work occurs. Every London request still
crosses the Atlantic, reaches Atlas's load balancer and cache, occupies a
connection, and makes the US origin network transmit another 2.4 MiB response.

The location of the cache determines which costs it removes:

| Architecture | Application and storage work | Traffic leaving the US origin | Path for a London reader |
|---|---|---|---|
| uncached origin | potentially repeated for every request | every response | London to US |
| cache inside the origin | mostly avoided on hits | every response | London to US |
| cache at a London CDN edge | avoided on hits | cache fills and revalidations only | London to London edge |

![An application cache, an origin cache, and a CDN cache remove different costs](/assets/img/cdn-edge/cache-location-comparison.svg)

The key distinction is:

> An origin cache saves computation. A CDN cache can save computation, origin
> bandwidth, and geographic distance.

Atlas could deploy its own cached web servers in London, Mumbai, Tokyo, and
Sydney. Once it adds global request steering, cache filling, expiration,
purging, capacity management, and failover across those sites, however, it is
building the machinery of a CDN.

Use the Atlas hero image to make the benefit concrete. It is 2.4 MiB, and
100,000 readers near London request the same version.

Without a CDN, the origin sends approximately:

~~~text
100,000 readers * 2.4 MiB = 240,000 MiB ~= 234 GiB
~~~

In a simplified warm-cache case with one London edge fill, the origin sends one
2.4 MiB copy upstream of the CDN. The edge supplies the remaining copies. A
real deployment has several cache nodes and PoPs, evictions, ranges, misses,
and revalidations, so the result will not be literally one origin fetch—but the
reuse principle is the same.

![One origin copy can become many nearby edge responses](/assets/img/cdn-edge/cdn-benefit-example.svg)

The CDN changes four things:

- **latency:** a warm response traverses the client-to-edge path instead of the
  client-to-origin path;
- **origin load:** cache hits avoid repeated application work, storage reads,
  connections, and response bytes at the origin;
- **burst capacity:** already cached objects can be served by many PoPs rather
  than forcing one application region to absorb the entire spike;
- **failure tolerance:** bounded stale content can sometimes remain available
  while the origin is slow or unreachable.

The latency comparison is structural rather than magical:

~~~text
origin-only ~= client-origin network + origin work + transfer
warm CDN    ~= client-edge network  + cache lookup + transfer
~~~

A CDN shifts work; it does not eliminate it. Edge delivery costs money, cold
misses still reach the origin, and an incorrect cache policy can serve stale or
private data at global speed. Whether it helps depends on reuse.

---

# 5. What Should—and Should Not—Go Through a CDN

The strongest candidates are responses that many readers can safely share:

| Workload | CDN value | Reason |
|---|---|---|
| versioned CSS, JavaScript, fonts, and images | very high | immutable and repeatedly reused |
| video and software downloads | very high | large responses make byte offload valuable |
| public articles and product data | high when freshness policy is explicit | repeated reads can share one representation |
| personalized account pages | usually not cacheable as shared content | one user's response must not reach another user |
| writes and mutations | no cache reuse | the origin still performs authoritative state changes |
| rare or nearly unique URLs | limited | misses and storage may cost more than reuse saves |

A CDN can still accelerate uncacheable traffic by terminating connections near
users, filtering attacks, compressing responses, or carrying traffic over an
optimized backbone. Those are useful edge-proxy functions, but they do not
provide the origin offload of a cache hit.

This distinction matters throughout the article: **passing through a CDN is
not the same as being served from a CDN cache.**

---

# 6. Regional Web Servers Are Ingredients, Not the Whole CDN

At the level of one machine, an edge cache is a specialized web server. It
accepts HTTP requests, terminates TLS, reads bytes from memory or disk, and
writes responses to network sockets. There is no mysterious alternative to a
web server hidden inside a CDN.

The distinction appears at the system level. One cached server in London is a
regional web server. A CDN is the machinery that turns servers in London,
Mumbai, Tokyo, and hundreds of other network locations into one delivery
service:

| One regional web server | CDN fleet |
|---|---|
| one known location | many PoPs selected per request |
| capacity planned for that server or region | load distributed across machines, PoPs, and providers |
| ordinary path through the Internet | direct peering, private interconnects, and sometimes caches inside ISP networks |
| operator manually chooses what it stores | fill, admission, prepositioning, eviction, and purge policies |
| local health check and failover | global traffic steering around machines, PoPs, and network failures |
| configuration belongs to one deployment | configuration and security policy propagated across the fleet |

![A CDN coordinates many delivery servers as one system](/assets/img/cdn-edge/regional-server-vs-cdn.svg)

It helps to separate two planes.

The **data plane** handles each request. It terminates the connection, applies
security policy, computes the cache key, returns a hit, or forwards a miss. It
must be extremely fast because every delivered byte passes through it.

The **control plane** decides how the fleet should behave. It distributes
tenant configuration and certificates, maps users to healthy capacity,
prepositions selected objects, propagates purges, removes unhealthy nodes, and
collects measurements that influence later routing decisions.

DNS, Anycast, BGP, and direct peering connect those planes to the Internet. A
PoP is not merely "a server in the same cloud region." It may be a metro cache,
an Internet exchange deployment, or an appliance embedded inside the reader's
ISP. **Network distance**—the links and providers traffic must cross—can matter
as much as geographic distance.

![The control plane coordinates a high-volume CDN data plane](/assets/img/cdn-edge/cdn-control-data-plane.svg)

Atlas could build all of this itself. But calling the result "our regional web
servers" would not change the architecture: once those servers share global
routing, content placement, invalidation, observability, capacity, and failure
management, they collectively form a private CDN.

---

# 7. Video Shows Why the Fleet Matters

Video turns the bandwidth argument into the dominant constraint. Suppose one
million viewers each consume a 5 Mbit/s stream:

~~~text
1,000,000 viewers * 5 Mbit/s = 5,000,000 Mbit/s = 5 Tbit/s
~~~

An origin-side RAM cache can ensure that encoding, application logic, and disk
reads are not repeated for every viewer. It cannot make those response bytes
disappear. The origin network would still need to sustain roughly 5 Tbit/s, and
all of it would traverse long-haul links toward access networks.

A CDN does not reduce the final 5 Tbit/s delivered to viewers either. Instead,
it distributes that traffic across many edge locations. Each location receives
or prepositions a smaller set of video objects and then sends those bytes over
short local paths to nearby viewers. One upstream copy of a popular segment can
support many downstream plays at that location.

![Video delivery moves fan-out from one origin to many edge networks](/assets/img/cdn-edge/video-bandwidth-fanout.svg)

That difference improves more than page-load latency:

- aggregate serving capacity becomes the sum of many PoPs rather than one
  origin region;
- fewer long-haul and transit links carry every repeated video byte;
- playback is less exposed to distant congestion, loss, and route changes;
- nearby capacity improves startup time, sustained throughput, and seeking;
- traffic can move to another appliance or PoP when one path is overloaded;
- providers can fill caches before the evening viewing peak.

The application and delivery paths also separate. Application servers still
authenticate the viewer, show the catalogue, apply entitlement policy, and
select a manifest. The high-volume data path then fetches video segments from a
nearby CDN server:

~~~text
small control path: player -> application API -> authorization + manifest
large data path:    player -> nearby CDN edge -> video segments
~~~

Netflix's [Open Connect](https://openconnect.netflix.com/en/) makes this split
concrete. Netflix can place Open Connect Appliances inside participating ISP
networks or deliver through direct interconnection. Its system uses proactive,
directed content placement so large objects can move during quieter periods and
be served locally during peak viewing, rather than making the first prime-time
viewer pull everything across the long-haul path.

Google similarly describes its media-delivery service as using
[YouTube's infrastructure](https://cloud.google.com/cdn) to bring live and
on-demand streams closer to viewers. The individual machines remain HTTP
servers; the advantage comes from the global edge network, placement and
routing systems, enormous aggregate bandwidth, and direct proximity to access
networks.

This yields the durable mental model:

> The origin decides what content exists and who may receive it. The CDN decides
> which safe nearby copy should deliver the bytes.

---

# 8. One Image's Journey Through the CDN

With the basic model established, follow one object through the complete
distributed system. Atlas publishes a live story about a solar eclipse. Its
page references a content-negotiated hero image:

~~~text
https://media.atlas.example/eclipse/hero?w=1200
~~~

Reader `R1` opens the page in London. The browser can display AVIF, so it sends:

~~~http
GET /eclipse/hero?w=1200 HTTP/1.1
Host: media.atlas.example
Accept: image/avif,image/webp,image/*;q=0.8
~~~

The origin has produced representation `hero-v7-avif`, 2.4 MiB in size, with
this policy:

~~~http
HTTP/1.1 200 OK
Content-Type: image/avif
Content-Length: 2516582
ETag: "hero-v7-avif"
Vary: Accept
Cache-Control: public, max-age=60, s-maxage=600, stale-while-revalidate=30, stale-if-error=86400
~~~

That one request will connect the entire CDN:

1. DNS and Internet routing steer `R1` toward a London PoP;
2. TLS and HTTP routing select the Atlas tenant;
3. request normalization creates the correct cache key and AVIF variant;
4. consistent hashing chooses an edge cache node;
5. edge and regional shield misses cause one origin fetch;
6. request collapsing makes thousands of waiting requests share that fetch;
7. the response is admitted into shield, disk, and memory caches;
8. fresh hits return bytes without contacting the origin;
9. an expired entry is conditionally revalidated with its `ETag`;
10. stale-while-revalidate hides revalidation latency;
11. stale-if-error protects readers during an origin failure;
12. an editorial correction triggers a globally propagated purge;
13. a failed edge node and then a failed region move traffic safely elsewhere.

![The Atlas hero image's complete CDN setting](/assets/img/cdn-edge/story-overview.svg)

A CDN is more than "servers closer to users." It is a distributed reverse
proxy whose routing, identity, time, storage, concurrency, invalidation, and
failure decisions must all agree on what an object means. The first decision
begins before HTTP, in name resolution and routing.

---

# 9. Steering a Reader to a Point of Presence

CDNs commonly combine two mechanisms rather than relying on one.

## DNS Steering

The authoritative DNS system can select an answer using resolver location,
measured latency, PoP health, capacity, and policy. A CNAME might lead from the
customer hostname into a CDN-controlled name whose answer changes over time.

DNS decisions are cached by recursive resolvers and clients. A low TTL enables
faster redirection but increases DNS load; a high TTL lowers lookup cost but
makes failures and traffic shifts react more slowly. The resolver's network
location may also differ from the end user's location.

## Anycast Routing

With Anycast, several PoPs advertise reachability to the same service address.
The Internet routing system selects a reachable route. "Nearest" means preferred
by routing policy and topology—not necessarily the geographically closest or
lowest-latency building.

![DNS policy selects an address while Anycast routing selects a PoP](/assets/img/cdn-edge/global-routing.svg)

For `R1`, DNS returns `203.0.113.80`, an address advertised from London,
Amsterdam, and Paris. Current BGP policy sends the connection to London.

If London withdraws the route, later connections can reach another PoP. An
existing TCP or QUIC flow might not migrate cleanly when routing changes; the
client often reconnects. Anycast improves reachability, but it is not an
application-level session migration protocol.

![Anycast route withdrawal moves new traffic to another PoP](/assets/img/cdn-edge/anycast-failover.svg)

The route has found a building. The CDN must now determine which customer and
which policy owns the request.

---

# 10. The Edge Ingress Pipeline Establishes Context

At the London PoP, the connection passes through several logical stages:

1. a network load balancer selects an ingress proxy;
2. TLS Server Name Indication selects a certificate and tenant configuration;
3. the HTTP authority (`Host` or `:authority`) is checked against that tenant;
4. security policy applies rate limits, bot rules, and a web application
   firewall where configured;
5. request normalization creates a canonical internal request;
6. cache policy decides whether the request is eligible for lookup;
7. the cache key chooses a stored representation.

![TLS, tenant routing, security, normalization, and cache lookup](/assets/img/cdn-edge/ingress-pipeline.svg)

The order is security-sensitive. The CDN must not select an Atlas cache entry
using one interpretation of the path while the origin interprets it another
way. Ambiguities involving duplicate headers, percent encoding, dot segments,
or conflicting authority fields should be rejected or canonicalized exactly
once before key construction and origin forwarding.

Multi-tenant isolation also begins here. The hostname or an internal tenant ID
must participate in routing and keying. Otherwise two customers with a path
named `/logo.svg` could collide.

---

# 11. The Cache Key Defines Object Identity

An edge cache does not ask whether it has "the URL" in an informal sense. It
computes an exact key. A representative key for the Atlas request is:

~~~text
tenant       = atlas
scheme       = https
authority    = media.atlas.example
path         = /eclipse/hero
query        = w=1200
method       = GET
variant      = accept-format:avif
~~~

![The request fields included in and excluded from the cache key](/assets/img/cdn-edge/cache-key.svg)

HTTP caches typically use the target URI and method as the primary key, then
use `Vary` to distinguish representations selected by request headers. CDN
products often add controlled normalization or tenant configuration.

Every choice trades correctness against hit ratio:

- omit `w=1200`, and different image sizes can collide;
- preserve every tracking parameter, and semantically identical URLs fragment
  the cache;
- omit the Atlas tenant, and customers can collide;
- include a unique request ID, and every request becomes a miss;
- ignore `Accept`, and an AVIF response might be sent to a client that cannot
  decode it.

Query normalization should be an allowlist with known semantics, not a blanket
"sort and remove anything unfamiliar" rule. A signature, authorization token,
or origin-routing parameter can be security-critical even if it looks like
noise.

---

# 12. `Vary` Creates Representation Variants

The origin returns:

~~~http
Vary: Accept
~~~

The edge may therefore store several responses under the same primary URI:

~~~text
Accept selects AVIF -> hero-v7-avif
Accept selects WebP -> hero-v7-webp
Accept selects JPEG -> hero-v7-jpeg
~~~

![One primary URI with three Vary-selected representations](/assets/img/cdn-edge/vary-variants.svg)

When another request arrives, the cache compares the request fields named by
the stored response's `Vary` value. Only a matching variant is reusable.
`Vary: *` never matches a later request.

Unbounded variation is dangerous. `Vary: User-Agent` can create thousands of
nearly unique variants. A CDN often maps raw request headers into a small,
explicit device or format class, while ensuring the origin uses the same
classification.

## Private and Authorized Responses

Shared caches must treat personalized data conservatively:

- `Cache-Control: private` prevents storage by a shared cache;
- `no-store` says not to store the response;
- `no-cache` allows storage but requires validation before reuse;
- requests with `Authorization` are not normally reusable by a shared cache
  unless response directives explicitly permit it.

Cookies should not automatically become part of a giant key. Instead, policy
should bypass shared caching for personalized routes or extract only a bounded,
audited variant such as `country=GB`.

The key identifies the object. The PoP still needs to select which cache machine
should own it.

---

# 13. Consistent Hashing Places the Object Inside a PoP

The London PoP has cache nodes `E1` through `E8`. Sending every request to a
random node would duplicate popular objects and reduce memory locality. A
dispatcher instead hashes the cache key onto a node or a small replica set.

Consistent or rendezvous hashing limits movement when membership changes. If
`E3` fails, only keys previously assigned to `E3` move, instead of nearly every
key being remapped by `hash(key) mod 7`.

![Consistent hashing assigns the Atlas representation to edge node E3](/assets/img/cdn-edge/consistent-hashing.svg)

The mapping is an optimization, not the source of truth. A lost node loses
replaceable cached bytes. The next owner refills them from the shield or origin.

Hot objects can exceed one node's network capacity even when storage is
balanced. A CDN can replicate a hot key, use two-choice routing, serve it from
per-process memory on several workers, or temporarily bypass strict ownership.
Object placement must consider traffic, not only bytes.

For our story, the key hashes to `E3`. Its memory and disk caches are both cold.

---

# 14. A Miss Walks the Cache Hierarchy

A CDN usually has more than one cache layer:

1. **process memory** holds the hottest small metadata and objects;
2. **PoP disk or shared cache** holds a larger working set;
3. a **regional shield** aggregates misses from many edge PoPs;
4. the **origin** generates or reads the authoritative response.

![Memory, PoP storage, regional shield, and origin hierarchy](/assets/img/cdn-edge/cache-hierarchy.svg)

`E3` misses in memory and disk. It forwards the canonical request to shield
`S-LON`. The shield also misses and requests the image from Atlas's origin.
The origin returns `hero-v7-avif` with its validators and cache policy.

The response fills outward:

~~~text
origin 200
  -> shield stores object
  -> E3 disk stores object
  -> E3 memory may admit hot object
  -> R1 receives bytes as they stream
~~~

![A cold miss fills the hierarchy while streaming to the reader](/assets/img/cdn-edge/cold-fill.svg)

The full 2.4 MiB need not arrive before downstream transmission begins. The
cache can stream chunks while writing the object, provided it publishes the
entry as reusable only after headers, length, integrity, and storage completion
are valid.

One miss is harmless. Ten thousand simultaneous misses for a newly published
object can become an origin outage.

---

# 15. Request Collapsing Prevents a Cache Stampede

At publication time, 10,000 readers request the hero image before `E3` finishes
its first fill. A naive cache forwards all 10,000 misses. The origin then does
10,000 times the work precisely when the object is hottest.

With **request collapsing**, the first miss becomes the leader for that cache
key. Later misses join a waiter set:

~~~text
1 leader -> shield/origin fetch
9,999 followers -> wait for leader result
~~~

![Many simultaneous misses collapse behind one origin fetch](/assets/img/cdn-edge/request-collapsing.svg)

When headers arrive, the cache must decide whether the result is reusable for
every waiter. Requests with different range, authorization, variant, or policy
cannot be collapsed merely because their paths look similar.

Collapsing also needs failure controls:

- a bounded wait deadline;
- leader cancellation that does not strand followers;
- retry limits and jitter;
- maximum waiter count and memory;
- correct propagation of non-cacheable or error responses;
- a purge generation check before publishing the completed fill.

The shield performs the same aggregation across PoPs. Edge collapsing protects
the shield; shield collapsing protects the origin.

---

# 16. Freshness Is a Time Calculation, Not Presence

Having bytes on disk does not make them reusable. A cache compares the stored
response's **current age** with its **freshness lifetime**:

~~~text
response_is_fresh = freshness_lifetime > current_age
~~~

For a shared CDN cache, `s-maxage=600` takes precedence over `max-age=60`.
The browser's freshness lifetime is 60 seconds, while the CDN's is 600 seconds.
Both compare that lifetime with inherited current age; a browser receiving the
response with `Age: 420` considers it stale immediately rather than receiving a
new 60-second window.

![Fresh, stale-while-revalidate, and stale-if-error time windows](/assets/img/cdn-edge/freshness-timeline.svg)

`Age` communicates a cache's estimate of seconds since the response was
generated or last validated by the origin. The calculation accounts for the
incoming `Age`, apparent origin age, request/response transit time, and resident
time. A downstream cache must not reset age to zero simply because it received
the object from an upstream cache.

![Age accumulates through origin, shield, edge, and browser](/assets/img/cdn-edge/age-propagation.svg)

At age 420 seconds, `E3` serves the hero as a fresh hit:

~~~http
HTTP/1.1 200 OK
Age: 420
ETag: "hero-v7-avif"
Cache-Control: public, max-age=60, s-maxage=600, stale-while-revalidate=30, stale-if-error=86400
Cache-Status: AtlasEdge; hit; ttl=180
~~~

No origin request occurs. At age 601, the stored response becomes stale. Stale
does not mean corrupt or deleted; it means the cache needs permission or
validation before ordinary reuse.

---

# 17. Revalidation Refreshes Metadata Without Resending the Body

When the object is stale, the cache can send a conditional request using its
validator:

~~~http
GET /eclipse/hero?w=1200 HTTP/1.1
Host: media.atlas.example
Accept: image/avif,image/webp,image/*;q=0.8
If-None-Match: "hero-v7-avif"
~~~

If the representation is unchanged, the origin returns:

~~~http
HTTP/1.1 304 Not Modified
ETag: "hero-v7-avif"
Cache-Control: public, max-age=60, s-maxage=600, stale-while-revalidate=30, stale-if-error=86400
~~~

The `304` has no image body. The cache updates stored response metadata, resets
the validated age calculation, and reuses the existing 2.4 MiB body.

![Conditional revalidation returns either 304 metadata or a new 200 body](/assets/img/cdn-edge/revalidation.svg)

If the origin has `hero-v8-avif`, it returns `200 OK` with the new body and
`ETag: "hero-v8-avif"`. The cache stores the new representation before making
it the reusable entry.

Strong entity tags identify byte-equivalent representations and are required
for some operations such as safely combining ranges. Weak validators can be
useful for semantic equivalence but do not prove byte-for-byte identity.

Revalidation saves bandwidth, but a synchronous revalidation still adds origin
latency to the unlucky request that discovers staleness.

---

# 18. Stale-While-Revalidate Hides Refresh Latency

The Atlas response allows 30 seconds of `stale-while-revalidate`. At age 601,
`E3` can immediately serve the stale `v7` body and launch one background
revalidation. Other requests continue receiving the usable stored body while
request collapsing prevents duplicate refreshes.

![One stale response is served while one background request revalidates](/assets/img/cdn-edge/stale-while-revalidate.svg)

If the origin replies `304`, the entry becomes fresh again. If it returns `v8`,
later readers receive the new body. The first readers intentionally traded up
to 30 seconds of additional staleness for lower latency and a protected origin.

This is a product decision, not a universally safe default. A CSS file with a
content hash can tolerate long caching. A rapidly changing emergency banner,
price, authorization decision, or account balance may require synchronous
validation or no shared caching at all.

The stale window should be interpreted with purge requirements. If an emergency
purge marks `v7` forbidden, the edge must not continue serving it merely because
the HTTP stale window would otherwise allow it.

---

# 19. Stale-If-Error Turns Cached Bytes into a Failure Buffer

Later, Atlas's origin region returns `503 Service Unavailable`. The response
allows `stale-if-error=86400`, so a cache with a previously valid copy may serve
it during the configured stale window instead of forwarding the failure.

![Origin failure falls back to a bounded stale response](/assets/img/cdn-edge/stale-if-error.svg)

This mechanism converts a cache from a performance optimization into a
resilience layer. It works only if:

- a usable response was cached before the outage;
- policy permits serving it stale;
- the object is not invalidated for correctness or safety reasons;
- the cache can distinguish eligible origin failure from an application result
  that should reach the user.

Serving stale forever conceals prolonged failure and unboundedly violates
freshness expectations. The CDN should attach warning/observability metadata,
track stale age, and stop at the configured boundary.

---

# 20. Expiration and Eviction Are Different Events

An entry can be:

- **fresh and resident**: reusable immediately;
- **stale and resident**: bytes exist but need validation or stale permission;
- **fresh but evicted**: policy would allow reuse, but local storage removed it;
- **expired and evicted**: neither bytes nor freshness remain locally.

![Freshness state and storage residency are independent axes](/assets/img/cdn-edge/expiration-vs-eviction.svg)

TTL controls reuse semantics. Eviction controls finite storage. LRU-like
recency, frequency, object size, fetch cost, tenant quota, and admission policy
may all affect which entries remain.

Caching every one-hit object can evict a small hot working set. A CDN may admit
an object to disk but require repeated access before promoting it to scarce
memory. Oversized objects may stream through without full caching, or use
segmented storage.

Eviction is not invalidation. Removing London's local copy does not remove
Amsterdam's copy, and it does not tell the origin that the representation is
wrong.

---

# 21. Negative Caching Protects Missing and Failing Resources

Suppose a page accidentally references `/eclipse/caption.vtt` before the file
exists. Without negative caching, every reader repeats the same origin `404`.
A short negative TTL can collapse that repeated failure:

~~~http
HTTP/1.1 404 Not Found
Cache-Control: public, s-maxage=15
~~~

![A short-lived cached 404 protects the origin without hiding recovery for long](/assets/img/cdn-edge/negative-caching.svg)

Negative caching needs tighter policy than successful content:

- cache only understood status codes;
- use short explicit TTLs;
- include authorization and variant boundaries correctly;
- avoid caching transient failures for longer than recovery objectives;
- allow urgent purge when the missing object is created.

A cached `404` for the wrong key is particularly damaging: it turns one routing
or authorization mistake into a distributed outage. Error responses must pass
the same identity and tenant checks as successful responses.

---

# 22. Range Requests Complicate Object Identity

Large video and software files are often requested in byte ranges:

~~~http
GET /eclipse/film.mp4 HTTP/1.1
Range: bytes=1048576-2097151
~~~

A successful response describes the selected bytes:

~~~http
HTTP/1.1 206 Partial Content
Content-Range: bytes 1048576-2097151/503316480
ETag: "film-v3"
~~~

![A large object is cached and served as validated byte segments](/assets/img/cdn-edge/range-requests.svg)

The CDN can cache the full object, cache independent segments, or fetch a larger
internal slice than the client requested. It must never combine byte ranges
from different object versions. Combining partial responses safely requires a
shared strong validator and consistent total length.

Range fragmentation creates its own stampede: many readers begin at offset
zero, while seek traffic creates a wide segment working set. Segment-size and
prefetch decisions balance hit ratio, memory, disk I/O, and wasted origin bytes.

---

# 23. Origin Shielding Aggregates the Global Miss Stream

Without a shield, 200 PoPs can each send one collapsed miss to the origin. That
is much better than millions of reader requests, but a globally viral object
still creates 200 origin reads.

A shield is a cache layer near the origin or at a well-connected regional hub:

~~~text
many readers -> many edge PoPs -> few shields -> origin
~~~

![Origin shielding collapses misses across multiple edge PoPs](/assets/img/cdn-edge/origin-shield.svg)

All layers must agree on the cache key and `Vary` semantics. If edges normalize
`utm_source` away but the shield does not, shield hit ratio fragments. If the
shield omits an authorization component included at the edge, data can leak.

The shield is also an overload controller. It can bound concurrent origin
requests, queue briefly, shed low-priority work, enforce retry budgets, and use
circuit breakers. Retries should occur at one layer; if browser, edge, shield,
and origin proxy all retry independently, one failure multiplies traffic.

A shield adds a network hop on a true miss. Its value comes from higher global
reuse and origin protection, not from being closer to the end user.

---

# 24. Purging Is a Distributed Consistency Problem

Atlas discovers that `hero-v7` contains an incorrect photo credit. Waiting up
to 600 seconds is unacceptable. The publishing system issues a purge for the
canonical key and all `Accept` variants.

A purge control plane:

1. authenticates and durably records the invalidation;
2. assigns an ordering token or generation;
3. fans the event to regions and PoPs;
4. marks matching entries unusable in memory and disk indexes;
5. acknowledges progress and retries disconnected sites;
6. prevents an older in-flight fill from republishing the purged generation.

![A purge generation propagates while blocking an older in-flight fill](/assets/img/cdn-edge/purge-propagation.svg)

Propagation is not instantaneous. During the interval, different PoPs can
serve different versions. A purge API should define what completion means:
accepted centrally, delivered to every healthy PoP, or verified no longer
servable.

## Versioned URLs Avoid the Hardest Purge

For immutable content, Atlas can publish:

~~~text
/eclipse/hero.v8.avif
~~~

and update the page reference. `v7` can keep a year-long TTL because its URL
never changes meaning. Versioning converts consistency from "remove every old
copy now" into "make new documents reference a new identity."

Purging remains necessary for legal removal, leaked secrets, unsafe content,
and stable URLs whose meaning must change immediately. Those cases require a
well-tested invalidation path, not hope that TTL is short enough.

---

# 25. Cache-Key Security Is Data Security

Many CDN vulnerabilities are identity disagreements:

- **cache poisoning**: attacker-controlled input changes the origin response
  but is absent from the key, so victims receive it;
- **cache deception**: a personalized route is made to look cacheable through
  path or extension tricks;
- **tenant collision**: host or tenant identity is omitted;
- **variant confusion**: `Vary` is missing or normalized differently;
- **web cache entanglement**: two layers construct different identities;
- **purge mismatch**: invalidation names a different canonical key than lookup;
- **signed-URL bypass**: a security parameter is dropped before authorization.

![One omitted key field turns an attacker response into a victim cache hit](/assets/img/cdn-edge/cache-poisoning.svg)

Defenses follow from making interpretation singular:

- normalize once and use the canonical form for lookup, forwarding, logging,
  and purge;
- reject ambiguous authority, path, length, and transfer semantics;
- partition caches by tenant and security policy;
- cache only routes explicitly declared safe;
- bound variant dimensions;
- include or validate every response-affecting input;
- never let a cached result bypass authorization that would occur on a miss.

A higher hit ratio is never worth crossing an authorization boundary.

---

# 26. Edge and Regional Failure

## One Cache Node Fails

When `E3` fails, consistent hashing moves its keys to successors. Stored bytes
on `E3` are disposable, but the new owners are cold. Rate limits and shield hits
prevent the refill wave from reaching the origin all at once.

## The London PoP Fails

The CDN withdraws or de-preferences London's route and changes DNS steering.
New connections from `R1` reach Amsterdam. Existing connections may fail and
retry. Amsterdam must have enough reserved headroom for failover traffic.

## London Is Isolated from Purge Distribution

Serving cached content while disconnected is acceptable only within policy. A
security purge stream may fail closed after a bounded control-plane lease,
whereas immutable public assets may continue serving. When London reconnects,
it must replay missed purge generations before advertising full readiness.

## The Shield Fails

Edges can select another shield or go directly to origin under a strict global
budget. Blindly bypassing the shield from every PoP can turn a shield outage
into an origin outage.

![Node failure, PoP withdrawal, and shield bypass under bounded failover](/assets/img/cdn-edge/regional-failover.svg)

## The Origin Fails

Fresh hits continue normally. Eligible stale objects use `stale-if-error`.
Uncached or forbidden-to-stale requests fail, ideally without synchronized
retry storms. A CDN improves availability only for state it already has and is
allowed to reuse.

---

# 27. Capacity and Origin-Offload Mathematics

Request hit ratio and byte hit ratio answer different questions:

~~~text
request_hit_ratio = cache_hits / cacheable_requests
byte_hit_ratio    = bytes_from_cache / cacheable_response_bytes
~~~

A million cached 2 KiB icons can produce an excellent request hit ratio while
one uncached 5 GiB download dominates origin bandwidth. Track both.

If cacheable request rate is `Q`, request hit ratio is `H`, and average collapsed
fan-in on a miss is `C`, an approximate shield request rate is:

~~~text
shield_requests/s ~= Q * (1 - H) / C
~~~

If shield hit ratio is `S`, approximate origin request rate becomes:

~~~text
origin_requests/s ~= Q * (1 - H) * (1 - S) / C
~~~

These are planning approximations. Variants, regional skew, object sizes,
revalidation, negative responses, ranges, retry, and bypass traffic all change
the real load.

![Edge hits, collapsed misses, shield hits, and resulting origin load](/assets/img/cdn-edge/origin-offload.svg)

Capacity must include failure states:

- one cache node's hot keys moving to neighbours;
- one PoP's traffic moving to regional peers;
- shield bypass budget;
- cold-cache refill bandwidth after deploy or restart;
- purge bursts for broad tags;
- TLS handshakes after route changes;
- log and metrics delivery during an incident.

Steady-state headroom is the resource that makes failover real.

---

# 28. Observing the Decision at Every Layer

A useful access log explains why the cache behaved as it did:

~~~text
request_id          r-8f3a
pop / node          lon-2 / E3
tenant              atlas
key_hash            7c91...
variant             avif
cache_status        hit | miss | stale | revalidated | bypass
age / ttl           420 / 180
collapse_role       leader | follower | none
upstream            S-LON | origin | none
purge_generation    9821
bytes               2516582
latency              edge=4ms upstream=0ms total=6ms
~~~

The standardized `Cache-Status` response field can expose whether a cache hit,
forwarded, stored, or collapsed a request and can report remaining TTL. Public
responses should avoid leaking sensitive cache keys or internal topology.

Monitor distributions, not only averages:

- request and byte hit ratio by tenant, route, PoP, and status code;
- fresh, stale, revalidated, bypass, and negative-hit rates;
- key cardinality and variant explosion;
- memory/disk admission, eviction, and object-size distributions;
- collapsed followers per leader and waiter timeouts;
- shield hit ratio and origin concurrency;
- revalidation `304` versus replacement `200` rate;
- purge enqueue, delivery, application, and verification latency;
- stale-if-error age and volume;
- node remaps, route withdrawals, and regional spillover;
- origin status, latency, retry, and circuit-breaker state.

The most informative incident timeline connects layers: route shift, TLS surge,
cache remap, hit-ratio fall, shield queue growth, then origin saturation.

---

# 29. Failure Scenarios in the Atlas Story

## The Origin Omits `Vary: Accept`

The AVIF response can be reused for a JPEG-only client. This is an origin and
cache-contract bug. Purge affected entries, correct `Vary`, and test variants at
every cache layer.

## A Tracking Parameter Fragments the Key

`utm_campaign` creates thousands of keys for identical bytes. Normalize it out
only after proving it does not affect authorization, origin routing, or content.

## A Purge Races an In-Flight Miss

Generation 9821 invalidates `v7` while an older fetch is completing. The fill
must compare its starting generation before publishing; otherwise it resurrects
the purged object.

## The Revalidation Leader Times Out

Followers must not wait forever or all retry simultaneously. Elect or permit a
bounded new leader with jitter, while stale policy determines whether waiting
readers receive old bytes or an error.

## A Node Restart Empties Memory

Disk and shield hits warm it gradually. Admission control prevents a one-hit
scan from replacing the hot working set. Prewarming is useful only for a known,
small, high-value set.

## A Cached `404` Outlives Object Creation

The short negative TTL eventually expires, but publication can also purge that
key immediately. Negative-cache policy belongs in the deployment workflow.

## A Range Segment Comes from a New Version

Never combine it with old segments unless a strong validator proves they belong
to the same representation. Otherwise the resulting video is a byte-level mix
that never existed at the origin.

## Amsterdam Has No Failover Headroom

Routing successfully moves London traffic, but Amsterdam overloads and withdraws
too, causing a cascade. Routing availability and serving capacity must be
planned together.

---

# 30. The Whole Image, End to End

The complete journey of `hero-v7-avif` is now one connected history:

1. DNS returns an Anycast service address for `media.atlas.example`.
2. Internet routing sends `R1`'s new connection to the London PoP.
3. TLS SNI and HTTP authority select Atlas's tenant configuration.
4. normalization produces canonical path `/eclipse/hero` and query `w=1200`.
5. `Vary: Accept` selects the AVIF representation key.
6. consistent hashing assigns that key to cache node `E3`.
7. memory and disk miss, then shield `S-LON` misses.
8. one collapsed leader fetches `hero-v7-avif` from the origin.
9. the response streams to readers and fills shield, disk, and memory.
10. later requests hit `E3` while `current_age < 600`.
11. at age 601, stale-while-revalidate serves `v7` while one conditional request
    carries `If-None-Match: "hero-v7-avif"`.
12. a `304 Not Modified` refreshes metadata without resending 2.4 MiB.
13. during a later origin `503`, stale-if-error serves the bounded stored copy.
14. an editorial purge records generation 9821 and invalidates all hero variants.
15. the next request fetches corrected `hero-v8-avif`.
16. when `E3` fails, only its hashed key range remaps and refills through shield.
17. when London withdraws, `R1` reconnects through Amsterdam.

![The complete CDN request, cache, purge, and failover lifecycle](/assets/img/cdn-edge/end-to-end.svg)

| Risk introduced | Mechanism that contains it |
|---|---|
| distant centralized origin | geographically distributed PoPs |
| static routing during failure | DNS steering and Anycast withdrawal |
| cross-tenant or cross-variant collision | canonical tenant-aware key plus `Vary` |
| duplicated objects on random nodes | consistent/rendezvous hashing |
| globally repeated edge misses | origin shield |
| simultaneous cold misses | request collapsing |
| cached bytes becoming old | freshness lifetime and validators |
| revalidation latency | stale-while-revalidate |
| origin outage | bounded stale-if-error |
| immutable files and finite storage | admission and eviction |
| repeated missing-object load | short negative caching |
| large-object seek traffic | validator-safe range caching |
| urgent correction | versioned URLs and purge generations |
| cache node or PoP loss | remapping, headroom, and regional failover |

---

# 31. What a CDN Guarantees—and What It Does Not

With correct configuration, a CDN can provide:

- lower latency and wide-area bandwidth use for cached content;
- bounded shared-cache freshness based on HTTP policy;
- representation-safe reuse through cache keys and `Vary`;
- efficient conditional revalidation with entity tags;
- origin-load reduction through edge hits, shields, and collapsing;
- bounded stale service during refresh or eligible failure;
- distributed invalidation with observable propagation;
- isolation and rerouting around cache-node and regional failure.

It does not automatically provide:

- the correct cache key for application semantics;
- safety for personalized responses marked public by mistake;
- instant simultaneous purge at every disconnected PoP;
- exactly one origin request across every region and failure race;
- freshness beyond what policy and purge delivery establish;
- unlimited resilience when no usable object is cached;
- correct byte-range assembly without validators;
- failover capacity that was never provisioned;
- protection from an origin and cache interpreting a request differently.

The origin, publishing system, CDN configuration, and application share the
contract. Headers alone cannot repair an ambiguous identity model, and a fast
cache hit is harmful if it returns the wrong representation.

---

# 32. Conclusion

CDN behavior becomes understandable when followed as one state machine rather
than reduced to "hit or miss."

An individual edge cache is a specialized web server. It becomes part of a CDN
when a global control plane coordinates its routing, configuration, content,
capacity, invalidation, and failure state with many other delivery sites. That
coordination moves high-volume fan-out away from one origin network and toward
the networks where readers actually are.

Routing chooses a PoP. Ingress establishes tenant and canonical request
semantics. The cache key and `Vary` define identity. Hashing chooses a cache
node. The hierarchy decides how far a miss travels. Collapsing turns concurrent
demand into one upstream operation. Freshness and validators decide when stored
bytes can be reused. Stale extensions trade bounded recency for latency and
availability. Purge generations override ordinary time when content must
disappear. Finally, remapping and route withdrawal move replaceable cache state
without pretending every connection survives.

The Atlas request can be compressed to:

~~~text
hostname
  -> DNS answer and Anycast route
  -> tenant-aware canonical request
  -> URI key + representation variant
  -> edge node ownership
  -> memory / disk / shield / origin
  -> collapsed fill
  -> freshness and revalidation
  -> stale failure policy
  -> purge generation
  -> node and regional failover
~~~

A CDN is successful when the reader sees a nearby byte, the origin sees a tiny
fraction of demand, and every layer agrees that it is the right byte.

---

# References

1. [RFC 9111 — HTTP Caching](https://www.rfc-editor.org/rfc/rfc9111.html)
2. [RFC 9110 — HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html)
3. [RFC 5861 — stale-while-revalidate and stale-if-error](https://www.rfc-editor.org/rfc/rfc5861.html)
4. [RFC 9211 — The Cache-Status HTTP Response Header Field](https://www.rfc-editor.org/rfc/rfc9211.html)
5. [RFC 4786 — Operation of Anycast Services](https://www.rfc-editor.org/rfc/rfc4786.html)
6. [Consistent Hashing and Random Trees](https://dl.acm.org/doi/10.1145/258533.258660)
7. [Netflix Open Connect](https://openconnect.netflix.com/en/)
8. [Netflix Open Connect Overview](https://openconnect.netflix.com/Open-Connect-Overview.pdf)
9. [Google Cloud CDN and Media CDN](https://cloud.google.com/cdn)
