---
layout: single
comments: true
title: "Inside a CDN: Cache Keys, Freshness, Revalidation, and Origin Protection"
date: 2026-08-17 02:00:00+0100
description: "A first-principles journey through CDN caching: how a request reaches an edge, how HTTP responses are identified and reused, and how freshness, revalidation, purging, shielding, and failover work."
tags: [cdn, caching, edge-computing, http, networking, distributed-systems, system-design]
categories: ['Distributed Systems Components']
---

# 1. Why a CDN Exists

Suppose Atlas publishes a news site from one US region. A reader in London
requests the story's 2.4 MiB hero image:

```text
London reader
    -> public Internet
    -> Atlas load balancer in the US
    -> image service
    -> object storage
    -> the same long path back to London
```

The Atlas deployment is the **origin**: the authoritative system that stores
or generates the response. The origin might contain load balancers, application
servers, databases, and object storage rather than one literal machine. What
matters is that every reader request travels to infrastructure responsible for
the canonical content.

An application cache at the origin can avoid regenerating the image or reading
it repeatedly from storage. It cannot shorten the London-to-US path, prevent
each request from reaching the origin network, or avoid transmitting another
2.4 MiB copy across that path.

The problem is not that the origin is a bad server. The problem is that the
same reusable bytes are produced in one place and consumed repeatedly around
the world.

A **content delivery network**, or **CDN**, moves reusable responses closer to
readers. It operates geographically distributed servers in **points of
presence**, or **PoPs**. A server handling requests in a PoP is commonly called
an **edge server**.

```text
origin-only
    London reader --------------------------> US origin

with a warm CDN cache
    London reader ----> London edge cache
```

![Origin-only delivery compared with edge delivery](/assets/img/cdn-edge/origin-vs-edge.svg)

The origin remains authoritative. The edge stores a disposable copy and reuses
it only when HTTP rules and CDN policy say that doing so is safe.

For 100,000 London readers requesting the same 2.4 MiB image, the origin-only
path transmits approximately:

```text
100,000 * 2.4 MiB = 240,000 MiB ~= 234 GiB
```

With a warm edge cache, the CDN can fetch a small number of upstream copies and
deliver the rest locally. Real systems have several cache nodes, fills,
evictions, ranges, and revalidations, so the origin will not necessarily send
exactly one copy. The important change is that demand is served by reuse near
the readers rather than repeated work at the origin.

![One origin copy can become many nearby edge responses](/assets/img/cdn-edge/cdn-benefit-example.svg)

This improves several different dimensions:

- **latency:** a warm response takes the client-to-edge path instead of the
  client-to-origin path;
- **origin load:** a hit avoids an origin request, application work, storage
  access, and response bytes;
- **burst capacity:** many PoPs can serve an already cached popular object;
- **failure tolerance:** policy may permit a bounded stale response while the
  origin is unavailable.

A CDN shifts work rather than eliminating it. Edge storage and bandwidth cost
money, cold misses still reach the origin, and a wrong cache decision can
distribute stale or private data extremely quickly.

> **What to remember:** An origin cache avoids repeated computation. A CDN
> cache can additionally avoid repeated long-distance transfer and origin
> traffic by reusing the response near its readers.

---

# 2. What a CDN Actually Caches

A CDN is a geographically distributed **reverse proxy**. A reverse proxy
accepts a request on behalf of an upstream server, applies policy, and either
answers locally or forwards the request upstream.

The cache does not merely save “a file.” It stores a reusable **HTTP response**
and the metadata needed to decide whether it can answer a later request:

```text
cache entry
    identity        which requests this entry can answer
    status          200, 301, 404, ...
    headers         Content-Type, Cache-Control, ETag, Vary, ...
    body            image bytes, HTML, JSON, video segment, ...
    timing          when it was generated, received, and validated
    storage state   memory, disk, or another cache layer
```

A stored response is useful only when three questions all have safe answers:

1. **Eligibility:** may a shared cache store and reuse this response?
2. **Identity:** does this later request ask for the same representation?
3. **Freshness:** may the stored response be reused now without contacting the
   origin?

Those questions distinguish a cache from a directory of downloaded files.

## Browser Cache, Origin Cache, and CDN Cache

The same HTTP caching model appears in several locations, but the trust and
cost boundaries differ:

| Cache | Who can reuse its entry? | What a hit avoids |
|---|---|---|
| Browser cache | Usually one browser profile | Network transfer from the browser outward |
| Origin-side cache | Requests reaching the origin deployment | Application work and storage access |
| CDN shared cache | Many users whose requests match the same public representation | Long-distance transfer and most origin work |

![An application cache, an origin cache, and a CDN cache remove different costs](/assets/img/cdn-edge/cache-location-comparison.svg)

A **private cache**, such as a browser cache, can store a response for one
user. A **shared cache**, such as a CDN, might reuse one entry for unrelated
users. Shared reuse therefore requires much stricter identity and authorization
boundaries.

## What Is a Good Candidate?

Responses with wide, safe reuse produce the strongest benefit:

| Workload | Typical CDN value | Why |
|---|---|---|
| Versioned CSS, JavaScript, fonts, and images | Very high | Immutable and requested repeatedly |
| Video segments and software downloads | Very high | Large bodies make byte offload valuable |
| Public articles and product data | High when freshness is explicit | Many readers can share a representation |
| Personalized account pages | Usually unsuitable for shared caching | One user's response must not reach another |
| Writes and mutations | No cache reuse | The origin must perform authoritative state changes |
| Rare or nearly unique URLs | Limited | Storage and miss handling may exceed saved work |

An uncacheable request can still benefit from edge TLS termination, attack
filtering, compression, or an optimized backbone. Passing through a CDN is not
the same as being answered from its cache.

When a request reaches an edge server, it can therefore take four broad paths:

