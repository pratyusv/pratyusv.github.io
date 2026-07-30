---
layout: single
comments: true
title: C++ Containers Index
date: 2026-07-18 10:00:00-0000
categories: C++
tags: [cpp, stl, containers, index]
---

This is the central index for the C++ containers series. The goal is to keep the overview short and link to focused notes for each container family.

## Core Sequence Containers

1. [Vector and String]({% post_url cpp-reference/2026-07-19-cpp-vector-string %})
2. [Deque and Sliding Windows]({% post_url cpp-reference/2026-07-21-cpp-deque %})
3. [List and LRU Patterns]({% post_url cpp-reference/2026-07-22-cpp-list-lru-patterns %})

## Associative Containers

1. [Maps and Hashing]({% post_url cpp-reference/2026-07-20-cpp-maps-hashing %})
2. [Sets and Multisets]({% post_url cpp-reference/2026-07-20-cpp-sets %})

## Container Adapters

1. [Queue, Stack, and Priority Queue]({% post_url cpp-reference/2026-07-21-cpp-container-adapters %})

## Iterator Rules

1. [Iterators and Invalidation]({% post_url cpp-reference/2026-07-23-cpp-iterators-invalidation %})

---

## Container Selection

| Need | Container |
|---|---|
| Dynamic array, index access | `vector` |
| Text manipulation | `string` |
| Key-value lookup, sorted keys | `map` |
| Key-value lookup, average `O(1)` | `unordered_map` |
| Unique values, sorted order | `set` |
| Sorted values with duplicates | `multiset` |
| Unique values, average `O(1)` | `unordered_set` |
| Stable iterators, node movement | `list` |
| FIFO processing | `queue` |
| LIFO processing | `stack` |
| Max/min element access | `priority_queue` |
| Push/pop from both ends | `deque` |

---

## Complexity Summary

| Container | Access | Insert | Delete | Lookup |
|---|---:|---:|---:|---:|
| `vector` | `O(1)` by index | `O(1)` end, `O(n)` middle | `O(1)` end, `O(n)` middle | `O(n)` |
| `string` | `O(1)` by index | `O(1)` end, `O(n)` middle | `O(1)` end, `O(n)` middle | `O(n)` |
| `map` | - | `O(log n)` | `O(log n)` | `O(log n)` |
| `unordered_map` | - | `O(1)` average | `O(1)` average | `O(1)` average |
| `set` | - | `O(log n)` | `O(log n)` | `O(log n)` |
| `multiset` | - | `O(log n)` | `O(log n)` | `O(log n)` |
| `unordered_set` | - | `O(1)` average | `O(1)` average | `O(1)` average |
| `list` | `O(n)` by traversal | `O(1)` with iterator | `O(1)` with iterator | `O(n)` |
| `queue` | front/back only | `O(1)` | `O(1)` | - |
| `stack` | top only | `O(1)` | `O(1)` | - |
| `priority_queue` | top only | `O(log n)` | `O(log n)` | - |
| `deque` | `O(1)` by index | `O(1)` ends | `O(1)` ends | `O(n)` |

---

## Interview Checklist

- Use `vector` when index access and traversal are central.
- Use `unordered_map` or `unordered_set` when fast lookup is central and order does not matter.
- Use `map`, `set`, or `multiset` when sorted order or range queries matter.
- Use `queue` for BFS and `stack` for iterative DFS or nested parsing.
- Use `priority_queue` for repeated best/min/max extraction.
- Use `deque` for sliding-window and monotonic-queue patterns.
- Use `list` only when stable iterators or node movement matter.
- Be careful: `map[key]` and `unordered_map[key]` insert default values when the key is missing.
- Be careful: vector reallocation invalidates existing pointers, references, and iterators.
