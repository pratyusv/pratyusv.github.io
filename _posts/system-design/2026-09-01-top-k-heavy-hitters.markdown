---
title: "Designing Top-K Most-Played Songs in a Sliding Window"
date: 2026-09-01 09:00:00 +0000
description: "Maintain exact Top-K playback counts over a sliding window using Kafka, time buckets, partitioned workers, local ranking merges, and replay-safe recovery. Includes hot keys and approximate counting."
categories:
  - "System Design"
tags: [system-design, heavy-hitters, top-k, streaming, sketches, analytics, resiliency]
---

Design a service that returns the most-played songs over the last five minutes.
Each accepted playback adds one to a song's count. Results update once per
second and include the songs' play counts and the time interval they cover.

At high traffic, the service must distribute counting across machines, expire
old plays, combine local rankings, and recover after failures. The design
starts with exact counts and introduces approximation when the keyspace makes
exact state too expensive.

## Requirements

| Requirement | Target |
|---|---|
| Ranking | Top K songs, K from 1 to 10; default 10. |
| Counting rule | One count per accepted unique playback. |
| Window | Last five minutes, advancing at one-second boundaries. |
| Filters | Global and configured country/language combinations. |
| Playback traffic | Example capacity target: 1 million records/second, with bursts up to 3 million. |
| Query latency | p99 below 50 ms. |
| Freshness | Publish within five seconds of the window ending during normal operation. |
| Delayed input | Two-second out-of-order allowance; later records go to offline correction. |
| Failure behavior | Serve the last complete result and its age. |

The traffic figures are sizing assumptions. Actual capacity depends on the
number of distinct songs, filter combinations, and retained bucket entries.

At `10:05:00`, the window is `[10:00:00, 10:05:00)`. It includes plays at the
left boundary and excludes plays at the right boundary. The next result covers
`[10:00:01, 10:05:01)`. Equal counts are ordered by song ID.

## Playback Records and Query API

An upstream playback service writes a record whenever a song is played:

```json
{
  "event_id": "play-7821",
  "song_id": "song_101",
  "event_time": "2026-09-01T10:04:21.481Z",
  "country": "IN",
  "language": "hi"
}
```

`event_id` identifies this particular play. If sending the record times out,
the playback service can send it again with the same ID. The counting service can then
recognize the repeated record and count the play once. A new play gets a new ID.

`song_id` identifies the song, and `event_time` determines which time window
includes the play. The country and language select the relevant leaderboards.
A configured filter combination is a **scope**. For example,
`country=IN|language=hi` is one scope.

The query API returns a precomputed ranking:

```http
GET /rankings?metric=recent_plays&country=IN&language=hi&window=5m&k=2
```

```json
{
  "metric": "recent_plays",
  "window_start": "2026-09-01T10:00:00Z",
  "window_end": "2026-09-01T10:05:00Z",
  "published_at": "2026-09-01T10:05:02.600Z",
  "mode": "exact",
  "items": [
    {"song_id": "song_101", "plays": 700},
    {"song_id": "song_303", "plays": 620}
  ]
}
```

`window_end` tells the caller how recent the counted plays are. `published_at`
identifies when that result became available. During a processing delay, these
timestamps can be several seconds apart.

## System Architecture

The ingestion API validates each record and confirms acceptance after Kafka
durably records it. Counting workers maintain window totals and publish local
rankings to a retained summary topic. Merger workers select the global result,
write it to Redis, and make it available to the Query API.

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/distributed-system-overview.svg" alt="The playback service sends records to an ingestion API, which validates them and writes them to Kafka. Counting workers consume Kafka partitions and write local Top-K results to a summary topic. Merger workers build complete global rankings in Redis. Query API instances return those rankings to readers." caption="The full event lifecycle: ingest and retain records, count by partition, merge matching windows, publish to Redis, and serve the completed ranking." %}

A **worker** is a running instance of the counting program. Its count maps,
time buckets, and ranking index live in memory or a local state store. A
**merger**, also called a reducer, collects the local lists and selects the
largest K counts for one scope and window endpoint.

Kafka retains records independently of the workers. A replacement worker can
restore its saved state and replay records after that snapshot. The Query API
reads completed Redis objects, allowing query traffic to scale independently
of counting and merge traffic.

