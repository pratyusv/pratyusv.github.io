---
layout: single
comments: true
title: C++ Maps and Hashing
date: 2026-07-20 10:00:00-0000
last_modified_at: 2026-08-17 00:00:00-0000
description: A practical guide to std::map and std::unordered_map, covering lookup, ordered bounds, insertion APIs, hashing contracts, invalidation, and performance.
toc: true
toc_sticky: true
categories: C++
tags: [cpp, stl, containers, map, unordered-map, hashing]
---

## Overview

C++ has two common key-value containers:

- `std::map`: ordered keys, `O(log n)` lookup, insertion, and key-based erasure.
- `std::unordered_map`: hash table, average `O(1)` operations.

Use the ordered version when sorted iteration, predecessor/successor queries, or range queries matter. Use the hash table when only fast lookup matters.

---

## Common Includes

{% highlight c++ %}
#include <cstddef>
#include <functional>
#include <iostream>
#include <map>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>
{% endhighlight %}

---

## map

`std::map` stores keys in sorted order. Internally, it is usually implemented as a self-balancing binary search tree.

{% highlight c++ %}
std::map<std::string, int> count;

count["apple"] = 2;
count["apple"]++;
count.insert({"banana", 3});

if (count.find("apple") != count.end()) {
  int value = count["apple"];
}

count.erase("banana");
{% endhighlight %}

A map iterator refers to a key-value pair:

{% highlight c++ %}
auto it = count.find("apple");
if (it != count.end()) {
  const std::string& key = it->first;  // keys cannot be changed in place
  int& value = it->second;             // mapped values can be changed
  value += 1;
}
{% endhighlight %}

`find(key)` returns `end()` when the key is absent. `contains(key)` provides a Boolean membership check in C++20. `at(key)` returns the mapped value without inserting, but throws `std::out_of_range` when the key is missing.

Iterate in sorted key order:

{% highlight c++ %}
for (const auto& [key, value] : count) {
  // key is sorted
}
{% endhighlight %}

Ordered operations:

{% highlight c++ %}
std::map<int, std::string> mp;
mp[10] = "a";
mp[20] = "b";
mp[30] = "c";

auto lower = mp.lower_bound(20);          // first key >= 20
auto upper = mp.upper_bound(20);          // first key > 20
{% endhighlight %}

Both functions return iterators:

- `lower_bound(key)` finds the first key that is **not ordered before** `key`. With the default comparator, this is the first key greater than or equal to it.
- `upper_bound(key)` finds the first key **ordered after** `key`. With the default comparator, this is the first key greater than it.
- Either result may be `end()` and must be checked before dereferencing.

The half-open range `[lower_bound(key), upper_bound(key))` contains every entry whose key is equivalent to `key`. For `map`, it contains at most one entry; the same rule is more useful with `multimap`, where it may contain several. With a custom comparator, interpret bounds using that ordering rather than numeric `>=` and `>`.

Bounds also support range queries:

{% highlight c++ %}
// Visit keys in the inclusive interval [10, 30].
for (auto it = mp.lower_bound(10); it != mp.upper_bound(30); ++it) {
  // it->first is a key in the requested interval
}
{% endhighlight %}

---

## unordered_map

`std::unordered_map` is the default hash map. Operations are `O(1)` average-case and `O(n)` worst-case.

Average constant time is not a latency guarantee. It depends on a suitable hash function, a reasonable load factor, and a well-distributed key set. Rehashing can make an individual insertion linear.

{% highlight c++ %}
std::unordered_map<std::string, int> count;

count["apple"]++;
count["banana"] = 3;

if (count.find("apple") != count.end()) {
  // exists
}

count.erase("banana");
{% endhighlight %}

When the approximate final size is known, reserve buckets before a large insertion phase:

{% highlight c++ %}
std::unordered_map<std::string, int> count;
count.reserve(10'000);
{% endhighlight %}

`reserve` can reduce repeated allocations and rehashes. A rehash invalidates all iterators, but pointers and references to existing elements remain valid. Erasing an element invalidates the iterator, pointer, and reference to that element.

Frequency counting:

{% highlight c++ %}
std::vector<int> nums = {1, 2, 2, 3};
std::unordered_map<int, int> freq;

for (int x : nums) {
  ++freq[x];
}
{% endhighlight %}

Two-sum pattern:

{% highlight c++ %}
std::vector<int> twoSum(const std::vector<int>& nums, int target) {
  std::unordered_map<int, int> index;

  for (int i = 0; i < static_cast<int>(nums.size()); ++i) {
    int need = target - nums[i];
    if (index.find(need) != index.end()) {
      return {index[need], i};
    }
    index[nums[i]] = i;
  }

  return {};
}
{% endhighlight %}

---

## operator[] Trap

`operator[]` is not read-only. If the key is missing, it inserts the key and default-constructs the value.

{% highlight c++ %}
std::unordered_map<std::string, int> count;

if (count["target"] == 5) {
  // count now contains "target" even if it was missing before
}
{% endhighlight %}

Use `find`, `contains`, or `at` when a lookup should not mutate the map.

{% highlight c++ %}
auto it = count.find("target");
if (it != count.end() && it->second == 5) {
  // read-only lookup
}

// C++20
if (count.contains("target") && count.at("target") == 5) {
  // read-only lookup
}
{% endhighlight %}

---

## insert, emplace, and try_emplace

{% highlight c++ %}
std::unordered_map<int, std::string> cache;

auto [first, inserted] = cache.insert({42, "answer"});
                                            // inserted == true
auto [same, inserted_again] = cache.emplace(42, "replacement");
                                            // inserted_again == false
                                            // existing value is unchanged
auto [lazy, added] = cache.try_emplace(7, "seven");
                                            // constructs value only if absent
auto [assigned, was_new] = cache.insert_or_assign(42, "new answer");
                                            // overwrites existing value
{% endhighlight %}

For unique-key maps, these functions return `{iterator, inserted}`. The iterator always identifies the resulting entry. The Boolean distinguishes a new insertion from an existing key.

Choose according to the intended collision behavior:

| Operation | Existing key | Value construction |
|---|---|---|
| `insert` | Preserves old value | A complete pair is supplied |
| `emplace` | Preserves old value | May construct arguments even when insertion fails |
| `try_emplace` | Preserves old value | Does not construct the mapped value when the key exists |
| `insert_or_assign` | Replaces old value | Constructs or assigns the mapped value |

---

## Custom Hash for pair

`std::map<std::pair<int, int>, T>` works because `std::pair` has lexicographic ordering. `std::unordered_map<std::pair<int, int>, T>` needs a custom hash.

{% highlight c++ %}
struct PairHash {
  std::size_t operator()(const std::pair<int, int>& p) const {
    std::size_t h1 = std::hash<int>{}(p.first);
    std::size_t h2 = std::hash<int>{}(p.second);
    return h1 ^ (h2 << 1);
  }
};

std::unordered_map<std::pair<int, int>, int, PairHash> grid_count;

grid_count[{1, 2}]++;
grid_count[{3, 4}] = 10;
{% endhighlight %}

An unordered container uses both a hash function and an equality predicate. They must agree: whenever two keys compare equal, they must produce the same hash. Equal hashes do not imply equal keys; collisions are expected and are resolved by the container.

The combination above is intentionally small and illustrative. For high-volume or adversarial workloads, hash quality, collision behavior, and denial-of-service exposure need explicit evaluation rather than assuming any bit-mixing expression is sufficient.

Common use cases:

- Grid coordinates.
- Geometry points with integer coordinates.
- Dynamic programming states.
- Graph states such as `{node, mask}`.

---

## Complexity

| Container | Ordering | Insert | Delete | Lookup |
|---|---|---:|---:|---:|
| `map` | Sorted by key | `O(log n)` | `O(log n)` | `O(log n)` |
| `unordered_map` | No sorted order | `O(1)` average | `O(1)` average | `O(1)` average |

---

## Checklist

- Use `unordered_map` for frequency counts, indices, and memoization.
- Use `map` for sorted iteration, range queries, and lower/upper bound.
- Avoid `mp[key]` for read-only checks.
- Use `try_emplace` when constructing a missing value is expensive; use `insert_or_assign` when replacement is intended.
- Call `reserve` before predictable bulk insertion into an unordered map.
- Remember that `unordered_map` iteration order is not stable or sorted.
- Rehashing an `unordered_map` invalidates iterators.

## Further Reading

- [C++ working draft: ordered associative containers](https://eel.is/c++draft/associative.reqmts)
- [C++ working draft: unordered associative containers](https://eel.is/c++draft/unord.req)
