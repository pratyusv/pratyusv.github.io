---
layout: single
comments: true
title: "Inside Distributed Rate Limiters: Token Buckets, Sliding Windows, and Global Quotas"
date: 2026-08-17 05:00:00+0100
description: "A connected API traffic story explaining rate-limit algorithms, atomic decisions, Redis-backed enforcement, sharding, regional quota leasing, overshoot, fairness, and failure policy."
tags: [rate-limiting, token-bucket, sliding-window, redis, quotas, api-gateway, distributed-systems]
categories: ['Distributed Systems Components']
redirect_from:
  - /blog/2021/ratelimiter/
---

# 1. One API Key, Many Gateways

Tenant `acme` has this contract for `POST /search`:

~~~text
sustained rate    10 requests/second
burst capacity    40 requests
monthly budget    20 million search units
~~~

At 09:00, Acme's batch job sends 80 requests almost simultaneously. DNS and
load balancing distribute them across six API gateways in two regions.

If every gateway independently allows 40 requests, the system may admit all 80
even though the tenant owns one global burst budget. If every request must
synchronously contact one remote counter, the limit is accurate but the rate
limiter becomes a latency and availability dependency for the entire API.

![One tenant's requests spread across gateways and regions](/assets/img/distributed-rate-limiting/story-overview.svg)

This is the central design tension:

> A distributed rate limiter must preserve a useful quota while making a fast
> decision on every request, even though enforcement is spread across many
> processes and failure domains.

We will follow Acme's traffic through local algorithms, one atomic shared
counter, sharded enforcement, and finally leased regional quota. At every step
we will calculate both the intended limit and the maximum overshoot.

---

# 2. Rate Limiting Is Admission Control

A rate limiter decides whether a unit of work may enter a protected system now.
It is not the same as authentication, billing, load balancing, or a circuit
breaker.

![The rate limiter sits before scarce downstream work](/assets/img/distributed-rate-limiting/request-path.svg)

| Mechanism | Primary question |
|---|---|
| Authentication | Who is calling? |
| Authorization | May this caller use this operation? |
| Rate limit | May this unit enter under the caller's policy now? |
| Concurrency limit | How many units may remain in flight? |
| Circuit breaker | Is the dependency healthy enough to receive work? |
| Quota accounting | How much durable entitlement remains? |

These mechanisms often compose. A caller may be below 10 requests per second
but already have 500 slow requests in flight. A concurrency limit must still
protect the database. A request may also pass the per-second limiter but exceed
the tenant's monthly purchased budget.

---

# 3. First Define the Policy Key

The counter is meaningless until we define which requests share it. For the
story, one descriptor is:

~~~text
tenant=acme | operation=search | plan=v7
~~~

Possible dimensions include:

- authenticated user or API key;
- tenant or organization;
- source IP or network prefix;
- route, method, model, or operation class;
- destination dependency;
- region or availability zone;
- risk class, account tier, or feature plan.

![Request attributes become a bounded-cardinality policy key](/assets/img/distributed-rate-limiting/policy-key.svg)

IP-only limits can punish many users behind one NAT and are easy to evade with
many addresses. User-only limits do not protect unauthenticated endpoints. A
production edge normally layers a coarse IP/network defense before
authentication and a precise tenant or user policy afterward.

The key space must be controlled. Placing arbitrary URLs, user agents, or raw
headers into keys creates unbounded state and metric cardinality. Normalize
routes and allow only policy-approved dimensions.

---

# 4. State the Contract Mathematically

"10 requests per second" can mean several different traffic envelopes. Define:

~~~text
A(t1, t2) = admitted cost in interval [t1, t2)
r         = sustained refill rate
B         = burst capacity
~~~

A token-bucket contract permits:

~~~text
A(t1, t2) <= B + r × (t2 - t1)
~~~

For Acme, `r = 10/s` and `B = 40`. An idle client may send 40 immediately.
Over the next two seconds, at most about 20 more units become available.

Also define what is counted:

- attempts or only successful requests;
- requests, bytes, records, tokens, or estimated compute units;
- rejected retries;
- cached responses;
- work that is cancelled or refunded;
- duplicate requests carrying one idempotency key.

Without this contract, two correct implementations can enforce different
products.

---

# 5. Fixed Windows Are Cheap but Have a Boundary Burst

Divide time into aligned windows and maintain one counter:

~~~text
key = tenant:acme:search:2026-08-17T09:00:00Z
allow if incremented count <= 10
expire key after the window
~~~

The state is one integer per active key per window. The decision is constant
time and easy to make atomic.

The weakness appears at the boundary. Acme can send ten requests at
`09:00:00.900` and ten more at `09:00:01.050`. Both windows are valid, but 20
requests arrive within 150 milliseconds.

![Two valid fixed windows create a burst at their boundary](/assets/img/distributed-rate-limiting/fixed-window-boundary.svg)

For a fixed limit `L` per window, a short interval crossing a boundary may
observe nearly `2L`. Randomizing window alignment per key spreads aggregate
load but does not change the individual contract.

Fixed windows are appropriate when low state cost matters more than smoothing,
or when the product itself is defined in calendar windows such as requests per
day.

---

# 6. Sliding Logs Enforce the Literal Window

A sliding log stores the timestamp of every admitted request. At time `now`:

1. remove timestamps `<= now - window`;
2. sum the cost of remaining entries;
3. admit if adding the request stays within the limit;
4. append the new timestamp if admitted.

![A sliding log expires exact request timestamps](/assets/img/distributed-rate-limiting/sliding-log.svg)

For the last-one-second contract, the decision is exact relative to the clock
and serialization point used by the store. The cost is state proportional to
traffic:

~~~text
memory per key = O(number of events in the window)
cleanup work   = O(expired events removed)
~~~

A sorted set can implement the log, but one hot tenant at millions of requests
per second creates millions of timestamp members. Unique member IDs are needed
when several requests share the same timestamp.

Sliding logs fit low-volume, strict limits such as password attempts. They are
usually too expensive for every request on a high-throughput API.

---

# 7. Sliding-Window Counters Approximate with Two Buckets

Keep counts for the current and previous fixed windows. If fraction `p` of the
current window has elapsed, estimate:

~~~text
estimated = current_count + previous_count × (1 - p)
~~~

At 25% into the window, retain 75% of the previous count.

![A weighted previous bucket approximates a sliding window](/assets/img/distributed-rate-limiting/sliding-window-counter.svg)

This reduces state to two counters per key and smooths the fixed-window edge.
It is still an approximation: it assumes previous-window traffic was evenly
distributed. If those requests were concentrated at one edge, the estimate may
temporarily overcount or undercount.

More sub-windows improve accuracy at additional storage and update cost. For
example, sixty one-second buckets approximate one minute more closely than two
one-minute buckets.

---

# 8. Token Buckets Encode Sustained Rate and Burst

A token bucket stores:

~~~text
capacity B
refill rate r tokens/second
current tokens T
last refill time t_last
~~~

For a request of cost `c` at time `t`:

~~~text
T_refilled = min(B, T + r × max(0, t - t_last))

if T_refilled >= c:
    T_next = T_refilled - c
    allow
else:
    T_next = T_refilled
    deny
~~~

![A token bucket permits a bounded burst and refills continuously](/assets/img/distributed-rate-limiting/token-bucket.svg)

No background timer is required. Refill can be calculated lazily when the next
request arrives. Use fixed-point integers—for example microtokens and monotonic
nanoseconds—to avoid floating-point disagreement and fractional-token loss.

The wait until cost `c` becomes available is:

~~~text
retry_after = max(0, c - T_refilled) / r
~~~

Token buckets are a strong default for APIs because `r` describes sustainable
load while `B` makes ordinary client bursts usable.

---

# 9. Leaky Buckets Shape Output Instead of Saving Burst Credit

The phrase **leaky bucket** is used for related algorithms. The queue form puts
accepted work into a bounded queue drained at a constant rate.

![A leaky-bucket queue turns bursty arrivals into steady departures](/assets/img/distributed-rate-limiting/leaky-bucket.svg)

If arrivals exceed the drain rate:

- queueing delay rises;
- the queue eventually fills;
- later requests are rejected or dropped.

This is traffic shaping, not merely policing. It is useful when a downstream
system needs smooth work arrival, but queued requests consume memory and caller
deadlines. A rate limiter that returns immediately should usually reject rather
than hide overload in an unbounded queue.

Token bucket and leaky queue can be combined: a token bucket admits a bounded
burst, then a concurrency or queue limit protects slow downstream work.

---

# 10. GCRA Represents the Same Envelope as a Virtual Schedule

The Generic Cell Rate Algorithm stores a theoretical arrival time `TAT`. Let:

~~~text
I = 1 / rate                  emission interval per unit
τ = burst tolerance          derived from burst capacity
~~~

A request at time `t` is conforming when:

~~~text
t >= TAT - τ
~~~

If admitted:

~~~text
TAT = max(t, TAT) + cost × I
~~~

![GCRA compares real arrival time with a virtual schedule](/assets/img/distributed-rate-limiting/gcra-timeline.svg)

GCRA needs one timestamp-like value per key and naturally calculates a retry
time. It is mathematically related to a token bucket, but its virtual-schedule
form can be convenient for atomic storage scripts. Token bucket is usually the
easier mental model; GCRA is worth recognizing when reading production limiter
implementations.

---

# 11. A Local Token Bucket Is Fast

Within one process, protect the state with one mutex or assign each key to one
event-loop shard. A compact C++ sketch is:

~~~cpp
struct Decision {
    bool allowed;
    std::uint64_t remaining_microtokens;
    std::chrono::nanoseconds retry_after;
};

class TokenBucket {
public:
    Decision allow(std::uint64_t cost, Clock::time_point now) {
        std::lock_guard lock(mu_);
        refill(now);

        if (tokens_ < cost) {
            return {false, tokens_, waitFor(cost - tokens_)};
        }

        tokens_ -= cost;
        return {true, tokens_, std::chrono::nanoseconds::zero()};
    }

private:
    std::mutex mu_;
    std::uint64_t tokens_;       // fixed-point microtokens
    Clock::time_point updated_;
};
~~~

The refill, comparison, and deduction are one critical section. Splitting them
allows concurrent requests to spend the same token.

Use a monotonic clock for elapsed time inside one process. Wall-clock steps
must not create tokens or move refill backward.

---

# 12. Local Buckets Do Not Form One Global Limit

Suppose six gateways each hold a full 40-token bucket for Acme:

~~~text
maximum immediate admission = gateways × local capacity
                            = 6 × 40
                            = 240 requests
~~~

![Independent local buckets multiply the global burst budget](/assets/img/distributed-rate-limiting/local-overshoot.svg)

Even if the 40 tokens are divided evenly, skew causes false rejection. Gateway
`G1` may exhaust its allocation while `G5` remains idle with unused tokens.

Local-only limiting is correct when the contract is explicitly per instance,
or when its purpose is coarse overload protection. It is not a strict tenant-
global quota unless the overshoot is part of the documented bound.

---

# 13. A Shared Decision Must Be Atomic

A central rate-limit service can serialize the read-refill-decide-update step
for Acme's key. Redis is a common state store because a script executes this
sequence atomically on the owning shard.

One compact token-bucket script has this shape. Here time is milliseconds,
tokens use fixed-point integer units, and `rate` is microtokens per millisecond:

~~~lua
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl_ms = tonumber(ARGV[5])

local state = redis.call('HMGET', KEYS[1], 'tokens', 'updated_ms')
local tokens = tonumber(state[1]) or capacity
local updated_ms = tonumber(state[2]) or now_ms

local elapsed = math.max(0, now_ms - updated_ms)
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated_ms', now_ms)
redis.call('PEXPIRE', KEYS[1], ttl_ms)
return {allowed, tokens}
~~~

![All gateways use one atomic decision on the key's owner](/assets/img/distributed-rate-limiting/atomic-shared-decision.svg)

The real script must define integer units, missing-state behavior, TTL, clock
source, maximum elapsed time, and response fields. Passing client wall time is
dangerous if clients disagree or can manipulate it. Prefer a trusted service or
store time source and clamp impossible elapsed values.

Script atomicity is local to one Redis shard. Every key needed for one atomic
decision must route to that shard, often using a hash tag.

---

# 14. The Synchronous Global Path Has a Cost

With a per-request global check:

~~~text
client -> gateway -> rate-limit service -> state shard -> decision -> gateway
~~~

The limiter adds network latency and one more dependency before the application
can run. Its capacity must cover the request rate of every protected service,
including rejected traffic during attacks.

![A global decision service becomes part of every request's latency path](/assets/img/distributed-rate-limiting/global-service-path.svg)

Bound the call with a short timeout and reuse connections. The gateway needs a
defined policy for timeout, malformed response, and unavailable state shard.
Retrying a timed-out decision is subtle: the first attempt may already have
deducted tokens. A unique decision ID or conservative no-retry policy avoids
double charging, but the correct choice depends on whether the quota is a
safety boundary or an overload heuristic.

Coarse local protection can reject obvious floods before they overload the
global service.

---

# 15. Shard by the Complete Limit Key

One state node cannot hold every tenant. Hash the canonical descriptor:

~~~text
hash(tenant=acme | operation=search | plan=v7) -> shard 12
~~~

All gateways must use the same key encoding and current shard map. The state
for one atomic decision remains on one owner.

![Rate-limit keys route through a versioned shard map](/assets/img/distributed-rate-limiting/sharded-state.svg)

Rebalancing needs the same care as any stateful partition move. If old and new
owners both accept deductions without coordination, quota is duplicated. Safe
options include:

- briefly stop decisions for the moving range;
- transfer state and switch routing with an epoch;
- forward old-owner requests to the new owner;
- use a storage system whose own resharding preserves single-key atomicity.

The [sharding guide]({% post_url distributed-systems/2022-01-12-sharding %})
develops partition maps, epochs, and live migration in detail.

---

# 16. Global Keys Become Hot Keys

Per-user keys distribute naturally. A global protection key such as
`destination=search-cluster` receives every request and remains on one shard.
Adding replicas does not make concurrent mutations to that one counter
independent.

![A global quota key concentrates traffic on one state owner](/assets/img/distributed-rate-limiting/hot-key.svg)

Options include:

- local coarse rejection before the global check;
- batching several units into one atomic deduction;
- partitioning entitlement into bounded local allocations;
- using a dedicated in-memory owner for the hottest key;
- enforcing a concurrency limit closer to the protected dependency;
- relaxing exactness and reconciling distributed counters.

Randomly striping a strict counter across `N` keys loses the ability to reject
exactly at the global boundary unless requests coordinate across all stripes.
Striping trades strictness for throughput; quantify that trade.

---

# 17. Entitlement and Scheduling Fairness Are Different

A per-tenant bucket stops one tenant from spending another tenant's contractual
rate. It does not guarantee fair service after admission. Acme may admit ten
expensive requests just before Birch admits ten cheap ones; a FIFO worker queue
can still make Birch wait behind all of Acme's work.

![Per-tenant admission and fair scheduling protect different boundaries](/assets/img/distributed-rate-limiting/tenant-fairness.svg)

A shared multi-tenant service often needs both:

- a bucket per tenant or plan for entitlement;
- cost-weighted tokens so expensive work is not counted as one cheap request;
- a tenant aggregate limit above individual user limits;
- per-tenant concurrency caps;
- a scheduler such as weighted fair queueing or deficit round robin;
- a global overload limit that may reduce everyone's admission proportionally.

Equal request counts are not equal resource shares when request cost differs.
Define policy weights from the product contract, then measure actual CPU, I/O,
memory, and latency to find tenants whose estimated cost is systematically low.

The limiter decides **whether** a request may enter. The scheduler decides
**which admitted request runs next**.

---

# 18. Hierarchical Limiting Protects Multiple Boundaries

One request can consume several budgets:

~~~text
edge IP defense             1 request
tenant Acme search rate     3 search units
Acme account aggregate      3 units
search-cluster protection   1 request
monthly purchased quota     3 units
~~~

![One request is evaluated against layered policy descriptors](/assets/img/distributed-rate-limiting/hierarchical-limits.svg)

Evaluate the cheapest and broadest defense first. The final decision is deny if
any mandatory limit is over quota.

Atomicity across unrelated keys is difficult. Deducting sequentially may spend
an outer budget before a later inner budget rejects. Possible contracts are:

- accept conservative leakage and size it;
- colocate related keys and update them atomically;
- check hard purchased quota first and treat protection limits as independent;
- reserve all costs under one transaction in a database;
- use compensation for approximate accounting, never for a safety-critical
  overload boundary.

Do not promise one atomic multi-dimensional quota if the state model cannot
provide it.

---

# 19. Local Plus Global Enforcement Reduces Load

A two-stage path uses:

1. a local token bucket to absorb abusive bursts cheaply;
2. a global service for the precise tenant-wide policy.

![A local coarse bucket shields the global fine-grained limiter](/assets/img/distributed-rate-limiting/local-global-stages.svg)

The local stage must not reject legitimate traffic merely because one instance
receives more than its share, unless that false-rejection risk is acceptable.
It is often configured looser than the global contract and used primarily as a
fuse.

Alternatively, the global service can give each gateway a small batch of
spendable tokens. Requests spend locally until the batch is low, then obtain
another allocation. This removes the global round trip from most requests but
turns unused allocations and failure recovery into quota-accounting problems.

---

# 20. Static Regional Quotas Are Predictable but Wasteful

For a global 1,000 requests/second limit, allocate:

~~~text
eu-west     500/s
us-east     400/s
ap-south    100/s
sum       1,000/s
~~~

![Static regional shares preserve the global cap but strand capacity](/assets/img/distributed-rate-limiting/static-regional-quota.svg)

Each region enforces independently, so a cross-region partition does not cause
overshoot. But if Europe needs 650/s while Asia is idle, Europe rejects traffic
despite 100/s of unused global entitlement.

Operators can move shares through configuration, but slow propagation creates
an overlap hazard. Do not activate `eu-west=600` before `ap-south=0` is known to
have relinquished its old 100. Versioned handoff or expiring allocations avoid
double assignment.

---

# 21. Leased Quota Buys Latency with Bounded Overshoot

A global allocator grants each region a bounded number of tokens for an epoch:

~~~text
allocation A-71: eu-west  300 tokens, expires 09:00:05
allocation A-72: us-east  200 tokens, expires 09:00:05
~~~

Regions spend locally and report usage. The allocator replenishes or
rebalances allocations according to demand.

![A global allocator leases bounded token batches to regions](/assets/img/distributed-rate-limiting/leased-quota.svg)

The allocator never gives out more live entitlement than the global budget it
is willing to risk. A region must stop spending an expired allocation, and an
allocation identity prevents replaying an old grant.

If allocation state can be lost or duplicated, the overshoot bound includes
every outstanding grant. With `R` regions and maximum unreported allocation
`q_i` per region:

~~~text
maximum unobserved spend <= Σ q_i
~~~

Smaller batches tighten the bound but increase allocator traffic. Larger
batches improve latency and partition tolerance but strand more capacity and
permit more unreported spend.

---

# 22. Rebalancing Quota Needs an Epoch

Suppose Europe is busy and Asia is idle. The allocator wants to transfer 80
tokens from `ap-south` to `eu-west`.

A safe handoff is:

1. stop renewing Asia allocation `A-72` or revoke it if reachable;
2. learn its final spend or wait until the allocation can no longer be used;
3. record the returned/expired amount;
4. issue Europe a new allocation in a greater epoch;
5. reject usage reports carrying an invalid allocation identity or epoch.

![Quota moves only after the old allocation is closed or bounded](/assets/img/distributed-rate-limiting/quota-rebalance.svg)

This is the lease-and-fencing pattern from the
[distributed locks guide]({% post_url distributed-systems/2026-08-17-distributed-locks-leases-fencing %}),
applied to spendable capacity. The scarce resource is not exclusive ownership;
it is a divisible number of tokens.

During an allocator partition, a region may continue only within its existing
unexpired allocation. That yields bounded autonomy instead of an unbounded
fail-open policy.

---

# 23. Derive the Overshoot Budget

Approximation should be a number, not a vague warning. Consider:

~~~text
N  = number of independent enforcement points
b  = maximum local unreported token batch
Δ  = propagation or report interval
rᵢ = admission rate at point i
~~~

Two useful upper bounds are:

~~~text
batch bound       <= N × b
delay bound       <= Σ(rᵢ × Δ)
combined bound    <= already-issued batches + in-flight decisions
~~~

![Local batches and reporting delay form the overshoot budget](/assets/img/distributed-rate-limiting/overshoot-budget.svg)

The exact expression depends on whether allocations expire, whether reports are
cumulative, and whether failover can replay grants. Include retries and requests
already admitted but not yet observed.

For overload protection, translate overshoot into downstream queueing and
latency. For paid quota, translate it into maximum unbilled cost. The business
consequence determines whether the bound is acceptable.

---

# 24. Weighted Requests Need Reservation Semantics

Not every request costs one token. Acme's search cost may depend on requested
work:

~~~text
metadata lookup       1 unit
full-text search      3 units
vector rerank        10 units
export job           100 units
~~~

![Weighted operations consume different amounts from the same bucket](/assets/img/distributed-rate-limiting/weighted-cost.svg)

If the true cost is known before work starts, deduct it atomically. If cost is
known only afterward:

1. reserve a conservative maximum;
2. execute the work;
3. commit actual usage and return the unused reservation;
4. expire abandoned reservations;
5. make settlement idempotent by request ID.

Do not let a negative adjustment create tokens beyond bucket capacity. For a
streaming response, enforce incremental budget as units are produced and stop
cleanly when the budget is exhausted.

Weighted limits are vulnerable to underestimation. Protect the downstream with
concurrency and resource limits even when quota accounting is correct.

---

# 25. Rate, Concurrency, and Queue Limits Work Together

At 10 requests/second:

~~~text
50 ms average service time  -> about 0.5 requests in flight
5 s average service time    -> about 50 requests in flight
~~~

Little's Law connects average concurrency `L`, admitted rate `λ`, and average
time in system `W`:

~~~text
L = λW
~~~

![Rate limits arrivals while concurrency limits accumulated work](/assets/img/distributed-rate-limiting/rate-vs-concurrency.svg)

A rate limit alone cannot contain a latency spike. Use:

- rate limits for traffic entitlement and sustained arrival control;
- concurrency limits for memory, connections, and in-flight work;
- bounded queues for short scheduling gaps;
- circuit breakers and load shedding when dependencies degrade.

Retry traffic consumes capacity too. Honor `Retry-After`, add jitter, and avoid
synchronized retry storms at the reset boundary.

---

# 26. Failure Policy Must Be Per Limit

When the rate-limit service times out, the gateway must choose deliberately.

![Different rate-limit purposes require different failure behavior](/assets/img/distributed-rate-limiting/failure-policy.svg)

| Limit purpose | Typical fallback |
|---|---|
| Volumetric abuse defense | Fail closed or use a restrictive local fallback |
| Protect fragile database | Fail closed, shed, or enforce local concurrency |
| Product-tier fairness | Use bounded local allowance and reconcile |
| Paid hard quota | Fail closed or consume preallocated durable entitlement |
| Non-critical analytics cap | Fail open with telemetry may be acceptable |

"Fail open" should never mean unlimited. A local emergency bucket can bound
admission while the global service is unavailable. "Fail closed" also needs a
recovery plan; otherwise a limiter outage becomes a total application outage.

Differentiate:

- timeout before any decision is known;
- explicit over-limit response;
- invalid policy configuration;
- state-shard unavailability;
- stale local allocation;
- complete control-plane outage.

---

# 27. Return a Useful Rejection

HTTP defines status `429 Too Many Requests`. A response may include
`Retry-After` with either a delay in seconds or an HTTP date.

~~~http
HTTP/1.1 429 Too Many Requests
Content-Type: application/json
Retry-After: 2

{"error":"rate_limit_exceeded","policy":"acme-search-v7"}
~~~

![The decision carries retry timing and policy metadata back to the client](/assets/img/distributed-rate-limiting/rejection-response.svg)

Return enough information for a cooperative client without exposing sensitive
system capacity. A `remaining` value is a snapshot and may already be stale
when several requests are concurrent. `Retry-After` should derive from the
actual algorithm rather than a fixed guess.

Do not cache tenant-specific 429 responses in a shared cache unless the cache
key and caching policy are explicitly safe.

---

# 28. Configuration Is Part of Correctness

Policy changes arrive while requests are in flight. A gateway may evaluate
`plan=v7` while the global service has already moved to `v8`.

![Versioned policy rollout keeps decisions attributable](/assets/img/distributed-rate-limiting/policy-rollout.svg)

Every decision should identify the policy version. Define transitions:

- lowering capacity: clamp stored tokens to the new capacity;
- raising capacity: decide whether existing buckets receive the increase
  immediately;
- changing refill rate: refill under the old rate up to the transition point,
  then use the new rate;
- changing key shape: dual-read or explicitly start new state;
- deleting a policy: choose allow, deny, or fallback behavior.

Distribute immutable snapshots, validate them before activation, and retain a
fast rollback. A partial configuration rollout can be more damaging than state
store failure because it produces plausible but inconsistent decisions.

---

# 29. Observability Without Cardinality Explosion

Track:

- allowed, denied, and shadow-denied decisions by policy;
- decision latency and timeout rate;
- tokens remaining distribution;
- retry-after distribution;
- local fallback and fail-open/closed decisions;
- allocation size, utilization, expiry, and stranded quota;
- estimated overshoot and reconciliation corrections;
- state-shard CPU, memory, hot keys, and script latency;
- configuration version skew;
- downstream latency and rejection correlation.

Do not attach raw tenant IDs to every metric in a large multi-tenant system.
Use logs or sampled traces for high-cardinality diagnosis and bounded labels for
aggregate metrics.

Shadow mode calculates decisions but does not enforce them. It reveals which
clients would be rejected before a policy launch.

![Shadow evaluation compares proposed policy with real traffic](/assets/img/distributed-rate-limiting/shadow-mode.svg)

Shadow mode still consumes limiter capacity and state. Protect the shadow path
from becoming the outage it is meant to prevent.

---

# 30. Test Time, Concurrency, and Failure

Use a controllable clock for algorithm tests. Avoid sleeping in unit tests.

Verify:

1. empty, full, and exactly depleted buckets;
2. fractional refill and long idle periods;
3. time that stays equal or moves backward at an input boundary;
4. many concurrent requests for one token;
5. fixed-window traffic immediately before and after reset;
6. sliding-log expiry at the exact boundary;
7. weighted requests larger than bucket capacity;
8. lost decision replies and retry behavior;
9. state-shard failover during an atomic deduction;
10. shard-map changes with a hot key in flight;
11. regional allocation expiry during a partition;
12. quota rebalancing with delayed usage reports;
13. policy rollout where old and new versions overlap;
14. rate-limit-service overload caused only by rejected traffic;
15. retry storms after a shared `Retry-After` interval.

The load test must use skewed keys. Uniform random tenants hide the hot-key
behavior that dominates production.

---

# 31. The Complete Acme Request

Now follow one search request costing three units:

1. The edge applies a coarse source-network bucket before authentication.
2. Authentication resolves tenant `acme` and plan version `v7`.
3. The gateway builds canonical descriptor `acme|search|v7`.
4. A loose local bucket rejects obvious floods without a network call.
5. The gateway's `eu-west` allocation `A-71` still has 18 tokens.
6. It atomically spends three locally, leaving 15.
7. A per-destination concurrency limiter reserves one search slot.
8. The request executes and returns successfully.
9. The gateway reports cumulative spend for `A-71` to the allocator.
10. When the allocation falls below its threshold, the gateway requests more.
11. The allocator observes Europe is busy and safely transfers expired quota
    from Asia in the next epoch.
12. If no allocation remains, the gateway returns 429 with calculated retry
    timing rather than exceeding its bounded entitlement.

![The end-to-end request crosses local, regional, and global controls](/assets/img/distributed-rate-limiting/end-to-end.svg)

The common path needs no cross-region round trip. The global allocator controls
the amount of live spendable quota. Local concurrency still protects the search
cluster if requests become slow.

---

# 32. Algorithm and Architecture Guide

| Requirement | Useful starting point |
|---|---|
| Calendar quota with minimal state | Fixed-window counter |
| Exact low-volume rolling limit | Sliding log |
| Approximate rolling limit | Sliding-window counter |
| Sustained rate plus allowed burst | Token bucket or GCRA |
| Smooth downstream departures | Leaky queue plus bounded waiting |
| Strict multi-instance decision | Atomic shared state per key |
| Very hot approximate global limit | Batched or leased local quota |
| Multi-region bounded autonomy | Expiring regional allocations |
| Durable purchased quota | Transactional ledger/reservation, often separate from request-rate protection |

For any design, ask:

1. What exact traffic envelope is promised?
2. What is the canonical key and who controls its cardinality?
3. What clock and serialization point define the decision?
4. Is refill/check/deduct atomic?
5. Is the limit local, regional, or global?
6. What is the calculated overshoot bound?
7. How are hot keys handled?
8. What happens during state-store, allocator, and network failure?
9. How do rate, concurrency, and durable quota interact?
10. What does a rejected client receive?
11. How are policy versions rolled out and attributed?
12. Can shadow traffic and rejected traffic overload the limiter itself?

---

# 33. Final Mental Model

The complete system separates several concerns:

~~~text
algorithm        -> shape of one key's allowed traffic
atomic owner     -> no concurrent double-spend for that key
shard map        -> route each key to its state owner
local fuse       -> protect the global decision path
quota allocation -> trade coordination frequency for bounded overshoot
concurrency      -> contain slow accumulated work
failure policy   -> choose bounded behavior when coordination is unavailable
policy version   -> make every decision explainable
~~~

A distributed rate limiter is not merely a counter in Redis. It is an admission
control system whose quality is determined by its traffic contract, atomicity
boundary, distribution strategy, overshoot budget, and failure behavior.

---

# References

- [Redis rate-limiter documentation](https://redis.io/docs/latest/develop/use-cases/rate-limiter/)
- [Redis algorithm comparison and examples](https://redis.io/learn/howtos/ratelimiting/)
- [Envoy global rate-limiting architecture](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/other_features/global_rate_limiting.html)
- [Envoy rate-limit service API](https://www.envoyproxy.io/docs/envoy/latest/api-v3/service/ratelimit/v3/rls.proto)
- [RFC 6585: 429 Too Many Requests](https://www.rfc-editor.org/rfc/rfc6585.html#section-4)
