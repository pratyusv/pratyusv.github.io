---
title: "Designing the Top K Songs by Active Listeners"
date: 2026-09-02 09:00:00 +0000
description: "Follow a music leaderboard from start and stop events to a Redis design, then develop retry safety, unique-user counting, recovery, and distributed ranking through concrete examples."
categories:
  - "System Design"
tags: [system-design, top-k, active-listeners, streaming, redis, resiliency]
---

A listener opens a music app and starts `song_101`. Its listener count increases
by one. A few minutes later, that listener switches to `song_202`: the first
song loses a listener and the second gains one. A popular-songs widget needs
to reflect those changes and return the K songs with the most listeners.

The platform has 10 million registered users, around 1 million concurrent
listeners, and millions of songs. The leaderboard should respond quickly—a
p99 target of 50 ms—and normally reflect playback changes within five seconds.
K is supplied by the caller, up to a configured maximum.

For this design, each user contributes to one current song. The player reports
starts and stops, including a stop when playback pauses or finishes. These
reports are the source of the counts. A client that disappears without sending
a stop can leave a stale listener; automatic cleanup is a production extension
at the end.

## Build a Leaderboard That Works

The first step is to connect a playback change to a count. The player already
knows the song it is starting or stopping, so it can report an event like this:

```json
{
  "user_id": "user_36",
  "playback_id": "playback_901",
  "song_id": "song_101",
  "type": "START"
}
```

`playback_901` identifies this particular listening attempt. Replaying the same
song or resuming after a pause creates a new playback ID. A stop identifies
the playback and song being ended. The playback service validates the event
and passes it to the program that updates counts, called the **count processor**.
It can forward the event without knowing the user's previous song.

Suppose the current counts are:

| Song | Active listeners |
|---|---:|
| song_101 | 120,000 |
| song_202 | 100,000 |
| song_303 | 90,000 |

A start adds one to a counter; a stop subtracts one. The query API also needs
to find the largest counters. A Redis **sorted set** supplies both operations:
each entry is a song ID, its score is the listener count, and Redis maintains
the score order as counts change.

{% include figure.html path="assets/img/active-listeners/simple-counting.svg" alt="The player sends a start or stop with its song and playback ID. The playback service validates it, and a count processor changes the song score in Redis. The Query API reads the highest K scores. This first design illustrates the normal event path." caption="The first working design: connect playback reports to Redis scores, then read the highest scores for the widget." %}

Follow `user_36`, who is currently included in `song_101`'s count. The player
switches songs and reports these two events:

```text
STOP  playback_901, song_101 → song_101: 120,000 → 119,999
START playback_902, song_202 → song_202: 100,000 → 100,001
```

The corresponding Redis score changes are:

```redis
ZINCRBY active_songs -1 song_101
ZINCRBY active_songs  1 song_202
```

The same mechanism handles someone stopping at the end of a song. Remove a
song with `ZREM` when its count reaches zero, so the leaderboard contains only
songs with listeners.

The widget calls `GET /rankings?k=5`. The query API reads:

```redis
ZRANGE active_songs 0 4 REV WITHSCORES
```

`REV` returns the largest scores first. For another K, the end index is `K - 1`.
Equal counts are ordered by descending song ID, the ordering Redis uses for a
reverse-range query. If fewer than K songs have listeners, return those songs.