## 1. Maintain Counts and a Local Ranking

For one scope, keep a hash map from song ID to play count. For example,
after receiving six plays:

```text
Incoming song IDs: song_101, song_202, song_101, song_303, song_303, song_101

Counts:
song_101 -> 3
song_202 -> 1
song_303 -> 2
```

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/local-counting.svg" alt="A playback of song_101 changes its count from two to three. The worker sorts song_101=3, song_303=2, song_202=1 and publishes song_101 and song_303 as Top-2." caption="A count map handles incoming plays. Sorting the counters produces the initial local leaderboard." %}

Each new playback adds one to the corresponding counter. A first
implementation can sort these entries once per second, take the first ten,
and publish that list. If D songs have appeared, the count map uses `O(D)`
space, each increment takes `O(1)` expected time, and sorting costs
`O(D log D)` per publication.

The count map produces cumulative playback totals. A five-minute leaderboard
also needs to remove plays as they become five minutes old.

### Track When Plays Expire

Group plays into time buckets. A bucket stores a count per song for one
interval. For example, this bucket contains plays between `10:04:00` and
`10:05:00`:

```text
bucket[10:04] = {
    song_101: 300,
    song_202: 180,
    song_303: 40
}
```

The worker also maintains `window_counts`, the total for each song across all
buckets in the current window. Using one-minute buckets to illustrate a
five-minute window:

| Bucket | song_101 | song_202 | song_303 |
|---|---:|---:|---:|
| 10:00–10:01 | 120 | 80 | 30 |
| 10:01–10:02 | 150 | 90 | 50 |
| 10:02–10:03 | 100 | 120 | 200 |
| 10:03–10:04 | 30 | 100 | 300 |
| 10:04–10:05 | 300 | 180 | 40 |
| **Total at 10:05** | **700** | **570** | **620** |

For K=2, the answer is `song_101: 700` followed by `song_303: 620`.

When the window advances to `10:06`, subtract the expired 10:00 bucket and add
the completed 10:05 bucket. Suppose the new bucket has 20, 50, and 200 plays:

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/window-expiration.svg" alt="A five-minute window moves from endpoint 10:05 to 10:06. song_101 changes from 700 to 600, song_202 from 570 to 540, and song_303 from 620 to 790 by subtracting the expired minute and adding the new minute." caption="Only the entering and expiring buckets change the totals. The updated counts can change the ranking." %}

The new Top-2 is `song_303: 790`, then `song_101: 600`. Window advancement
requires reading the entering and expiring buckets rather than the original
playback records.

For the required one-second updates, use 300 one-second buckets. At endpoint
T, `window_counts` contains the sum over `[T - 300s, T)`. To advance one second:

```text
advance_window(T):
    for song, count in buckets[T - 300s]:
        window_counts[song] -= count

    for song, count in buckets[T]:
        window_counts[song] += count

    remove counters that reached zero
    publish_top_k(window_end=T + 1s)
```

`buckets[T]` contains records with timestamps in `[T, T + 1s)`. Incoming
records update their buckets; the worker adds a completed bucket to the running
window when it advances to that endpoint. Publication waits for delayed
records as described below.

Store buckets in a ring buffer so expired slots can be reused. Each slot also
stores its bucket timestamp, allowing the worker to distinguish an old delayed
record from a record belonging to the slot's new interval. Retain additional
open buckets for the two-second arrival allowance.

Because the endpoints fall on whole seconds, the included buckets exactly
cover each requested window. An arbitrary endpoint such as `10:05:00.450`
would require keeping individual timestamps for the two partial boundary
seconds or accepting a time-boundary approximation.

### Avoid Sorting Every Song on Every Update

If sorting all counters once per second becomes expensive, maintain an ordered
index alongside `window_counts`. A balanced search tree can store entries
ordered by descending count and then ascending song ID. Using the totals
from the example window ending at 10:05:

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/ranking-index.svg" alt="At 10:05 the ranking is song_101=700, song_303=620, song_202=570. After bucket expiration and addition, the 10:06 ranking is song_303=790, song_101=600, song_202=540." caption="A balanced tree keeps the ranking current as counts increase or decrease. Each changed count replaces its old index entry." %}