```text
fresh reusable entry     -> return it
stale reusable entry     -> validate it or apply stale policy
no matching entry        -> fetch upstream and perhaps store the response
unsafe or ineligible     -> bypass shared caching
```

> **What to remember:** A cache hit is permission to reuse a stored HTTP
> response for this request. It is not merely proof that some bytes exist on
> disk.

---

# 3. The Atlas Image Request

Atlas publishes a story about a solar eclipse. The page references this image:

```text
https://media.atlas.example/eclipse/hero?w=1200
```

The browser can display AVIF, so it sends:

```http
GET /eclipse/hero?w=1200 HTTP/1.1
Host: media.atlas.example
Accept: image/avif,image/webp,image/*;q=0.8
```

The origin currently has representation `hero-v7-avif`, 2.4 MiB in size:

```http
HTTP/1.1 200 OK
Content-Type: image/avif
Content-Length: 2516582
ETag: "hero-v7-avif"
Vary: Accept
Cache-Control: public, max-age=60, s-maxage=600, stale-while-revalidate=30, stale-if-error=86400
```

This response carries its reuse contract. `Vary: Accept` says that different request header
values can select different image formats. `s-maxage=600` gives a shared cache
a ten-minute freshness lifetime. `ETag` lets the cache later ask whether its
stored representation is still current without downloading the body again.
The stale directives define bounded exceptions after normal freshness ends.

The first London reader will cause this path:

```text
browser
    -> DNS and Internet routing
    -> London PoP ingress
    -> tenant and security policy
    -> cache identity and variant
    -> edge cache node
    -> regional shield
    -> Atlas origin
```

The response then fills the caches on its way back. Later readers reuse the
stored response until time, eviction, invalidation, or a changed request makes
that reuse unsafe.