With D active songs, a [score change](https://redis.io/docs/latest/commands/zincrby/)
takes `O(log D)` and [reading K entries](https://redis.io/docs/latest/commands/zrange/)
takes `O(log D + K)`. A separate ranking algorithm is unnecessary at this stage:
the sorted set already keeps the songs in order.

The widget can now retrieve the leaders, and reported playback changes update
the appropriate scores. An interruption between the player and Redis exposes
the first weakness of this design.

### Keep Playback Events During an Outage

Suppose Redis is unavailable for 30 seconds. Listeners continue changing songs
during that interval. The playback service needs somewhere to save their
events until counting can resume.

Place Kafka between the playback service and the count processors. Kafka
retains accepted events, and processors read them as capacity becomes
available. The playback service acknowledges an event after Kafka durably
accepts it. A player that receives no acknowledgement retries the same event
with the same playback ID and event type.

{% include figure.html path="assets/img/active-listeners/decision-baseline.svg" alt="Kafka is added between the playback service and count processors. It retains start and stop events while processors or Redis are unavailable. Processors update Redis, and the Query API continues reading the highest K scores directly." caption="The first architecture change adds a retained event log. The counter logic stays the same; events can now wait and be replayed after a failure." %}

Kafka acceptance and leaderboard visibility happen at different times. The
five-second freshness target measures the processing delay between them. A
query reads the counts processed so far; its response time is not proof that
all events through that moment have arrived.

That delay is usually acceptable for a live popular-songs widget. The design
can continue serving while a brief backlog catches up. It also means an event
can be delivered more than once, which changes the counting problem.

## Keep the Counts Correct When Events Repeat or Arrive Late

Consider the start of `playback_901` again. A processor increments `song_101`,
then crashes before saving its Kafka progress. Its replacement receives the
same start. A second increment would add a listener who does not exist.

The processor needs to remember whether that playback has already been
counted. Keep a small record in Redis beside the song scores:

```text
playback_901:
    user_id: user_36
    song_id: song_101
    status: PLAYING
```

When the start is repeated, the record already says `PLAYING`, so the count
stays unchanged. The first stop changes the record to `CLOSED` and subtracts
one. A repeated stop finds `CLOSED` and subtracts nothing.

The record also solves a less obvious case. A stop can reach the processor
before its start. Subtracting immediately would produce an incorrect count.
Instead, save `CLOSED` without decrementing. When the delayed start arrives,
the processor sees that the playback has ended and leaves it closed.

| Saved state | Incoming event | Action |
|---|---|---|
| No record | START | Save PLAYING; add one listener. |
| PLAYING | START | Keep the existing contribution. |
| PLAYING | STOP | Save CLOSED; remove one listener. |
| No record | STOP | Save CLOSED; change no count. |
| CLOSED | START or STOP | Keep it closed; change no count. |

{% include figure.html path="assets/img/active-listeners/playback-lifecycle.svg" alt="A first start adds one listener and saves PLAYING. A stop removes one and saves CLOSED. Repeated events leave the count unchanged. A stop arriving first saves CLOSED without subtracting, so a delayed start cannot revive the playback." caption="One playback ID changes the count at most once in each direction. A new listening attempt gets a new ID." %}

The playback service checks that all events for a playback identify the same
user and song. Keeping a closed record is part of correctness: deleting it
while its old start can still be retried would let that start count again.
Set a supported retry and replay interval, then retain closed records for that
interval or reject older playback IDs when accepting events.

### Save the Record and Score Together

There is another failure between saving `PLAYING` and incrementing the score.
If either write succeeds alone, a retry can leave a missing or duplicate
listener. Both changes therefore belong in one Redis operation.

A short Redis function or Lua script reads the playback record, decides the
allowed change, and writes the new record and song score without another
command running between them. It also saves the processed Kafka position.
After that operation succeeds, the processor acknowledges its progress.
Repeating it after a lost response encounters the same saved state and
changes no count. Redis describes this [atomic execution model](https://redis.io/docs/latest/develop/programmability/eval-intro/).

Validate the operation before writing: Redis scripts do not roll back earlier
writes after a runtime error. Keep the script short, and reconcile state after
an unexpected partial failure. In Redis Cluster, the record and leaderboard
keys touched by the script must share a hash slot; a common hash tag such as
`{active}` places them together. This deliberately keeps the initial counting
state on one shard.

Retries are now safe for an individual playback. Returning to `user_36`'s song
switch reveals why that guarantee alone is not enough for a user leaderboard.

### Count the User Once During a Song Switch

Suppose the start of `playback_902` arrives while the stop of `playback_901`
is delayed. Both playback records can say `PLAYING`, even though the user has
already moved to the second song. Two devices can create the same overlap.

The requirement is one contribution per user. Add a current-user record:

```text
user_36:
    playback_id: playback_901
    song_id: song_101
```

When the start for `playback_902` arrives, the processor reads this record and
finds the previous contribution. In one Redis operation, it closes
`playback_901`, subtracts one from `song_101`, adds one to `song_202`, and saves
`playback_902` as the user's current playback.

The old stop eventually arrives and finds `playback_901` already closed. It
changes nothing. The processor learned the previous song from accepted events;
the playback service still only had to forward each event's own song ID.

A late start from an old device also needs a clear rule. The playback
platform's authorization service can assign increasing playback versions:
42 for the old playback, 43 for the new one. The processor accepts the newer
authorized version and ignores the older one. A client cannot choose its own
version to regain control of playback. The user record retains the latest
authorized version alongside the current song and playback ID.

Keep the latest version after a stop, including a stop that arrives before
its start. A newer stop closes any older counted contribution and records
the new playback as closed. Its later start stays closed. This prevents a
stopped or superseded playback from reappearing because of delayed delivery.

{% include figure.html path="assets/img/active-listeners/user-contribution.svg" alt="A newer start for user_36 replaces playback_901 on song_101 with playback_902 on song_202. The Redis operation closes the old playback and moves the user's one contribution. A delayed stop for the old playback subtracts nothing." caption="The user record is added to solve an observed overlap: a new start arrives before the old stop. It provides the previous contribution to replace." %}

The extra record has a specific job. Playback records protect individual starts
and stops; the user record enforces one current contribution across playbacks.
If an upstream system already supplies that exclusive contribution stream,
the leaderboard can rely on that guarantee instead.

## Recover the Same Design After a Worker Fails

The Redis operation is the same for every user, so several processors can
share the work. A **worker** is a running copy of that processing program.
Kafka divides the event stream into numbered groups called **partitions** and
assigns each partition to one worker in a consumer group.

Use `user_id` as the routing key so all events for one user reach the same
input partition. For example, partition 7 contains events for `user_36` and
`user_52`; Worker 1 reads that partition. Partition 8 contains `user_83` and
`user_94`; Worker 2 reads it. Each worker can read several partitions.

{% include figure.html path="assets/img/active-listeners/redis-worker-ownership.svg" alt="Kafka partitions 7 and 8 are assigned to two count workers. Each processes many users and calls Redis. Redis holds the playback records, current-user contributions, song scores, and applied input positions. A durable backup saves those together." caption="Worker assignments distribute event processing. Playback records and song totals remain in Redis when an individual worker disappears." %}

If Worker 1 dies, its replacement resumes partition 7. The records for
`user_36` remain in Redis. Any repeated start or stop is handled by the checks
already introduced. A separate copy of every user's record inside the worker
is unnecessary in this design.

A disconnected old worker might return after its replacement starts. Give each
partition assignment an increasing ownership version, and check that version
inside the Redis operation. Once a new assignment is active, the old worker's
new writes are rejected. This number identifies the authorized processor;
the playback version identifies the user's newest playback decision.

### Recover When Redis Also Loses State

Now suppose Redis restores an older backup. Kafka can supply the missing
events, but recovery needs a position that matches the restored data.

A Kafka **offset** is an event's position in one partition. If the restored
Redis state includes partition 7 through offset 92,000, replay starts at
92,001. Using a newer consumer acknowledgement would skip events whose count
changes disappeared with the lost Redis writes.

Save song scores, playback records, user records, and their processed input
positions together. Restore that consistent copy, then replay later events.
Persistence and replicas reduce the amount of replay, but their guarantees
depend on configuration; Redis documents the relevant
[failover and write-safety limits](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/).

A complete rebuild needs an authoritative current-playback snapshot with
matching log positions, or enough retained history to reconstruct every still-
counted playback. A fixed recent window can miss a playback whose start was
much earlier. Closed-record retention and admission rules must support the
chosen replay boundary as well.

Rebuild into separate keys and switch the query API after catch-up and
validation. An optional cached Top K can serve during the outage with its
saved time and stale status. If no usable result exists, return unavailable.

The system now handles the main failure cases without changing its overall
shape: Kafka retains the input, processors retry safely, and Redis holds the
current records and ranking. The remaining question is how far that shape
can scale.

## Grow Beyond One Redis Leaderboard

One million concurrent listeners does not mean one million updates per second.
If each listener changes songs every three minutes on average:

```text
1,000,000 / 180 ≈ 5,600 song changes/second
5,600 × 2      ≈ 11,200 START/STOP events/second
```

With 100 input partitions and 10 workers, each worker handles roughly ten
partitions, 100,000 active users, and 1,120 events/second under this assumption.
Starts, stops, retries, and synchronized traffic add to that load. These are
sizing assumptions to test, not a benchmark result.

Redis receives the record checks and accepted score changes from all workers.
Measure the full operation, including the overlapping starts and stops that
become no-ops. Useful signals are input lag, Redis CPU and memory, update and
Top-K latency, and restore time.

State size deserves the same attention. One million 128-byte user records
would occupy 128 MB before database overhead. Playback records, song entries,
replicas, and retained closed records add more. At 5,600 completed playbacks
per second, keeping closed records for an hour adds about 20 million records.
The retry policy can cost more memory than the current leaderboard.

Adding workers helps event processing, but one sorted-set key remains on one
Redis shard. Redis Cluster distributes keys, not the members of one sorted
set. That is the boundary described by
[Redis Cluster's key distribution](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/).

If the complete workload fits within that shard's latency, memory, and recovery
targets, the design is sufficient. Sustained saturation or an unacceptable
restore time justifies the next architecture change.

### Split Song Counters Across Workers

Until this point, each processor made a user decision and changed Redis in the
same operation. To distribute counting, separate those responsibilities.
**User workers** retain the playback records and decide each user's accepted
contribution. **Song workers** maintain the totals for groups of songs. A group
of song counters is a **song partition**.

Follow the same switch again:

```text
user_36 starts song_202 while counted against song_101
    → User Worker 1 accepts the new contribution
        → song_101 −1 → song partition 3 → Song Worker 1
        → song_202 +1 → song partition 9 → Song Worker 2
```

`user_36` continues to reach User Worker 1. The accepted count changes take a
second route, by `song_id`, so changes from every listener of a song meet at
its owner. A worker can own many song counters and several partitions.

{% include figure.html path="assets/img/active-listeners/distributed-decision-path.svg" alt="User workers accept contributions from events grouped by user. A second Kafka topic carries their count changes, routed by song, to song workers. Local rankings flow to a merger, which publishes the overall Top K in Redis for queries." caption="The second architecture change moves song counts out of the single Redis index. User decisions and song aggregation now scale as separate stages." %}

The new boundary creates a new failure case: a user worker might save the
song switch but crash before sending its two count changes. Save its decision
and pending output together. A transactional stream-processing handoff, or a
saved list of changes that can be resent, makes the output recoverable. Give
each accepted change a stable identity so song workers can apply a resend once.

These workers can keep local state for throughput. A durable **checkpoint**
saves that state, matching input positions, and pending output. A replacement
restores the checkpoint and replays Kafka. This checkpoint is necessary
because records now live in worker-local stores, unlike the earlier design
where every processor used shared Redis state.

### Find the Global Winners From Local Rankings

Each song worker needs the same operations Redis previously provided: a count
map and an index ordered by count and then descending song ID. Updating a song
replaces its old index entry. A balanced tree gives `O(log D)` updates for D
songs in the partition and `O(log D + K)` local reads.

Keep every active song in the local index. If the fifth-ranked song loses
listeners, the sixth may replace it. Storing only the five winners would lose
that replacement candidate.

A **merger** combines the local leaders into the overall Top K. Consider a
later result with these complete song counts, using K=5:

{% include figure.html path="assets/img/active-listeners/local-top-k-merge.svg" alt="Two song partitions send their five leading complete counts. Among the ten candidates, song_101, song_202, song_303, song_404, and song_505 form the global Top 5. Lower local entries remain in the workers' indexes." caption="Each partition sends only its local Top K. The merger selects the highest K candidates, using the same ordering and the consistency policy described below." %}

An omitted song already has K songs ahead of it in its own partition. Those
songs also rank ahead of it globally, so the omitted song cannot be a global
winner. With P partitions, the merger examines at most `P × K` candidates.
For 200 partitions and K=5, that is 1,000 candidates. A size-K heap selects the
result in `O(PK log K)`. Retain lists for `K_max` when supporting smaller
request sizes through the same API.

This reasoning depends on complete counters. A song split across regions
needs its regional counts added before candidate pruning. Local winners from
partial counters are insufficient to identify every global winner.

### Handle a Popular Song That Overloads Its Owner

Partitioning spreads different songs across workers. A new release can still
send a large burst of changes for one song to a single owner.

First combine accepted changes into batches. Hundreds of additions and
removals can become one net update, such as +300. A batch needs its own stable
identity so retrying it does not apply another +300. Preserve the result's
chosen processing boundary when combining changes.

If batching is insufficient, split the hot song into groups based on a stable
hash of `user_id`. A small group number is called a **salt**. For example,
`hash(user_36) % 4 = 2` places that user's contribution in group 2.

{% include figure.html path="assets/img/active-listeners/stable-user-salting.svg" alt="User_36 belongs to salt group 2 for song_101. Both its addition and removal reach that partial counter. A combiner adds all four partial counts to reconstruct the complete song total before ranking." caption="A hot song can use several partial counters. The addition and later removal for one listener must reach the same partial." %}

A **combiner** adds every registered partial for that song before the result
enters global ranking. Send all partials even when none is locally popular.
Exclude the split song from ordinary local Top-K lists so its reconstructed
count appears once.

Save the contribution's salt and routing version with the user record. If the
number of groups changes, existing removals must still find their original
partial, or a migration must move those contributions coherently. Keeping old
routing until playback ends is simpler, but long-running playbacks can keep
that version alive indefinitely.

### Decide Which Local Results Can Be Combined

Distribution also changes what a query means. The original Redis read observes
one database state. Two song workers can be at different positions in the
stream: the decrement for `song_101` may be processed while the increment for
`song_202` is still waiting.

For a live popular-songs widget, the merger can combine the latest local lists
and tolerate this temporary skew. Track each partition's lag and list age,
and set a limit on how much skew is acceptable. The combined ranking reflects
those local states, with no claim of a common source-time boundary.

If the product requires each accepted switch to appear as one completed
change, add a common snapshot. A **barrier** is a marker naming the input
positions included in that snapshot. User workers emit all earlier decisions
before the marker. Song workers process their preceding changes before
producing a local list for that marker.

{% include figure.html path="assets/img/active-listeners/common-snapshot.svg" alt="Song partitions 3 and 9 complete snapshot 184. The merger waits for both expected lists, keeps snapshot 185 separate, and publishes the completed Top-K object for snapshot 184." caption="A common snapshot adds a stronger guarantee: every local list includes the same completed decision boundary, including both sides of each included switch." %}

The marker must reach every expected downstream route, including empty ones.
A stage with several inputs waits for the marker from each input and buffers
later data while aligning that boundary. The merger combines lists with the
same snapshot and routing versions. An empty partition sends an empty list to
confirm completion. A retry replaces its previous list, adding no extra count.

Publish the finished ranking and snapshot ID as one Redis object. Check the
current publisher's ownership version and increasing snapshot ID so an old
publisher cannot overwrite a newer result. If a contributor falls behind,
serve the last complete result with its saved metadata and bound the pending
snapshot buffers.

The snapshot identifies a consistent processing boundary. Calling it
`as_of=10:05:00` additionally requires every source to establish that all
accepted input through 10:05:00 has been included. A publication timestamp
alone does not establish that completeness. Stream processors provide
[checkpoint and replay mechanisms](https://nightlies.apache.org/flink/flink-docs-stable/docs/learn-flink/fault_tolerance/)
for coordinating this state across stages.

The added waiting and recovery state are the cost of the stronger guarantee.
A continuously updated widget can keep the simpler contract; a product that
needs a consistent snapshot pays for coordination explicitly.

## Verify the Design Against the Same Playback Story

The example now supplies a useful set of checks:

| Event or failure | Expected result |
|---|---|
| Repeat the start of playback_901 | song_101 gains one listener in total. |
| Deliver its stop before its start | The playback stays closed and contributes zero. |
| Start playback_902 before the old stop arrives | user_36 moves from song_101 to song_202 once. |
| Deliver the old stop afterward | song_202 keeps the user's contribution. |
| Deliver an old-device start | Its older authorized version changes no count. |
| Crash after the Redis update | Replaying the event leaves the counts unchanged. |
| Restore an older backup | Replay begins at the input positions saved with that backup. |
| Delay a required distributed snapshot list | Keep serving the previous complete snapshot. |

For one contribution per user, the sum of song counts equals the number of
counted user records at the same processing boundary. A negative count or a
mismatch calls for reconciliation and replay. Forcing a negative count to zero
would hide the error that caused it.

Monitor input lag, Redis operation latency, duplicate and obsolete events,
active and closed record counts, query latency, and restore time. After
distribution, add missing local lists, partial-counter coverage, snapshot age,
and progress differences between partitions. Validate sampled results against
authoritative playback state, including retry storms and device transfers.

### Add Disconnect Detection When the Product Needs It

The ranking relies on reported stops. If a player loses connectivity before
sending one, its last contribution remains. A production deployment
can add activity reports—for example, every 15 seconds—and remove a
contribution after 45 seconds without accepted activity.

That timeout uses the same removal path as a stop. Check the saved playback
identity, version, and deadline before removing it, so an old timer cannot
remove a newer playback. Recovery restores deadlines and catches up on
retained activity before expiring contributions. This extension adds heartbeat
traffic, timers, and a delay before silent disconnects disappear from counts.

## Related Design: Recent Playback Counts

The [sliding-window Top-K design]({{ '/blog/2026/top-k-heavy-hitters/' | relative_url }})
ranks songs by plays during the last five minutes. It covers time buckets,
window expiration, and approximate heavy hitters.