When a song's count changes, remove its old index entry, change the count, and
insert the updated entry. Remove the song from both structures when its count
reaches zero. Each change costs `O(log D)`, and reading K entries costs
`O(log D + K)` with a balanced tree.

When the example advances to 10:06, update all three entries using the bucket
changes. The index becomes `(790, song_303), (600, song_101), (540, song_202)`.
Reading its first two entries returns the new leaderboard.

Keep every active song in the index. When expiration lowers a current winner's
count, the next song in order can take its place. A heap storing only the K
winners would need another structure or a scan of the counts to find that
replacement.

## 2. Distribute the Counting Work

As counting exceeds one worker’s CPU or memory capacity, different workers
can maintain different groups of song counters.

### Partitions and Workers

A **partition** is a numbered group of counters and their associated buckets
and ranking entries. A **worker** is the process that maintains one or more
of those groups.

For example, three partitions can be assigned to two workers:

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/partitions-and-workers.svg" alt="Worker 1 stores partitions 0 and 1, while Worker 2 stores partition 2. Partition 0 contains song_101, song_404, song_707; partition 1 contains song_202, song_505, song_808; partition 2 contains song_303, song_606, song_909." caption="Partition IDs describe groups of counting state. Workers are the processes that store and update those groups." %}

Worker 1 keeps separate counting state for partitions 0 and 1. Worker 2 keeps
the state for partition 2. The partition IDs remain stable when their state moves
to another process.

Choose a song's partition using a deterministic hash:

```text
partition_id = hash(scope, song_id) % partition_count
worker = assignments[partition_id]
```

The hash produces an integer; the remainder chooses a partition. For three
partitions, the result is 0, 1, or 2. The assignment table then identifies the
worker that receives the update. A coordinator maintains this assignment
table and arranges transfers when workers join or fail.

For example, if `song_202` in the India/Hindi scope maps to partition 1:

```text
Playback of song_202
    -> partition 1
    -> Worker 1
    -> increment song_202 in partition 1's event-time bucket
```

Including the scope keeps separate counters for the same song in different
leaderboards. All updates for a particular `(scope, song_id)` reach one active
worker, giving that worker the complete count for that song and scope.

In a simple deployment, Kafka input partitions and counting partitions can
share this assignment. The ingestion API chooses the Kafka partition using
the same hash mapping, and each counting worker reads its assigned input
partitions. For multiple scopes, expand a play into one counter update per
matching scope before routing. A later design can introduce an aggregation
stage between raw playback ingestion and the counting partitions.

### Add Workers by Moving Partitions

If Worker 1 is overloaded, start Worker 3 and transfer partition 1 to it:

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/partition-transfer.svg" alt="Before a transfer Worker 1 owns partitions 0 and 1 and Worker 2 owns partition 2. After state transfer Worker 3 owns partition 1. Its counts, buckets, ranking index, and input position move together." caption="Moving a partition redistributes state while preserving the counter-to-partition mapping." %}

The transfer includes counts, buckets, ranking entries, and input progress.
Once Worker 3 is ready, route partition 1's updates to it. Recovery coordination
ensures one active worker handles that partition at a time.

The partition count sets the maximum number of workers that can share this
stage. In practice, configure more partitions than workers so they can be
redistributed as the fleet grows. Increasing the partition count itself
requires redistributing keys and their state.

### Merge Local Top-K Lists

Each worker calculates a Top-K list for each partition it handles. For K=2:

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/exact-top-k-merge.svg" alt="Partitions 0, 1, and 2 send their Top-2 lists. song_707=10, song_808=9, and song_909=8 are omitted. The merger ranks six candidates and returns song_303=790 and song_101=600." caption="Each partition sends only its two leading songs. Because every song has one complete count, the union contains the global winners." %}

These counts extend the example window ending at 10:06 with more songs. The merger
receives six entries, selects the largest two, and returns `song_303=790`,
`song_101=600`.

Why can it ignore every song outside a partition's Top-K? An omitted song has
at least K songs ahead of it in its own partition. Those same K songs also
rank ahead of it globally. It therefore cannot enter the global Top-K. This
argument uses the same tie ordering on every worker.

