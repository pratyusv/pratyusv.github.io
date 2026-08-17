---
layout: single
comments: true
title: C++ Iterators and Invalidation
date: 2026-07-23 10:00:00-0000
last_modified_at: 2026-08-17 00:00:00-0000
description: A practical guide to C++ iterators, half-open ranges, iterator categories, invalidation rules, and safe mutation during traversal.
toc: true
toc_sticky: true
categories: C++
tags: [cpp, stl, containers, iterators, invalidation]
---

## Overview

Iterators provide a common traversal interface across STL containers. The same syntax works for contiguous containers such as `vector`, node-based containers such as `list`, and tree-based containers such as `map`.

The main correctness risks are:

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
| Input | `istream_iterator` | Single-pass reading and `++it` |
| Output | `ostream_iterator`, `back_insert_iterator` | Single-pass writing and `++it` |
| Forward | `forward_list`, unordered containers | Multi-pass traversal and `++it` |
| Bidirectional | `list`, `map`, `set` | Forward operations plus `--it` |
| Random access | `deque` | Bidirectional operations plus `it + n`, `it[n]`, and iterator ordering |
| Contiguous | `vector`, `array`, `string`, `span` | Random-access operations with elements contiguous in memory |

The readable iterator categories form a capability hierarchy: forward iterators meet input requirements, bidirectional iterators add reverse movement, random-access iterators add constant-time jumps, and contiguous iterators additionally guarantee adjacent storage. Output iterators model a writing role rather than another level of readable access.

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

Advancing an iterator outside its valid range is not a bounds-checked operation. `std::next(nums.end())`, for example, is invalid. The caller must know that the requested destination is reachable.

---

## Invalidation Rules

| Container and operation | Iterator invalidation | Reference and pointer invalidation |
|---|---|---|
| `vector` insertion with reallocation | All, including `end()` | All elements |
| `vector` insertion without reallocation | At or after the insertion point, including `end()` | At or after the insertion point |
| `vector` erasure | At or after the first erased element, including `end()` | At or after the first erased element |
| `deque` insertion at either end | All iterators | Existing element references and pointers remain valid |
| `deque` insertion in the middle | All iterators | All references and pointers |
| `deque` erasure at an end | Erased elements; erasing the last element also invalidates `end()` | Erased elements only |
| `deque` erasure in the middle | All iterators, including `end()` | All references and pointers |
| `list` insertion | None | None |
| `list` erasure | Erased elements only | Erased elements only |
| `map`, `set`, `multiset` insertion | None | None |
| `map`, `set`, `multiset` erasure | Erased elements only | Erased elements only |
| Unordered-container insertion without rehash | None | None |
| Unordered-container rehash | All iterators | Existing element references and pointers remain valid |
| Unordered-container erasure | Erased elements only | Erased elements only |

The past-the-end iterator deserves explicit attention. It does not refer to an element, so a rule that preserves references to elements does not necessarily preserve a previously saved `end()` iterator.

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

The iterator returned by `erase` is the correct resumption point. Do not increment the erased iterator first, and do not use a previously cached `end()` when the operation may have invalidated it.

---

## Checklist

- Treat `[begin, end)` as the default STL range shape.
- Never dereference `end()`.
- Use `const auto&` to avoid unnecessary copies in range loops.
- Use `std::next` and `std::prev` when the container is not random-access.
- After `erase`, use the iterator returned by `erase`.
- Re-check iterator validity after container growth, especially for `vector` and `unordered_map`.

## Further Reading

- [C++ working draft: iterator requirements](https://eel.is/c++draft/iterator.requirements)
- [C++ working draft: container requirements](https://eel.is/c++draft/container.requirements)
- [C++ working draft: deque modifiers](https://eel.is/c++draft/deque.modifiers)
