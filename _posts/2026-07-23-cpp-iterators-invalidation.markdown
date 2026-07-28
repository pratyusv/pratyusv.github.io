---
layout: single
comments: true
title: C++ Iterators and Invalidation
date: 2026-07-23 10:00:00-0000
categories: C++
tags: [cpp, stl, containers, iterators, invalidation]
---

## Overview

Iterators provide a common traversal interface across STL containers. The same syntax works for contiguous containers such as `vector`, node-based containers such as `list`, and tree-based containers such as `map`.

The main interview risks are:

- Confusing `end()` with the last element.
- Using an algorithm that requires a stronger iterator category.
- Continuing to use an iterator after it has been invalidated.

---

## Common Includes

{% highlight c++ %}
#include <algorithm>
#include <iterator>
#include <list>
#include <map>
#include <vector>
{% endhighlight %}

---

## Half-Open Ranges

STL algorithms work on half-open ranges: `[begin, end)`.

{% highlight c++ %}
std::vector<int> nums = {10, 20, 30};

auto first = nums.begin();                // points to 10
auto last = nums.end();                   // one past 30

for (auto it = first; it != last; ++it) {
  int value = *it;
}
{% endhighlight %}

`end()` is not dereferenceable. It is a sentinel used for comparison.

---

## Iteration Patterns

Explicit iterator:

{% highlight c++ %}
for (auto it = nums.begin(); it != nums.end(); ++it) {
  *it += 1;
}
{% endhighlight %}

Const iterator:

{% highlight c++ %}
for (auto it = nums.cbegin(); it != nums.cend(); ++it) {
  int value = *it;
}
{% endhighlight %}

Reverse iterator:

{% highlight c++ %}
for (auto it = nums.rbegin(); it != nums.rend(); ++it) {
  int value = *it;
}
{% endhighlight %}

Range-based loop:

{% highlight c++ %}
for (const auto& value : nums) {
  // read without copying
}

for (auto& value : nums) {
  value *= 2;
}
{% endhighlight %}

---

## Iterator Categories

Not all iterators support the same operations.

| Category | Examples | Supports |
|---|---|---|
| Forward | `forward_list` | `++it` |
| Bidirectional | `list`, `map`, `set` | `++it`, `--it` |
| Random access | `vector`, `deque`, `array` | `it + n`, `it[n]`, ordering comparisons |

This is why `std::sort` works on `vector` but not on `list`.

{% highlight c++ %}
std::vector<int> v = {3, 1, 2};
std::list<int> l = {3, 1, 2};

std::sort(v.begin(), v.end());            // OK
// std::sort(l.begin(), l.end());         // does not compile

l.sort();                                 // OK
{% endhighlight %}

---

## Iterator Helpers

Use iterator helpers instead of assuming pointer arithmetic works.

{% highlight c++ %}
std::list<int> nums = {10, 20, 30, 40, 50};

auto it = nums.begin();
std::advance(it, 3);                      // it points to 40

auto next_it = std::next(it);             // points to 50, it unchanged
auto prev_it = std::prev(it);             // points to 30, it unchanged

int dist = static_cast<int>(std::distance(nums.begin(), it));
{% endhighlight %}

For `list`, these operations walk nodes and are `O(n)`. For `vector`, they are `O(1)`.

---

## Invalidation Rules

| Container | Invalidated by insert | Invalidated by erase |
|---|---|---|
| `vector` | All iterators if reallocated; otherwise at/after insert position | At/after erased position |
| `deque` | Usually all iterators | Usually all iterators |
| `list` | Nothing | Only erased element |
| `map`, `set`, `multiset` | Nothing | Only erased element |
| `unordered_map`, `unordered_set` | All iterators if rehashed | Only erased element |

For `vector`, reallocation also invalidates pointers and references to elements.

---

## Erase While Iterating

Wrong:

{% highlight c++ %}
std::vector<int> nums = {1, 2, 3, 4, 5};

for (auto it = nums.begin(); it != nums.end(); ++it) {
  if (*it % 2 == 0) {
    nums.erase(it);                       // it is invalid after erase
  }
}
{% endhighlight %}

Correct:

{% highlight c++ %}
for (auto it = nums.begin(); it != nums.end();) {
  if (*it % 2 == 0) {
    it = nums.erase(it);                  // next valid iterator
  } else {
    ++it;
  }
}
{% endhighlight %}

C++20:

{% highlight c++ %}
std::erase_if(nums, [](int x) {
  return x % 2 == 0;
});
{% endhighlight %}

---

## Checklist

- Treat `[begin, end)` as the default STL range shape.
- Never dereference `end()`.
- Use `const auto&` to avoid unnecessary copies in range loops.
- Use `std::next` and `std::prev` when the container is not random-access.
- After `erase`, use the iterator returned by `erase`.
- Re-check iterator validity after container growth, especially for `vector` and `unordered_map`.