For P partitions, the merger processes at most `P × K` candidates. With 200
partitions and K=10, that is 2,000 candidates per scope per publication. A
size-K heap selects the final result in `O(PK log K)` time.

This reduction depends on each song having a complete count in one partition.
If records were assigned randomly by playback, the merger would first need
to sum each song's contributions across partitions.

The same restriction applies to regional rankings. Suppose `song_101` has 60
plays in each of two regions. Each region also has a different local winner
with 70 plays. Regional Top-1 lists omit `song_101`, even though its global
count of 120 is highest. For exact global results, combine regional counts by
song before selecting Top-K. A hierarchy of Top-K mergers works when its
branches contain disjoint sets of complete song counters.

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/regional-merge-counterexample.svg" alt="Region 1 has song_101=60 and song_202=70. Region 2 has song_101=60 and song_303=70. Both regional Top-1 lists omit song_101, whose combined count of 120 is globally highest." caption="The exact local-Top-K proof applies to complete counters. Regional or salted partial counts must be summed before candidate pruning." %}

### Deployment With Three Partitions

The deployment below connects the partition assignments to the rest of the
service. Each counting worker sends its local lists to a retained summary
topic. The merger waits for all three partitions, publishes the global result
to Redis, and the Query API serves it.

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/distributed-system-deployment.svg" alt="Kafka partitions 0 and 1 are assigned to Worker 1; Kafka partition 2 is assigned to Worker 2. Each worker maintains separate counting state for its partitions and sends local lists to a retained summary topic. A merger waits for partitions 0, 1, and 2 and publishes song_303=790 and song_101=600 to Redis for the Query API." caption="Kafka retains the input partitions; the assigned worker maintains their counting state. Local summaries pass through a durable topic before global ranking and query serving." %}

## 3. Publish a Ranking for One Agreed Window

Workers progress at different speeds. At the same wall-clock time, one may be
ready to publish the window ending at `10:05:00` while another is still
processing `10:04:59`.

Their counts cover different intervals. The merger needs a Top-K list from
every expected partition for the **same window endpoint** before publishing.

### Account for Delayed Playback Records

A play at `10:04:59.800` might arrive at `10:05:01`. Waiting briefly before
closing the window allows that record to be included.

Each input reports a **watermark**: the event timestamp through which it is
ready to finish processing under its delay policy. A common policy is to take
the largest observed event timestamp and subtract the two-second allowance.
When the watermark reaches `10:05:00`, the worker can finish that endpoint.

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/event-time-watermark.svg" alt="A play occurs at 10:04:59.800 and arrives at 10:05:01. It belongs to the window ending 10:05. With a two-second out-of-order allowance, observed event time 10:05:02 permits a watermark of 10:05:00." caption="A playback belongs to a window by event time. The arrival allowance gives delayed records time to reach the worker before the window closes." %}

A worker reading several input partitions uses their minimum watermark and
processes all preceding records before closing the window. If one input goes
quiet, source heartbeats can report its progress. Another option is to mark it
idle after a timeout and exclude it from the minimum; records that arrive
behind the closed window when it resumes follow the late-record policy.
Validate timestamps so an incorrect future timestamp cannot advance progress
prematurely.

Records arriving behind a closed endpoint go to an offline correction stream.
The live ranking is exact for unique records accepted before that endpoint
was closed. Supporting corrections to already published live windows would
require retaining and publishing revised versions of those windows.

### Send a Complete Local Result

For each scope and endpoint, the worker sends one replacement list per
partition:

```json
{
  "metric": "recent_plays",
  "partition_id": 1,
  "owner_epoch": 4,
  "sequence": 98211,
  "scope": "country=IN|language=hi",
  "window_start": "2026-09-01T10:01:00Z",
  "window_end": "2026-09-01T10:06:00Z",
  "definition_version": 3,
  "topology_version": 12,
  "items": [
    {"song_id": "song_202", "plays": 540},
    {"song_id": "song_505", "plays": 60}
  ]
}
```

`owner_epoch` identifies the current assignment of that partition to a worker.
It increases when a new worker takes over. `sequence` orders messages within
that assignment. The recovery section explains how these fields prevent stale
workers and retries from altering the result.

