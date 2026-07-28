---
layout: single
comments: true
title: C++ Maps and Hashing
date: 2026-07-20 10:00:00-0000
categories: C++
tags: [cpp, stl, containers, map, unordered-map, hashing]
---

## Overview

C++ has two common key-value containers:

- `std::map`: ordered keys, `O(log n)` operations.
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

---

## unordered_map

`std::unordered_map` is the default hash map. Operations are `O(1)` average-case and `O(n)` worst-case.

{% highlight c++ %}
std::unordered_map<std::string, int> count;

count["apple"]++;
count["banana"] = 3;

if (count.find("apple") != count.end()) {
  // exists
}

count.erase("banana");
{% endhighlight %}

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

cache.insert({42, "answer"});             // does not overwrite existing 42
cache.emplace(7, "seven");                // constructs pair in place
cache.try_emplace(42, "new answer");      // constructs value only if key is absent
{% endhighlight %}

`try_emplace` is useful when the value is expensive to construct or move.

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
- Remember that `unordered_map` iteration order is not stable or sorted.
- Rehashing an `unordered_map` invalidates iterators.
