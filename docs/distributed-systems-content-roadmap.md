# Distributed Systems Content Roadmap

This document tracks coverage gaps and future article opportunities for the
distributed-systems section of the blog. It is intentionally stored outside
`_posts` so that it remains an editorial planning document rather than a
published post.

## Current Anchor Articles

The strongest long-form articles currently cover:

- Apache Kafka
- Redis Cluster
- Raft
- Layer 4 and Layer 7 load balancing
- CDN and edge-cache internals
- WebSocket server internals
- SSTable internals

These establish the preferred style for future distributed-systems content:

- begin with a small, precise mental model;
- distinguish the data plane from the control plane;
- derive mechanisms from concrete failure cases;
- state invariants and guarantees explicitly;
- include implementation-oriented code;
- use diagrams for packet paths, state ownership, and transitions;
- finish with operational consequences and an end-to-end trace.

## Highest-Priority Missing Topics

### 1. Distributed Transactions — Complete

Completed as one checkout's connected journey through atomic commit, 2PC
prepare/decision/recovery, blocking and uncertain outcomes, idempotency, saga
forward and compensation paths, outbox relay, inbox deduplication, exactly-once
boundaries, and reconciliation; includes twenty-three SVG diagrams and concise
C++/SQL examples.

Suggested title:

> Inside Distributed Transactions: 2PC, Sagas, Outbox, Idempotency, and Recovery

Core subjects:

- atomic commit versus consensus;
- two-phase commit;
- coordinator and participant recovery;
- prepared transactions and blocking;
- uncertain transaction outcomes;
- saga orchestration and choreography;
- compensating operations;
- transactional outbox and change data capture;
- inbox and deduplication tables;
- idempotency keys;
- exactly-once claims and their actual boundaries.

Why it matters:

This connects Kafka transactions, load-balancer retries, database replication,
and application-level failure recovery. It is the recommended next article.

### 2. Time, Ordering, and Causality

Suggested title:

> Time in Distributed Systems: Clocks, Causality, Vector Clocks, and HLCs

Core subjects:

- wall-clock error and NTP;
- monotonic versus real-time clocks;
- happens-before relationships;
- Lamport clocks;
- vector clocks;
- concurrent writes;
- hybrid logical clocks;
- causal consistency;
- last-write-wins anomalies;
- uncertainty intervals and TrueTime-style designs.

Why it matters:

The current collection moves from CAP to Raft without a dedicated treatment of
time, event ordering, or causality.

### 3. Replication and Consistency Models

Suggested title:

> Inside Replicated Databases: Quorums, Read Repair, Anti-Entropy, and Conflict Resolution

Core subjects:

- synchronous and asynchronous replication;
- leader-based and leaderless replication;
- quorum equations;
- sloppy quorums and hinted handoff;
- read repair;
- Merkle-tree anti-entropy;
- conflict detection and resolution;
- replica lag;
- failover data loss;
- linearizability;
- sequential and causal consistency;
- eventual consistency;
- session guarantees.

Why it matters:

Redis and Raft explain particular replication designs. A general framework is
still needed for comparing database guarantees.

### 4. Distributed Locks, Leases, and Fencing

Suggested title:

> Distributed Locks: Leases, Fencing Tokens, Sessions, and Failure Safety

Core subjects:

- why a distributed lock is not merely a remote mutex;
- lease expiration;
- paused clients and stale lock holders;
- fencing tokens;
- ZooKeeper ephemeral nodes;
- etcd transactions and leases;
- leader election;
- lock-service partitions;
- the Redlock debate;
- protecting the resource after lock acquisition.

Why it matters:

The existing ZooKeeper article mentions locks and elections but does not explain
their failure semantics.

### 5. Service Discovery and Configuration Propagation

Suggested title:

> Inside Service Discovery: Registries, Leases, Watches, Health, and Convergence

Core subjects:

- registration and deregistration;
- DNS discovery versus registry APIs;
- leases and heartbeats;
- push watches versus polling;
- client-side and proxy-side discovery;
- discovery state versus health state;
- stale endpoint views;
- versioned configuration snapshots;
- control-plane outages;
- reconnect storms;
- Kubernetes Services and EndpointSlices.

Why it matters:

The existing service-discovery post is only a short note. This article would
also pair directly with the load-balancer control-plane discussion.

## Important Infrastructure Components

### 6. Distributed Rate Limiting

Suggested title:

> Inside Distributed Rate Limiters: Token Buckets, Sliding Windows, and Global Quotas

Core subjects:

- token and leaky buckets;
- fixed and sliding windows;
- local versus global enforcement;
- Redis and atomic scripts;
- overshoot under propagation delay;
- hierarchical and regional quotas;
- leased quota;
- hot keys;
- per-tenant fairness;
- fail-open and fail-closed behavior.

### 7. Partitioning and Live Rebalancing

Suggested title:

> Partitioning at Scale: Consistent Hashing, Virtual Nodes, Hotspots, and Rebalancing

Core subjects:

- range and hash partitioning;
- modulo hashing;
- consistent and rendezvous hashing;
- virtual nodes;
- partition maps and epochs;
- partition split and merge;
- online migration;
- dual reads and writes;
- hotspot detection and isolation;
- balancing data size versus traffic.

The existing sharding and consistent-hashing posts can be consolidated into
this article.

### 8. LSM Storage-Engine Internals

Suggested title:

> Inside an LSM Storage Engine: WAL, Memtables, Bloom Filters, Compaction, and Recovery

Core subjects:

- write-ahead logging;
- mutable and immutable memtables;
- SSTable creation;
- manifests and version sets;
- sparse indexes;
- Bloom filters;
- block caches;
- leveled and size-tiered compaction;
- tombstone propagation;
- crash recovery;
- read, write, and space amplification.

This would place the existing SSTable article inside a complete storage-engine
architecture. A later companion could compare B-trees and MVCC.

### 9. Object-Storage Internals

Suggested title:

> Inside Object Storage: Metadata, Erasure Coding, Repair, and Consistency

Core subjects:

- object namespace and metadata partitioning;
- immutable data fragments;
- replication versus erasure coding;
- placement;
- checksums and bit rot;
- background repair;
- atomic object visibility;
- multipart-upload garbage collection;
- read-after-write consistency;
- durability calculations;
- regional failure.

The current S3 article focuses mainly on multipart upload and could eventually
be incorporated into this broader treatment.

### 10. Distributed Scheduling

Suggested title:

> Inside a Distributed Scheduler: Placement, Leases, Preemption, and Reconciliation

Core subjects:

- desired versus observed state;
- resource offers;
- bin packing;
- constraints and affinity;
- reservations;
- lease-based ownership;
- duplicate scheduling;
- preemption;
- reconciliation loops;
- node failure;
- scheduler leadership;
- control-plane recovery.

### 11. Distributed ID Generation

Suggested title:

> Distributed ID Generation: Snowflake, Sequences, UUIDs, and Clock Failure

Core subjects:

- uniqueness versus ordering;
- database sequences;
- range allocation;
- Snowflake-style bit layouts;
- worker-ID assignment;
- clock rollback;
- epoch and sequence exhaustion;
- UUID variants;
- collision analysis;
- information leakage.

### 12. CDN and Edge Caching — Complete

Completed as one image's connected journey through DNS and Anycast steering,
tenant-aware cache keys, `Vary`, hierarchical lookup, request collapsing,
freshness and revalidation, bounded stale serving, negative and range caching,
origin shielding, purge generations, security, and regional failure. The post
includes twenty-five SVG diagrams and protocol-level HTTP examples.

Suggested title:

> Inside a CDN: Cache Keys, Revalidation, Purging, and Origin Protection

Core subjects:

- cache hierarchy;
- cache keys and variation;
- TTL and revalidation;
- stale-while-revalidate;
- request collapsing;
- negative caching;
- range requests;
- purge propagation;
- consistent hashing;
- origin shielding;
- regional failover.

## Advanced Topics

These are valuable after the foundational and infrastructure gaps are covered:

- **CRDTs:** state-based and operation-based designs, convergence, causal
  delivery, tombstones, and metadata growth.
- **Change data capture:** WAL tailing, snapshots, ordering, schema changes,
  checkpoints, and duplicate delivery.
- **Distributed tracing:** context propagation, head and tail sampling, clock
  skew, span loss, and cardinality control.
- **Multi-region databases:** locality, quorum placement, leader placement,
  RPO/RTO, failover, and conflict resolution.
- **Byzantine fault tolerance:** the Byzantine failure model, the `3f + 1`
  requirement, PBFT-style phases, signatures, and comparison with Raft.
- **Distributed stream processing:** event time, watermarks, state stores,
  checkpoints, replay, and barrier alignment.

## Existing Articles That Need Upgrading

These topics already exist, but their current coverage is substantially below
the Kafka, Redis, Raft, load-balancer, WebSocket, and SSTable standard.

| Existing subject | Current state | Recommended action |
|---|---|---|
| Service discovery | Very short note | Full rewrite |
| Capacity estimation | Rewritten long-form guide | Complete; maintain as workload assumptions evolve |
| Sharding | Rewritten as partitioning and live-rebalancing deep dive | Complete; maintain with implementation experience |
| Consistent hashing | Consolidated into the sharding article | Complete; legacy URL redirects to the canonical article |
| Distributed cache | Short overview | Merge with or redirect to Redis |
| Cassandra | Complete | Rewritten around a concrete schema and complete routing, read, write, storage, consistency, failure, and repair paths; added two concise C++ sketches and nine SVG diagrams |
| ZooKeeper | Complete | Rewritten as one connected registration-to-fencing story covering znodes, sessions, watches, Zab, recovery, recipes, failure handling, and operations; added two concise C++ sketches and thirteen SVG diagrams |
| GFS | Complete | Rewritten as one record's connected journey through chunk lookup, leases, data/control separation, record append, consistency, stale-replica fencing, recovery, snapshots, and garbage collection; added one concise C++ sketch and sixteen SVG diagrams |
| Bigtable | Complete | Rewritten as one row's connected journey through ordered schema design, hierarchical tablet lookup, shared commit logging, memtables, SSTables, merged reads, compaction, splitting, Chubby fencing, and recovery; added one concise C++ sketch and twenty SVG diagrams |
| S3 multipart upload | Focused walkthrough | Expand into object-storage internals |
| CAP theorem | Conceptual overview | Rewrite using formal histories and partition behavior |
| 2020 load balancer | Superseded overview | Redirect to the new load-balancer deep dive |

## Recommended Publication Order

1. Distributed transactions
2. Service discovery
3. Replication and consistency models
4. Distributed locks, leases, and fencing
5. Time, ordering, and causality
6. Distributed rate limiting
7. Partitioning and live rebalancing
8. LSM storage engines
9. Object storage
10. Distributed scheduling

## Recommended Next Article

The next article should be:

> Inside Distributed Transactions: 2PC, Sagas, Outbox, Idempotency, and Recovery

It fills the largest remaining conceptual gap and can reuse concrete failure
scenarios already introduced in the Kafka and load-balancer articles.