`definition_version` identifies the counting rules: song identity, filters,
and window settings. `topology_version` identifies the partition and routing
configuration. The merger groups compatible messages by metric, scope, and window
endpoint.

It tracks the expected partitions from that configuration. A partition with
zero plays sends an empty list, confirming that it has finished the endpoint.
Once every expected partition has contributed, the merger selects Top-K and
writes a completed global result. Keep local lists in a durable summary topic
so the merger can recover them after a crash.

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/aligned-window-merge.svg" alt="Partition 0 and partition 2 have results ending at 10:05, while partition 1 has sent a result ending at 10:06. The merger keeps separate groups and waits for partition 1 at 10:05 rather than mixing the windows." caption="A newer local result goes into a different merge group. Each published ranking contains counts for one common interval." %}

The merger may collect several endpoints at once. Taking the minimum of the
workers' watermarks indicates which endpoints can finish; it does not turn
lists for different endpoints into a common snapshot. Keep each endpoint's
lists together until its merge completes or a coordinated recovery skips it.

For 100 scopes, 200 partitions, and K=10, publication every second produces up
to 200,000 candidate records per second. Partition count, scope count, and
publication frequency determine the merger's load. Reducers can be distributed
by `(scope, window_size)` when different rankings need separate capacity.

## 4. Serve Queries From Completed Results

Each completed ranking is a small object containing the song IDs, counts, and
window metadata. Store that object in Redis under a key for its scope and
window size:

```text
ranking:recent_plays:country=IN:language=hi:5m
```

The Query API reads the object and returns its first K entries. Keeping ten
entries supports all requested values of K from 1 to 10. A larger supported K
would require retaining larger lists throughout the counting and merge stages.

Store the whole ranking as one value and replace it atomically. A request
then sees either the previous complete object or the new complete object.
The publication operation also checks the active merger's ownership generation
and rejects updates with an older window endpoint. This prevents a delayed
publisher from overwriting fresher results.

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/atomic-publication.svg" alt="The merger builds a complete ranking, checks its publisher epoch and window endpoint, and atomically replaces the Redis value. A reader sees complete version 41 or complete version 42." caption="Write the complete result atomically and guard it with publisher ownership and endpoint ordering. Failed publication leaves the previous result available." %}

For larger payloads, use immutable versioned objects and an atomic `latest`
pointer. Write and validate the object first, then change the pointer. Retain
previous versions long enough for readers that already fetched their pointer.

If one counting partition falls behind, continue returning the last completed
ranking. Calculate its age as `now - window_end`, return that metadata, and
alert when it exceeds the five-second target. This keeps the read path
available while clearly showing the processing delay.

## 5. Handle a Song With Extremely High Traffic

Partitioning distributes different songs across workers. A viral song still
sends all its updates to one counting partition. For example, `song_101` might
receive 250,000 plays/second while other songs receive only a few.

There are two ways to reduce that worker's load: combine updates before they
reach it, or split its counter and recombine the partial counts.

### Combine Plays Before Sending Them to the Counting Worker

Add an aggregation stage between raw ingestion and counting. Each input worker
combines plays for the same `(scope, song_id, second)` into a count update:

```text
2,500 playback records for song_101 during 10:04:21
    -> one update: (song_101, bucket=10:04:21, count=2500)
```

With 100 input workers, 250,000 plays for one song can become roughly 100
updates per second at its counting worker. The exact reduction depends on
how frequently input workers flush their accumulated counts.

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/pre-aggregation.svg" alt="One hundred input workers each combine about 2500 song_101 plays in a second into a bucket count update. The counting worker receives about 100 count updates per second representing the same 250000 plays." caption="Pre-aggregation reduces update traffic at the song owner while preserving the represented play count and its event-time bucket." %}

Raw playback records must be distributed across input partitions for those
workers to share the load. If every raw record for the song enters one Kafka
partition, that partition can become the earlier bottleneck. Broad ingestion
partitioning followed by routing on `(scope, song_id)` lets input processing
and counting scale separately.