![The Atlas hero image's complete CDN setting](/assets/img/cdn-edge/story-overview.svg)

The next question is not yet whether the image is cached. The browser first has
to reach an Atlas-capable edge location.

---

# 4. How the Browser Reaches an Edge

The hostname `media.atlas.example` points traffic at the CDN instead of
directly at the Atlas origin. CDNs commonly combine DNS steering with Anycast
routing.

## DNS Chooses a Service Address

The authoritative DNS system can select an answer using resolver location,
measured latency, PoP health, available capacity, and customer policy. A CNAME
can lead from the Atlas hostname to a CDN-controlled hostname whose answers
change over time.

DNS does not run for every HTTP request. Recursive resolvers and clients cache
answers according to their TTL. A shorter TTL allows faster redirection but
increases DNS traffic; a longer TTL reduces lookup work but leaves old answers
in use longer. The resolver's location can also differ from the reader's
location, so DNS steering is an informed approximation rather than exact
per-user routing.

## Anycast Chooses a Reachable PoP

With **Anycast**, several PoPs advertise reachability to the same service IP
address. Internet routing selects one advertised path. “Nearest” means
preferred by current routing topology and policy—not necessarily the closest
building or even the path with the lowest measured latency.

For the Atlas request, DNS returns `203.0.113.80`, advertised from London,
Amsterdam, and Paris. Current routing sends the new connection to London:

![DNS policy selects an address while Anycast routing selects a PoP](/assets/img/cdn-edge/global-routing.svg)

If London withdraws that route, later connections can reach another PoP.
Existing TCP or QUIC connections might fail and reconnect; Anycast changes
reachability, not application-session ownership or transport state.

![Anycast route withdrawal moves new traffic to another PoP](/assets/img/cdn-edge/anycast-failover.svg)

DNS and Anycast solve different parts of steering:

```text
DNS       hostname -> service address
Anycast   service address -> one currently reachable PoP
```

They find a delivery location. They do not decide whether the request is
cacheable or which stored object it represents.

## Ingress Establishes Request Context

Inside the London PoP, the connection passes through several logical stages:

1. a local load balancer selects an ingress proxy;
2. TLS Server Name Indication selects candidate certificate and tenant state;
3. HTTP `Host` or `:authority` confirms the requested tenant;
4. rate limits, bot policy, and a web application firewall run where enabled;
5. request normalization produces one canonical internal interpretation;
6. cache policy decides whether shared lookup is allowed;
7. the cache key identifies the requested representation.

![TLS, tenant routing, security, normalization, and cache lookup](/assets/img/cdn-edge/ingress-pipeline.svg)

The order is security-sensitive. The cache and origin must not interpret the
same path, authority, percent encoding, duplicate header, or message length in
different ways. Ambiguous input is rejected or canonicalized once, and that
canonical interpretation is used for lookup, forwarding, logging, and purge.

Multi-tenant isolation begins here as well. An internal tenant identifier or
validated hostname must participate in routing and key construction;
otherwise two customers that both publish `/logo.svg` could collide.

> **What to remember:** Steering chooses an edge location. Ingress then proves
> which tenant and request the edge is handling before any cache lookup occurs.

---

# 5. The Cache Key Defines “The Same Response”

A cache lookup needs an exact identity. “The same URL” is often too vague:
query parameters can select image size, request headers can select format or
language, and identical paths can belong to different tenants.

A representative internal identity for the Atlas request is:

```text
tenant       = atlas
scheme       = https
authority    = media.atlas.example
path         = /eclipse/hero
query        = w=1200
method       = GET
variant      = accept-format:avif
```

![The request fields included in and excluded from the cache key](/assets/img/cdn-edge/cache-key.svg)

HTTP caching uses the request method and target URI as the primary cache key.
Stored `Vary` metadata then distinguishes representations selected by request
headers. A CDN can add controlled tenant partitioning and normalization, but it
must preserve the application's semantics.

Every key choice trades correctness against reuse:

- omit `w=1200`, and different image sizes can collide;
- preserve every tracking parameter, and equivalent URLs fragment into many
  cold entries;
- omit the Atlas tenant, and two customers can share data accidentally;
- include a unique request ID, and every request becomes a miss;
- ignore a representation-selecting header, and a client can receive a format
  it cannot use.

Query normalization should therefore use an allowlist of understood rules. A
signature, authorization token, experiment selector, or origin-routing
parameter can affect the response even if it resembles disposable tracking
data.

## `Vary` Adds Request-Header Dimensions

Atlas returns:

```http
Vary: Accept
```

The primary URI can now have several stored variants:

```text
Accept selects AVIF -> hero-v7-avif
Accept selects WebP -> hero-v7-webp
Accept selects JPEG -> hero-v7-jpeg
```

![One primary URI with three Vary-selected representations](/assets/img/cdn-edge/vary-variants.svg)

For a later request, the cache compares the fields named by the stored
response's `Vary` value. Only a matching variant can be reused. `Vary: *`
never matches a later request.

Variation must be bounded. `Vary: User-Agent` can produce thousands of nearly
unique entries. A CDN might convert raw device or format headers into a small,
audited set of classes, provided the edge and origin use the same
classification.

## Eligibility Comes Before Reuse

Even a perfectly matching key is not enough. Shared caches must apply response
policy:

| Directive or condition | Meaning for shared caching |
|---|---|
| `public` | The response is explicitly reusable by a shared cache when other rules permit. |
| `private` | A shared cache must not store the response. A private browser cache may. |
| `no-store` | Do not store the response in a cache. |
| `no-cache` | Storage is allowed, but the response must be validated before reuse. |
| `s-maxage=N` | Shared-cache freshness lifetime is `N` seconds and takes precedence over `max-age`. |
| `must-revalidate` | Once stale, do not reuse without successful validation unless another explicit rule permits it. |
| Request has `Authorization` | Shared reuse requires an explicit response directive that permits it. |

`no-cache` is commonly misunderstood: it does not mean “do not store.” It means
“do not reuse without successful validation.” `no-store` is the directive that
forbids storage.

Cookies should not automatically become an unbounded cache-key dimension.
Personalized routes normally bypass shared caching. If a bounded cookie-derived
variant such as a country or plan tier is truly safe, it is extracted through
explicit, reviewed policy.

Most CDN caching policies concentrate on `GET` and `HEAD`. Other methods are
not made reusable merely because they return a body; both HTTP semantics and
explicit CDN policy must permit storage and later reuse. Likewise, some status
codes are cacheable by default while others require explicit freshness. The
safe operational rule is to configure understood methods, routes, and statuses
rather than treating every response as a candidate.

The most important safety rule is simple:

> Never let a cache hit skip authorization that an equivalent miss would have
> performed.

> **What to remember:** The key proves identity; cache directives and security
> policy prove eligibility. Safe shared reuse needs both.

---

# 6. A Cold Miss Becomes a Warm Hit

Most CDNs use **pull caching**. Atlas publishes the image at its origin and
routes the hostname through the CDN. It does not upload the image to every PoP
before anyone requests it.

The first London request is a **cold miss**:

1. no reusable Atlas AVIF entry exists locally;
2. the edge forwards the canonical request upstream;
3. the origin returns the image and cache policy;
4. the CDN streams the response to the reader while storing an eligible copy.

The next matching request can be a **warm hit**:

1. the edge finds the matching stored representation;
2. the entry is eligible and still fresh;
3. the edge returns it without contacting the origin.

![A pull CDN learns on the first request and reuses on later requests](/assets/img/cdn-edge/pull-cache-lifecycle.svg)

Some operators deliberately **prewarm** or preposition important objects before
a launch. That reduces first-reader latency but consumes storage and fill
bandwidth in locations that might never request the object. Pull caching pays
the fill cost only where demand appears.

## One PoP Contains Several Cache Layers

The London PoP is not one enormous map. A typical request can pass through:

1. **process memory** for the hottest metadata and small bodies;
2. **PoP-local memory or disk** for a larger working set;
3. a **regional or origin shield** shared by several PoPs;
4. the **origin** when no reusable upstream entry exists.

![Memory, PoP storage, regional shield, and origin hierarchy](/assets/img/cdn-edge/cache-hierarchy.svg)

Inside the PoP, a dispatcher can use consistent or rendezvous hashing to map a
key onto a cache node. This keeps repeated requests for one object near the
same storage instead of duplicating it randomly across every node.

![Consistent hashing assigns the Atlas representation to edge node E3](/assets/img/cdn-edge/consistent-hashing.svg)

If node `E3` disappears, only the keys assigned to its portion of the mapping
move to other nodes. The mapping is an optimization, not durable authority:
lost cache bytes can be fetched again.

Hot objects can exceed one node's network capacity even if storage is balanced.
A CDN can replicate a hot key, use two-choice routing, or serve it from several
worker memories. Placement must account for request rate as well as object
size.

## The Response Fills Outward

For the first request, `E3` misses in memory and disk. Its shield also misses
and fetches `hero-v7-avif` from Atlas:

```text
origin 200 response
    -> shield stores an eligible response
    -> E3 stores it on disk
    -> E3 may promote it to memory
    -> reader receives bytes as they arrive
```

![A cold miss fills the hierarchy while streaming to the reader](/assets/img/cdn-edge/cold-fill.svg)

The edge does not need to receive all 2.4 MiB before sending the first bytes to
the reader. It can stream and store concurrently. The entry must not become
reusable, however, until headers, length, storage completion, and any integrity
checks are valid. A disconnected or truncated fill is not a complete cached
response.

> **What to remember:** A miss is an upstream fetch, not a failure. If the
> response is eligible, that fetch creates the stored entry that later requests
> can reuse.

---

# 7. Concurrent Misses Must Share Work

A single cold miss is harmless. Ten thousand readers requesting a newly
published object before its first fill finishes can overwhelm the origin.

Without coordination:

```text
10,000 edge misses -> 10,000 upstream fetches
```

With **request collapsing**, the first miss becomes the leader for that cache
identity. Equivalent later requests become followers:

```text
1 leader             -> shield or origin fetch
9,999 followers      -> wait for the same result
```

![Many simultaneous misses collapse behind one origin fetch](/assets/img/cdn-edge/request-collapsing.svg)

Equivalence matters. Requests with different authorization, range, variant,
or cache policy cannot safely join merely because their paths look similar.
When the response headers arrive, the cache must also confirm that the result
is reusable for the waiting requests.

Collapsing needs bounded failure behavior:

- a maximum waiter count and wait deadline;
- leader cancellation that cannot strand followers;
- retry limits and randomized delay;
- correct propagation of non-cacheable and error responses;
- a generation check so an older fill cannot publish after a purge.

The same pattern can operate at multiple layers. Edge collapsing protects a
shield, and shield collapsing protects the origin across several PoPs.

The standardized `Cache-Status` header can describe a hit, forwarded miss,
stored result, or collapsed request. For example:

```http
Cache-Status: AtlasEdge; fwd=uri-miss; collapsed; stored
```

That is diagnostic metadata, not a caching instruction. Public responses
should avoid exposing sensitive cache keys or internal topology.

> **What to remember:** A viral object should create many downstream responses,
> not many simultaneous origin computations. Request collapsing changes
> concurrency, while caching changes reuse over time.

---

# 8. Freshness Determines Whether Stored Bytes Are Reusable Now

An eligible, matching response can still be too old for ordinary reuse.
HTTP calls a stored response **fresh** while its freshness lifetime exceeds its
calculated current age:

```text
fresh = current_age < freshness_lifetime
```

For the Atlas response, `s-maxage=600` takes precedence in the shared CDN cache
over `max-age=60`:

```http
Cache-Control: public, max-age=60, s-maxage=600
```

The CDN can normally reuse the response for ten minutes. A browser receiving
the same response has a one-minute lifetime, but it does not necessarily
receive a new minute. If the CDN sends `Age: 420`, the browser knows the
response has already aged well beyond its browser-cache lifetime.

Explicit freshness is normally chosen in this order: a shared cache uses
`s-maxage` when present, otherwise `max-age`, otherwise an `Expires` time
relative to `Date`. When no explicit lifetime exists, HTTP can permit
**heuristic freshness**, where the cache estimates a conservative lifetime
from metadata such as `Last-Modified`. Important CDN routes generally use an
explicit policy so freshness is intentional rather than inferred.

`Age` is the cache's estimate of how long ago the response was generated or
last validated at the origin. Correct calculation includes inherited `Age`,
apparent origin age, transfer time, and time resident in each cache. A
downstream cache cannot reset the age merely because it received the response
from an upstream cache.

![Age accumulates through origin, shield, edge, and browser](/assets/img/cdn-edge/age-propagation.svg)

At age 420 seconds, the London edge can return a fresh hit:

```http
HTTP/1.1 200 OK
Age: 420
ETag: "hero-v7-avif"
Cache-Control: public, max-age=60, s-maxage=600, stale-while-revalidate=30, stale-if-error=86400
Cache-Status: AtlasEdge; hit; ttl=180
```

No origin request occurs. At age 600, the response becomes **stale**. Stale
does not mean corrupt, deleted, or unusable under every circumstance. It means
the ordinary freshness permission has ended; the cache now needs validation
or a specific rule allowing stale reuse.

![Fresh, stale-while-revalidate, and stale-if-error time windows](/assets/img/cdn-edge/freshness-timeline.svg)

Freshness and storage residency are independent:

| State | Meaning |
|---|---|
| Fresh and resident | Reusable immediately. |
| Stale and resident | Bytes exist, but reuse needs validation or stale permission. |
| Fresh by policy but evicted | It would have been reusable, but the local copy is gone. |
| Stale and evicted | Neither a local body nor ordinary freshness remains. |

![Freshness state and storage residency are independent axes](/assets/img/cdn-edge/expiration-vs-eviction.svg)

A TTL controls reuse semantics. **Eviction** manages finite storage. Recency,
frequency, body size, fetch cost, tenant quota, and admission policy can all
influence which entries remain. Removing London's local copy does not purge
Amsterdam or tell the origin that the representation has changed.

> **What to remember:** Presence answers “do we have bytes?” Freshness answers
> “may we reuse them without contacting the origin?” They are separate
> decisions.

---

# 9. Revalidation Asks Whether the Stored Representation Changed

When `hero-v7-avif` becomes stale, the edge could download the entire 2.4 MiB
body again. The `ETag` validator allows a cheaper question:

```http
GET /eclipse/hero?w=1200 HTTP/1.1
Host: media.atlas.example
Accept: image/avif,image/webp,image/*;q=0.8
If-None-Match: "hero-v7-avif"
```

If the selected representation is unchanged, the origin returns:

```http
HTTP/1.1 304 Not Modified
ETag: "hero-v7-avif"
Cache-Control: public, max-age=60, s-maxage=600, stale-while-revalidate=30, stale-if-error=86400
```

The `304` carries no image body. It tells the cache to reuse the stored body
while updating the response metadata used for future decisions.

![Conditional revalidation returns either 304 metadata or a new 200 body](/assets/img/cdn-edge/revalidation.svg)

If Atlas has published `hero-v8-avif`, the origin instead returns `200 OK` with
the new body and `ETag: "hero-v8-avif"`. The cache completes and validates that
replacement before publishing it as the reusable entry.

Entity tags can be strong or weak. A strong validator identifies a
byte-equivalent representation and is needed for operations such as safely
combining partial ranges. A weak validator can express semantic equivalence but
does not prove that every byte is identical.

`Last-Modified` is another validator. A cache can send
`If-Modified-Since: <stored timestamp>` when no suitable entity tag exists.
Timestamps have coarser semantics and can miss multiple changes within their
resolution, so an origin-generated `ETag` is usually the clearer validator for
versioned or rapidly changing representations.

Revalidation saves transfer, but a synchronous check still adds origin latency
to the request that first discovers staleness.

## `stale-while-revalidate` Hides That Latency

Atlas permits 30 seconds of `stale-while-revalidate`. At age 601, the edge may
return the stale `v7` body immediately while launching one background
revalidation. Equivalent requests can continue using that stored body during
the window while collapsing prevents duplicate refreshes.

![One stale response is served while one background request revalidates](/assets/img/cdn-edge/stale-while-revalidate.svg)

If the origin returns `304`, the entry becomes fresh again. If it returns `v8`,
later readers receive the replacement. The earlier readers deliberately traded
up to 30 seconds of additional staleness for lower latency.

This is an application decision, not a universal default. A versioned asset can
tolerate generous caching. A safety alert, price, authorization decision, or
account balance may require synchronous validation or no shared caching.

An emergency invalidation overrides ordinary stale permission. If Atlas marks
`v7` forbidden, the edge cannot keep serving it merely because the HTTP stale
window has time remaining.

## `stale-if-error` Trades Recency for Availability

Suppose the origin later returns an eligible `503 Service Unavailable`.
`stale-if-error=86400` allows a previously valid stored response to be used
within a bounded stale window instead of returning that error.

![Origin failure falls back to a bounded stale response](/assets/img/cdn-edge/stale-if-error.svg)

This works only when:

- a usable response was cached before the failure;
- the error is eligible under the configured policy;
- stale age remains within the allowed boundary;
- no security or editorial invalidation forbids the object;
- observability records that stale fallback occurred.

Serving stale indefinitely would hide a prolonged outage and violate the
content's freshness contract. A CDN improves availability only for state it
already has and is allowed to reuse.

> **What to remember:** Revalidation asks whether old bytes are still current.
> Stale directives define bounded cases where latency or availability is more
> valuable than immediate recency.

---

# 10. Special Responses Need Explicit Cache Policy

The successful hero image is the simplest case. Missing objects, large files,
redirects, and errors can also be cached, but only with carefully bounded
semantics.

## Negative Caching

Suppose the page references `/eclipse/caption.vtt` before that file exists.
Without negative caching, every reader repeats the same origin `404`.

```http
HTTP/1.1 404 Not Found
Cache-Control: public, s-maxage=15
```

A 15-second cached `404` can protect the origin while allowing recovery soon
after the object appears.

![A short-lived cached 404 protects the origin without hiding recovery for long](/assets/img/cdn-edge/negative-caching.svg)

Negative caching should use an allowlist of understood status codes and short,
explicit TTLs. Publication can purge an old negative entry immediately. A
mistaken negative key is particularly damaging because it turns one routing,
variant, or authorization error into a distributed outage.

## Range Requests

Video players and download clients often request part of a large object:

```http
GET /eclipse/film.mp4 HTTP/1.1
Range: bytes=1048576-2097151
```

The response identifies both the selected interval and complete object:

```http
HTTP/1.1 206 Partial Content
Content-Range: bytes 1048576-2097151/503316480
ETag: "film-v3"
```

![A large object is cached and served as validated byte segments](/assets/img/cdn-edge/range-requests.svg)

The CDN can store a complete body, independent segments, or a larger internal
slice than the client requested. It must never assemble a response from ranges
belonging to different versions. Safe combination requires a shared strong
validator and consistent total length.

Range traffic also changes capacity: many viewers begin at offset zero, while
seeking creates a wide working set. Segment size and prefetching trade hit
ratio against memory, disk I/O, latency, and wasted upstream bytes.

## Admission Is Not Automatic

An eligible response need not be worth storing at every layer. Caching a stream
of one-hit objects can evict a small hot working set. A CDN might:

- keep metadata and small hot bodies in memory;
- admit larger objects to disk first;
- promote only after repeated access;
- stream an oversized object without storing the complete body;
- reserve tenant quotas so one workload cannot evict every other tenant.

Admission and eviction are resource policies. They cannot make an ineligible
response safe to reuse.

---

# 11. Shields Protect the Origin from Global Demand

Edge collapsing limits one PoP to one upstream miss for a key. Without another
layer, 200 PoPs can still produce 200 near-simultaneous origin requests for a
globally popular object.

An **origin shield** is a cache near the origin or at a well-connected hub:

```text
many readers -> many edge PoPs -> a few shields -> origin
```

![Origin shielding collapses misses across multiple edge PoPs](/assets/img/cdn-edge/origin-shield.svg)

The shield creates reuse and collapse across PoPs. It can also bound origin
concurrency, queue briefly, shed low-priority work, enforce retry budgets, and
open a circuit breaker when the origin is failing.

Every layer must construct compatible identities. If the edge removes a
tracking query parameter but the shield preserves it, the shield's hit ratio
fragments. If the shield omits an authorization or tenant boundary enforced at
the edge, data can leak.

Retries also require one owner. If browser, edge, shield, and origin proxy each
retry independently, one upstream failure multiplies traffic. A retry budget
defines which layer retries, how often, and with what randomized delay.

A shield adds a hop on a true miss. Its value is global aggregation and origin
protection, not proximity to the reader.

Approximate request offload can be reasoned about in stages. If cacheable
request rate is `Q`, edge request hit ratio is `H`, collapsed miss fan-in is
`C`, and shield hit ratio is `S`:

```text
requests reaching shield ~= Q * (1 - H) / C
requests reaching origin ~= Q * (1 - H) * (1 - S) / C
```

These are planning approximations. Variants, revalidation, ranges, errors,
retry, regional skew, and bypass traffic all change the real result.

![Edge hits, collapsed misses, shield hits, and resulting origin load](/assets/img/cdn-edge/origin-offload.svg)

Request hit ratio and byte hit ratio answer different questions:

```text
request_hit_ratio = cache hits / cacheable requests
byte_hit_ratio    = bytes from cache / cacheable response bytes
```

A million cached 2 KiB icons can produce a high request hit ratio while one
uncached 5 GiB download dominates origin bandwidth. Both metrics are needed.

> **What to remember:** Edge caches protect readers from distance. A shield
> protects the origin from the combined miss stream of the edge fleet.

---

# 12. Changing Content Requires Time or Invalidation

Normal freshness says how long an old representation may remain reusable. It
does not instantly remove a response already copied into many PoPs.

## Prefer Versioned URLs for Immutable Assets

Atlas can publish a corrected image under a new identity:

```text
/eclipse/hero.v8.avif
```

and update the page to reference it. `hero.v7.avif` can retain a long TTL
because its URL never changes meaning. Versioning converts “remove every old
copy now” into “make new documents reference a new object.”

This is the simplest model for compiled assets, application bundles, images,
and other immutable content. It is not sufficient when the old URL itself must
stop working—for example after a legal removal, leaked secret, unsafe file, or
urgent correction on a stable URL.

## A Purge Is a Distributed State Change

Suppose `hero-v7` contains an incorrect photo credit and cannot wait for the
ten-minute freshness lifetime. Atlas issues a purge for the canonical identity
and its `Accept` variants.

A purge system must:

1. authenticate and durably record the request;
2. assign an ordered generation or operation ID;
3. distribute it to relevant regions and PoPs;
4. mark matching memory and disk entries unusable;
5. track application and retry disconnected sites;
6. prevent an older in-flight fill from publishing after invalidation.

![A purge generation propagates while blocking an older in-flight fill](/assets/img/cdn-edge/purge-propagation.svg)

The fill race is easy to miss:

```text
generation 9820: miss for v7 starts
generation 9821: purge commits
generation 9820: old fetch completes
```

If the fill publishes without checking its starting generation, it resurrects
the purged object. The completed fetch must be discarded or evaluated under
the new generation.

Purge propagation is not physically instantaneous. An API must define whether
“complete” means accepted centrally, delivered to all currently healthy PoPs,
or verified no longer servable. Disconnected PoPs need an explicit rule for
how long they may serve without current invalidation state.

> **What to remember:** TTL handles ordinary change over time. Versioned URLs
> avoid changing an existing identity. Purge is the emergency distributed path
> for making an existing identity unusable.

---

# 13. A Fleet Turns Edge Caches into a CDN

One cached web server in London can reduce latency for London. A CDN is the
coordination that makes servers across many networks and locations behave as
one delivery service.

| One regional cache | CDN fleet |
|---|---|
| One chosen location | Many PoPs selected through global steering |
| Local health and capacity | Node, PoP, and regional failover |
| Manually configured storage | Shared fill, admission, eviction, and purge policy |
| One deployment's configuration | Versioned tenant and security state distributed globally |
| One network path | Peering, private interconnects, and sometimes caches inside access networks |

![A CDN coordinates many delivery servers as one system](/assets/img/cdn-edge/regional-server-vs-cdn.svg)

The fleet separates a high-volume **data plane** from a lower-volume **control
plane**.

The data plane handles live requests:

```text
connection -> security -> normalized request -> cache decision -> response
```

The control plane distributes the state that tells those request handlers how
to behave:

```text
tenant configuration
certificates
purge generations
routing and health policy
content-placement plans
```

![The control plane coordinates a high-volume CDN data plane](/assets/img/cdn-edge/cdn-control-data-plane.svg)

The edge must not perform a global control-plane query for every request. Each
PoP keeps a validated local snapshot so the data plane can remain fast and can
continue safely through a bounded control-plane interruption.

## Control-Plane State Has Different Consistency Needs

A purge, certificate update, and routing-health sample are all “control data,”
but they do not need the same contract:

- purge and security configuration need durable ordering;
- certificate state must respect validity and tenant binding;
- routing policy needs a coherent version;
- short-lived CPU samples can be eventually consistent and disposable.

Safety-critical updates are committed to replicated authority before they are
acknowledged. Regional fanout services distribute immutable versions:

```text
tenant config       version 418
certificate         generation 73
purge               generation 9821
routing policy      version 204
```

An edge validates a complete snapshot and installs it atomically. It does not
combine half of configuration 418 with half of 417.

![A replicated CDN control plane publishes versioned state to independent PoPs](/assets/img/cdn-edge/control-plane-ha.svg)

If a control-plane leader fails, a replica can take over from committed state.
Terms or epochs fence the old leader so it cannot later publish an older
certificate, resurrect a purge generation, or create a competing routing
decision.

## A PoP Can Be Reachable but Not Safe to Serve

During control-plane isolation, the London PoP has cached bytes and can still
receive traffic. Whether it may serve depends on the state involved:

- immutable public objects can often continue under the last-known-good policy;
- ordinary configuration can remain valid for a bounded lease;
- a new tenant cannot activate without locally proven configuration;
- an expired certificate cannot be stretched by a cache lease;
- security-sensitive purge state can require withdrawal or fail-closed behavior
  after a short deadline.

![A disconnected PoP applies different bounded policies to cached control state](/assets/img/cdn-edge/control-plane-outage.svg)

Routing readiness must include more than “the machines answer health checks.” A
PoP should advertise a tenant only while it has sufficiently current,
internally consistent certificate, configuration, purge, and serving state.

> **What to remember:** Edge servers answer requests from local state. The
> control plane makes that local behavior coherent across the fleet without
> becoming a synchronous dependency of every cache lookup.

---

# 14. Failure Changes Placement, Warmth, and Available Policy

Cached bodies are replaceable, but failures still change load and correctness
conditions. Each layer has a different recovery action.

## One Cache Node Fails

When `E3` fails, consistent hashing remaps its keys to remaining nodes. The lost
bodies can refill from disk peers, shield, or origin. The dangerous part is the
cold wave: many newly assigned keys can miss at once. Admission limits,
collapsing, and shield hits must prevent recovery from becoming an origin
incident.

## One PoP Fails

The CDN withdraws or de-preferences London's route and changes DNS steering.
New connections reach Amsterdam or another healthy site. Existing transports
may fail and reconnect. The destination PoP needs reserved network, CPU,
storage, and TLS capacity for spillover; routing cannot create capacity that
was never provisioned.

## A Shield Fails

Edges can select another shield or use direct origin access under a strict
global budget. Allowing every PoP to bypass blindly turns a shield outage into
an origin outage.

## The Origin Fails

Fresh hits continue without origin access. Eligible stale entries can use
`stale-if-error`. Requests with no stored response, or content forbidden from
stale reuse, fail. The CDN cannot manufacture authoritative content it never
received.

![Node failure, PoP withdrawal, and shield bypass under bounded failover](/assets/img/cdn-edge/regional-failover.svg)

## The Invalidation Stream Is Interrupted

A disconnected PoP can serve only within its control-policy lease. On
reconnection it retrieves missed purge generations or a fresh snapshot before
advertising full readiness. Otherwise it could reintroduce content that the
rest of the fleet has already forbidden.

Failure often moves several metrics at once:

```text
route shift
    -> TLS connection surge at failover PoP
    -> cache remapping and lower hit ratio
    -> shield queue growth
    -> higher origin load
```

Designs must be tested in that combined recovery state, not only at steady
state.

> **What to remember:** A cache node stores replaceable data, but losing it is
> not free. Recovery consumes cold-fill bandwidth and shifts pressure to every
> upstream layer.

---

# 15. Cache Identity Is a Security Boundary

Many cache vulnerabilities are disagreements about what a request means:

- **cache poisoning:** attacker-controlled input changes an origin response but
  is absent from the key, so a victim receives the stored result;
- **cache deception:** a personalized route is made to resemble a public static
  resource;
- **tenant collision:** host or tenant identity is missing from the key;
- **variant confusion:** `Vary` is absent or interpreted differently;
- **cache-layer disagreement:** edge and shield normalize into different
  identities;
- **purge mismatch:** invalidation names a different canonical identity from
  lookup;
- **signed-URL bypass:** authorization input is removed before the decision it
  protects.

![One omitted key field turns an attacker response into a victim cache hit](/assets/img/cdn-edge/cache-poisoning.svg)

The core defenses are consistency and explicit policy:

- normalize once, then use the same canonical form for keying, forwarding,
  logging, and purge;
- reject ambiguous authority, path, length, and transfer semantics;
- partition storage by tenant and security policy;
- cache only routes deliberately classified as safe;
- bound every variant dimension;
- include or validate every input that changes the response;
- apply equivalent authorization on hits and misses.

Increasing hit ratio is never worth crossing a privacy or authorization
boundary.

## Bound Work Before It Becomes an Incident

An internet-facing CDN also limits:

- request and connection establishment rate;
- header and body sizes;
- normalization and security-processing work;
- variants and key cardinality per route or tenant;
- collapsed followers and wait duration;
- memory and disk occupancy;
- concurrent shield and origin fetches;
- retries and stale-serving duration;
- purge fanout and tag expansion.

These bounds protect against attacks, client bugs, configuration mistakes, and
ordinary traffic spikes.

---

# 16. Capacity Is Mostly Bytes, Locality, and Recovery

Connection count matters, but large reusable bodies make bandwidth and storage
locality dominant.

For event or object request rate `R`, encoded body size `B`, and downstream
fan-out `F`:

```text
downstream body bandwidth ~= R * B * F
```

Video makes the scale visible. One million viewers consuming 5 Mbit/s each
require approximately:

```text
1,000,000 * 5 Mbit/s = 5 Tbit/s
```

An origin RAM cache can avoid repeated encoding and disk reads. It cannot avoid
sending roughly 5 Tbit/s out of the origin network. A CDN distributes those
bytes across many PoPs and access-network paths. One upstream copy of a popular
segment at a location can support many local plays.

![Video delivery moves fan-out from one origin to many edge networks](/assets/img/cdn-edge/video-bandwidth-fanout.svg)

The small application-control path and large media path can remain separate:

```text
player -> application API -> authentication, entitlement, manifest
player -> nearby CDN      -> high-volume media segments
```

Capacity planning includes failure states:

- one node's hot keys moving to neighbors;
- one PoP's traffic moving to regional peers;
- cold-cache refill after restart or deployment;
- a shield-bypass budget;
- broad purge bursts;
- TLS and QUIC handshakes after route changes;
- log delivery and control-plane catch-up during recovery.

Steady-state unused capacity is what makes failover possible.

---

# 17. Observe Why Every Request Took Its Path

A useful request record explains the decisions rather than reporting only a
final “hit” or “miss”:

```text
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
```

Monitor distributions by tenant, route, PoP, and status rather than relying on
fleet-wide averages:

- request and byte hit ratio;
- fresh, stale, revalidated, bypass, and negative-hit rates;
- key cardinality and variant explosion;
- object admission, eviction, and size distributions;
- collapsed followers, leader duration, and waiter timeouts;
- shield hit ratio, queue depth, and origin concurrency;
- revalidation `304` versus replacement `200` rate;
- purge commit, delivery, application, and verification latency;
- active configuration, certificate, routing, and purge versions per PoP;
- stale-if-error age and volume;
- route withdrawals, node remaps, and regional spillover.

The most useful incident view connects ownership stages. A route shift followed
by a TLS surge, cache cooling, shield congestion, and origin saturation is one
causal chain, not five unrelated alerts.

Test failure paths directly: stop a cache node, isolate a PoP from purge
distribution, make the shield fail, slow the origin, race a purge with an
in-flight fill, and redirect a region's traffic. Verify that queues remain
bounded and forbidden entries do not reappear.

---

# 18. When CDN Caching Is the Wrong Tool

CDN delivery is most valuable when many users request the same safely reusable
responses. It is not a reason to mark every route public.

| Situation | Better approach |
|---|---|
| Personalized or authorization-sensitive response | Bypass shared caching; use private browser caching only when appropriate. |
| Write or mutation | Send to the authoritative application service. |
| Response changes faster than acceptable validation cost | Use a shorter lifetime, synchronous validation, or no shared caching. |
| Nearly every URL is unique | Optimize the origin path and remove accidental key fragmentation. |
| Large dynamic response has little reuse | Use edge proxying for network/security benefits without storing it. |
| Content must disappear everywhere immediately | Avoid treating caches as the only enforcement mechanism; use versioning, revocation, and fail-closed purge policy. |

A CDN can still proxy, filter, terminate TLS, and carry these requests. The
question is specifically whether shared response reuse is correct and valuable.

---

# 19. The Complete Atlas Request

The hero image now has one connected history:

1. Atlas publishes `hero-v7-avif` and an HTTP policy describing shared reuse.
2. DNS returns a CDN service address and routing sends the reader to London.
3. TLS and HTTP authority select the Atlas tenant.
4. ingress normalizes the request and applies security and cache eligibility.
5. the URI, tenant, method, query, and `Vary: Accept` identify the AVIF variant.
6. consistent hashing assigns that identity to edge node `E3`.
7. memory and disk miss, then the shield misses.
8. one collapsed leader fetches from Atlas while equivalent followers wait.
9. the response streams to readers and fills shield, disk, and memory.
10. later requests hit while `current_age < 600`.
11. after staleness, one conditional request sends
    `If-None-Match: "hero-v7-avif"`.
12. `304 Not Modified` refreshes metadata without resending 2.4 MiB.
13. an eligible origin failure uses the bounded `stale-if-error` copy.
14. an urgent correction commits purge generation 9821 and blocks older fills.
15. the next miss stores corrected `hero-v8-avif`.
16. if `E3` fails, its identities remap and refill through the shield.
17. if London withdraws, a new connection reaches another prepared PoP.

![The complete CDN request, cache, purge, and failover lifecycle](/assets/img/cdn-edge/end-to-end.svg)

The request can be compressed into five decisions:

```text
where?       DNS and routing choose a PoP
who/what?    ingress establishes tenant and canonical request
which entry? cache key and Vary identify a representation
reuse now?   eligibility, freshness, validation, and stale policy decide
if not?      hierarchy, collapsing, shield, and origin produce a response
```

Around those decisions, the fleet distributes configuration, invalidation,
capacity, and failure recovery.

## What the CDN Can and Cannot Provide

With correct origin and edge policy, a CDN can provide:

- lower latency for warm, reusable responses;
- reduced origin requests and response bytes;
- representation-safe reuse through keys and `Vary`;
- bounded freshness and conditional revalidation;
- controlled stale service during refresh or eligible failure;
- distributed invalidation with observable propagation;
- remapping and rerouting around nodes and PoPs.

It cannot automatically provide:

- the correct application-specific cache key;
- safety for a private response mistakenly declared public;
- simultaneous purge at every isolated site;
- unlimited availability when no reusable response exists;
- correct range assembly without validators;
- capacity that was not reserved for failover;
- agreement when the origin and cache parse a request differently.

The origin, publishing system, and CDN share one HTTP caching contract. A fast
hit is useful only when every layer agrees that it is the right response for
this reader, at this time.

---

# Compact Glossary

| Term | Direct meaning |
|---|---|
| Origin | Authoritative system that stores or generates a response. |
| CDN | Geographically distributed reverse-proxy and caching fleet. |
| PoP | Point of presence containing CDN networking and serving capacity. |
| Edge server | CDN server handling requests near readers. |
| Reverse proxy | Server that answers or forwards requests on behalf of an upstream service. |
| Private cache | Cache whose stored response is reused for one user context, such as a browser profile. |
| Shared cache | Cache that can reuse a response for multiple users, such as a CDN. |
| Cache entry | Stored HTTP status, headers, body, identity, timing, and storage metadata. |
| Cache key | Primary identity used to look up candidate stored responses. |
| Variant | Representation selected by request fields named by `Vary`. |
| Cache hit | Request satisfied using a stored response without forwarding it. |
| Cache miss | No reusable stored response; the request is forwarded upstream. |
| Fresh | Reusable without contacting the origin under ordinary cache policy. |
| Stale | Ordinary freshness has ended; reuse needs validation or explicit stale permission. |
| Validator | Metadata such as an `ETag` used to ask whether a representation changed. |
| Revalidation | Conditional upstream request that confirms or replaces a stored response. |
| Request collapsing | Making equivalent concurrent misses share one upstream operation. |
| Shield | Upstream cache that aggregates misses from several edges or PoPs. |
| Eviction | Removing an entry to reclaim finite storage. |
| Purge | Distributed invalidation that makes matching stored entries unusable. |
| Data plane | Servers and network paths that handle live reader requests. |
| Control plane | Systems distributing configuration, certificates, purge, and routing state. |
| Anycast | Advertising one service address from multiple network locations. |

---

# References

1. IETF, [RFC 9111: HTTP Caching](https://www.rfc-editor.org/rfc/rfc9111.html)
2. IETF, [RFC 9110: HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html)
3. IETF, [RFC 5861: `stale-while-revalidate` and `stale-if-error`](https://www.rfc-editor.org/rfc/rfc5861.html)
4. IETF, [RFC 9211: The `Cache-Status` HTTP Response Header Field](https://www.rfc-editor.org/rfc/rfc9211.html)
5. IETF, [RFC 4786: Operation of Anycast Services](https://www.rfc-editor.org/rfc/rfc4786.html)
6. Karger et al., [Consistent Hashing and Random Trees](https://dl.acm.org/doi/10.1145/258533.258660)
