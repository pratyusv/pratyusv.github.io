

# SSTable Diagram Plan

## Goal
Create high-quality architecture and data-flow diagrams for the SSTable blog post.

---

# 1. End-to-End Write Path

## Purpose
Show how writes move through the WAL, memtable, flush pipeline, and SSTables.

## Components
- Client
- WAL
- Memtable
- Flush Process
- SSTable
- Compaction

## Flow
Client -> WAL -> Memtable -> Flush -> SSTable -> Compaction

## Blog Section
"End-to-End Data Flow"

---

# 2. SSTable Internal File Layout

## Purpose
Visualize the physical structure of one SSTable.

## Components
- Metadata
- Bloom Filter
- Index Summary
- Primary Index
- Data Blocks

## Blog Section
"SSTable File Layout"

---

# 3. Data Block Structure

## Purpose
Show record-level structure inside a block.

## Components
- key_length
- value_length
- timestamp
- key bytes
- value bytes
- Prefix compression
- Restart points

## Blog Section
"SSTable File Layout"

---

# 4. Read Path Lookup Flow

## Purpose
Show how a point lookup traverses Bloom filters, indexes, and blocks.

## Components
- Memtable
- Immutable Memtable
- Bloom Filter
- Index Summary
- Primary Index
- Data Block

## Flow
Lookup -> Bloom Filter -> Index Summary -> Primary Index -> Data Block

## Blog Section
"Read Path"

---

# 5. Range Scan Merge

## Purpose
Visualize K-way merge across SSTables and memtables.

## Components
- Memtable Iterator
- SSTable Iterators
- Min Heap
- Output Stream
- Tombstone Filtering

## Blog Section
"Range Scans"

---

# 6. WAL + Recovery Flow

## Purpose
Show crash recovery and WAL replay.

## Components
- WAL Segments
- Crash
- Replay
- Memtable Rebuild
- SSTables

## Blog Section
"WAL, Flush, and Recovery Semantics"

---

# 7. Tombstone Lifecycle

## Purpose
Show how deletes propagate through compaction.

## Components
- Live Value
- Tombstone
- Compaction
- Tombstone Grace Window
- Final Cleanup

## Blog Section
"Deletes, Tombstones, and TTL"

---

# 8. Compaction Merge Flow

## Purpose
Visualize sorted merge compaction.

## Components
- SSTable Inputs
- Merge Iterator
- Conflict Resolution
- New SSTable Outputs

## Blog Section
"Compaction Internals"

---

# 9. STCS vs LCS

## Purpose
Compare size-tiered and leveled compaction.

## Components
- STCS Layout
- LCS Levels
- Read Amplification
- Write Amplification

## Blog Section
"Compaction Internals"

---

# 10. Read / Write / Space Amplification

## Purpose
Show tradeoffs between amplification dimensions.

## Components
- Read Amplification
- Write Amplification
- Space Amplification
- Tradeoff Arrows

## Blog Section
"Read, Write, and Space Amplification"

---

# 11. Point Read Worked Example

## Purpose
Visualize tombstone shadowing older versions.

## Components
- SSTable-5 Tombstone
- SSTable-4 Value
- SSTable-2 Value
- Lookup Flow

## Blog Section
"Worked Example"

---

# 12. Range Query Worked Example

## Purpose
Visualize iterator merge for a range scan.

## Components
- Memtable Stream
- SSTable Streams
- Heap Ordering
- Tombstone Suppression
- Final Output

## Blog Section
"Worked Example: Range Scan Merge"

---

# 13. Operational Failure Modes

## Purpose
Show common SSTable production bottlenecks.

## Components
- Compaction Debt
- Tombstone Accumulation
- Bloom Filter False Positives
- Write Stalls
- Hot Partitions

## Blog Section
"Operational Failure Modes"

---

# Recommended Build Order

1. End-to-End Write Path
2. Read Path Lookup Flow
3. Compaction Merge Flow
4. SSTable Internal File Layout
5. STCS vs LCS
6. Tombstone Lifecycle
7. Range Scan Merge
8. WAL + Recovery Flow
9. Amplification Tradeoffs
10. Worked Examples