Keep the second in each update so the counting worker can expire it correctly.
Deduplicate playback IDs before combining records, and make batch delivery
replay-safe: receiving the same `+2500` update twice must apply it once. A
stream-processing framework can coordinate checkpoints across these stages;
a custom implementation needs durable batch identities or a transactional
handoff.

### Split the Counter Across Several Partitions

If aggregation is insufficient, divide one song's traffic into several groups.
A small integer called a **salt** selects the group:

```text
salt = hash(event_id) % split_factor
partition = hash(scope, song_id, salt) % partition_count
```

Each assigned worker maintains a partial count. A combiner receives every
partial for that song at the same window endpoint and adds them:

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/hot-song-splitting.svg" alt="song_101 is split into four salted groups with counts 63000, 61000, 66000, and 60000. All partials reach the combiner, which reconstructs a count of 250000 before global ranking." caption="The combiner receives all salted partials for the same endpoint. It sends one complete song count to the global ranking stage." %}

Workers send these registered partials regardless of their local rank. Ranking
the partials first can discard a globally important song. The global merger
combines ordinary partitions' Top-K lists with the reconstructed counts of
split songs. Ordinary lists exclude those split songs to keep each logical
count represented once.

Splitting a song during an active window also requires its earlier unsplit
plays. Until those plays expire:

```text
song_total = remaining_unsplit_count + sum(salted_counts)
```

Maintain a versioned registry of split songs, their routing boundaries, and
the expected partials. Preserve the routing generation on records or batches
so replay makes the same choice. Coordinate the registry change with local
ranking publication; both the unsplit history and new partial counts must
reach the combiner before the new configuration publishes a result.

## 6. Control Memory Usage

The main memory cost comes from the number of distinct counters and bucket
entries, not the ten entries returned to a reader.

For a worker, let D be its active `(scope, song_id)` counters and B be its
retained `(scope, song_id, second)` bucket entries:

```text
memory ~= D * (count-map entry + ranking-index entry)
        + B * bucket-entry size
        + duplicate tracking and processing buffers
```

A song played in every second of the window has one total counter and up to
300 bucket entries. Using illustrative entry sizes:

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/memory-cost.svg" alt="The example uses 0.8 GB for ten million count-map entries, 0.48 GB for the ranking index, and 3.2 GB for one hundred million bucket entries. The subtotal is 4.48 GB, and bucket entries make up about 71 percent." caption="Count distinct counters and distinct song–second entries separately. Repeated presence across buckets can dominate memory use." %}

Actual sizes depend on key representation and data structures. Compact numeric
song IDs and packed storage can reduce overhead. More scopes multiply state:
one play may update India/Hindi, India/all-languages, and global/Hindi
leaderboards. Additional window lengths can share buckets but need separate
window totals and ranking indexes.

For a bounded song catalog, exact counts can remain practical after
partitioning. For an unbounded keyspace such as search queries, a fixed-memory
approximate design can bound the required state.

### Estimate Counts With a Count-Min Sketch

A Count-Min Sketch is a rectangular array of counters. Each row has its own
hash function. An incoming key increments one counter in every row. To estimate
a key's count, hash it into each row and take the minimum counter value.

If a song has ten plays, collisions with other songs might produce queried
counters of 13, 10, and 12. The estimate is ten. If collisions affect every
row, the estimate is higher than the true count.

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/count-min-sketch.svg" alt="For song_101 with true count ten, the three hash rows select counters 13, 10, and 12. Their minimum is ten. Other songs can collide with the selected counters and cause overestimation." caption="The sketch estimates a supplied song ID. Compatible bucket sketches can be added to estimate counts over a longer window." %}

The standard point-query bound is:

```text
true_count(x) <= estimate(x) <= true_count(x) + epsilon * N
with probability at least 1 - delta for a fixed key x
```

Here N is the total number of plays represented by the sketch. Width is about
`e / epsilon`, and depth is about `ln(1 / delta)`. These bounds are derived in
the [Count-Min Sketch literature](https://archive.dimacs.rutgers.edu/~graham/pubs/papers/encalgs-cm.pdf).

At N=300 million and epsilon=0.0001, the additive error bound is 30,000 plays.
That is an absolute error relative to total traffic. With delta=0.01, roughly
27,183 columns and five rows of 64-bit counters occupy 1.09 MB per sketch.
A 300-bucket ring would occupy about 326 MB before candidate tracking. Budget
this per scope and per counting partition using the chosen layout.

Sketches with identical sizes and hash functions can be added. Keep one per
bucket and sum the active buckets to estimate the five-minute counts. An
aggregate array can also be maintained by adding an entering bucket's array
and subtracting an expiring bucket's array.

### Keep the IDs of Candidate Songs

A sketch contains counters but no song IDs. Pair it with a candidate tracker
such as **Space-Saving**, which keeps a bounded table of named keys.

For each incoming key, Space-Saving increments its existing counter or takes
a free slot. When the table is full, a new key replaces the smallest counter
and starts at that counter's value plus one. It records the inherited value
as estimation error. With m counters and N events, keys occurring more than
N/m times are retained. The algorithm and guarantees are described in the
[Space-Saving paper](https://www.cse.ust.hk/~raywong/comp5331/References/EfficientComputationOfFrequentAndTop-kElementsInDataStreams.pdf).

For sliding windows, keep candidate tables per bucket alongside the sketches.
At publication, union the active candidate IDs, estimate each count against
the merged sketch, and select the largest K estimates. Expiring a bucket also
removes its candidate table; ordinary Space-Saving replacement counts cannot
be reversed using the exact map's subtraction procedure.

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/approximate-window.svg" alt="Each bucket has a sketch and a named candidate table. Expiration discards both. The active sketches are added, active candidate IDs are unioned, and each candidate is estimated against the combined sketch before approximate Top-K selection." caption="Sliding-window approximation needs a count summary and a candidate-discovery policy. Expiring a bucket removes both parts." %}

A useful discovery guarantee follows from using m=100 counters in every
bucket-partition. A song with more than 1% of the entire window's plays must
exceed 1% in at least one bucket-partition. It is therefore present in at least
one candidate table. Retaining the full union preserves this threshold
guarantee. Keeping only each bucket's Top-K can miss a song that is consistently
popular across the whole window.

This gives guaranteed discovery above a chosen frequency threshold and a
probabilistic count-error bound. If the tenth song falls below that threshold,
its inclusion is uncertain. To capture less frequent songs, increase the table
size and budget the additional state and candidate traffic.

A fixed-key sketch confidence bound also needs adjustment when querying many
candidates repeatedly. Measure Top-K overlap with exact offline results and
count error near the cutoff. Return approximate-mode metadata with the
algorithm and error parameters.

In streaming terminology, Top-K asks for a fixed number of largest counts;
heavy hitters are keys exceeding a frequency threshold. The approximate design
determines whether the result guarantees a fixed Top-K or discovery above a
frequency threshold.

## 7. Recover After Failures

Kafka provides retained input, but workers also need a saved copy of their
counting state. A **checkpoint** records the state and the input position it
represents:

```text
Partition 1 checkpoint
    input processed through Kafka offset 92000
    time buckets and window counts
    ranking index
    closed window endpoint
    duplicate-tracking state
    pending local-ranking publications
    counting and routing versions
```

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/checkpoint-replay.svg" alt="A checkpoint saves counts, buckets, ranking entries, and input progress through offset 92000. A replacement worker restores it, replays Kafka records 92001 onward, and resumes publishing complete window results." caption="A consistent state/input checkpoint gives the replacement worker a precise restart position. Retained Kafka input supplies the remaining records." %}

The offset is the record's position within a Kafka partition. After a crash,
the replacement restores this checkpoint and resumes from offset 92001. The
saved state and input position must describe the same point in processing.
In a multi-stage pipeline, coordinate the checkpoint across the input workers,
forwarded batches, and counting workers.

### Prevent an Old Worker From Publishing

A failed worker may still be running but temporarily disconnected. When a new
worker takes its partition, the coordinator increments the partition's
ownership generation, called an **epoch**:

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/ownership-epochs.svg" alt="Worker 1 sends an obsolete partition 1 result at epoch 3 and sequence 100. Worker 3 sends the authorized epoch 4 result at sequence 1. The merger rejects the old epoch and accepts the current owner before publishing." caption="Ownership epochs prevent delayed or disconnected old workers from changing the ranking after a replacement has taken over." %}

The merger accepts new messages only from the authorized epoch. An epoch-3
message is rejected after the takeover, even if it arrives later or has a
larger sequence number. Uncommitted contributions from the old owner are
replaced by the new owner's recovered results. Previously published complete
rankings remain available during recovery.

### Make Retries Safe at Each Boundary

Local rankings are full replacement lists keyed by partition, scope, and
window endpoint. Applying a repeated list replaces the same contribution
instead of adding its counts again. Persist pending publications with the
checkpoint or use transactional output so a crash cannot permanently lose a
window's result.

Producer retries are handled separately using `event_id`. Retain the IDs for
the supported retry and event-acceptance period, and checkpoint that tracking
state. Deduplicate before converting individual plays into aggregated counts.
When one play has already been expanded into updates for several scopes, use
`(scope, event_id)` as the duplicate-checking key so each scope receives its
contribution.
At 1 million events/second, retaining five minutes of 16-byte IDs alone costs
4.8 GB across the fleet, before map overhead. This cost should be included
unless the source guarantees unique accepted playback records.

End-to-end exactly-once processing requires replayable input plus transactional
or idempotent output. A checkpoint by itself protects only the state covered
by that checkpoint. [Flink's fault-tolerance documentation](https://nightlies.apache.org/flink/flink-docs-stable/docs/learn-flink/fault_tolerance/)
explains how consistent snapshots and replay support this recovery model.

### Recover the Merger and Rebuild History

The merger restores its checkpoint or rereads the retained local-ranking topic.
It reconstructs each incomplete endpoint's expected partition set and received
lists, then resumes publication under a new authorized publisher generation.

For a cold start, replay at least five minutes of accepted playback history
before marking a result complete. Retain enough input and local-ranking
messages to cover the expected recovery interval.

Keep raw events for offline validation and rebuilds. Historical corrections
publish a separate result version so a backfill does not add old plays to the
live counters. When song normalization or counting rules change, start a new
definition version and migrate or rebuild its state before serving it.

## 8. Operate the Service

The coordinator maintains the partition assignments, worker epochs, scope
definitions, window settings, and hot-song split registry. Workers attach the
relevant versions to their results. This lets the merger verify that it is
combining counts produced under the same rules.

Pre-aggregation and hot-song splitting produce two paths into the global
merger. Ordinary songs arrive through local
Top-K lists; split songs arrive as complete counts from combiners. The
coordinator manages assignments and versions, while checkpoint storage
preserves the processing state needed for recovery.

{% include figure.html path="assets/img/top-k-heavy-hitters/guide/distributed-system-production.svg" alt="Raw Kafka records feed input workers that deduplicate and aggregate plays by song and second. Counting workers maintain window state. Ordinary local Top-K lists flow through a summary topic, while registered hot-song partials flow through combiners. Both paths reach the global merger, then Redis and the Query API. A coordinator manages ownership and versions, and durable checkpoints support replay." caption="Pre-aggregation reduces incoming update volume. Hot-song combiners reconstruct split counts before ranking, while coordinated checkpoints and ownership rules support recovery." %}

The main operational signals are:

| Signal | What to investigate |
|---|---|
| Kafka lag by partition | Whether ingestion or counting is falling behind. |
| Active counters and bucket entries per worker | Memory growth and uneven key distribution. |
| Time spent closing buckets and updating indexes | Whether per-second processing fits the publication interval. |
| Checkpoint size, duration, and restore time | Whether workers can recover within the availability target. |
| Missing lists for a window endpoint | Which partition is delaying a global ranking. |
| `now - window_end` | The age of the window represented by the served result. |
| Partial-count completeness for split songs | Whether a combiner has every required contribution. |
| Difference from exact offline rankings | Duplicate handling, count errors, and candidate recall. |

If an endpoint remains incomplete for too long, cap the merger's buffered
state and recover from retained summaries or coordinate a jump to a newer
complete endpoint. Keep the last complete ranking available throughout.

## Related Design: Active Listeners

The [active-listener leaderboard]({{ '/blog/2026/top-k-active-listeners/' | relative_url }})
ranks songs by the number of users listening now. That design tracks user
sessions, heartbeats, stops, and device transfers.
