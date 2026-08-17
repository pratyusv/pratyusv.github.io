---
layout: single
comments: true
title: C++ List and LRU Patterns
date: 2026-07-22 10:00:00-0000
last_modified_at: 2026-08-17 00:00:00-0000
description: A practical guide to std::list, including insertion, erasure, splice overloads, iterator stability, list algorithms, and an LRU cache design.
toc: true
toc_sticky: true
categories: C++
tags: [cpp, stl, containers, list, lru, linked-list]
---

## Overview

`std::list` is a doubly linked list. It is not the default sequence container, but it is useful when stable iterators and constant-time node movement matter.

A common systems use case is an LRU cache: `std::list` stores recency order, and `std::unordered_map` maps keys to list iterators. This combination makes lookup and recency updates constant time on average.

---

## Common Includes

{% highlight c++ %}
#include <iterator>
#include <list>
#include <stdexcept>
#include <unordered_map>
{% endhighlight %}

---

## Basic Operations

{% highlight c++ %}
std::list<int> nums = {10, 20, 30, 40};

nums.push_front(5);
nums.push_back(50);

auto it = std::next(nums.begin(), 2);     // points to 20
auto inserted = nums.insert(it, 15);      // inserts before it; points to 15
auto next = nums.erase(it);               // erases 20; points to 30
{% endhighlight %}

`insert(position, value)` inserts immediately before `position` and returns an iterator to the new element. `erase(position)` removes the denoted element and returns an iterator to the following element. Erasing `end()` is invalid.

Insertions do not invalidate existing iterators or references. Erasure invalidates only iterators and references to erased elements.

{% highlight c++ %}
std::list<int> nums = {10, 20, 30, 40};

auto it = std::next(nums.begin(), 2);     // points to 30
nums.insert(it, 25);                      // [10, 20, 25, 30, 40]
{% endhighlight %}

---

## splice

`splice` moves nodes between lists or within the same list by rewiring pointers. It does not copy or move the stored value.

Read its three forms as follows:

{% highlight c++ %}
destination.splice(insert_before, source);
destination.splice(insert_before, source, element);
destination.splice(insert_before, source, first, last);
{% endhighlight %}

| Part | Meaning |
|---|---|
| `destination` | The list that will contain the selected nodes |
| `insert_before` | A position in `destination`; moved nodes are placed immediately before it |
| `source` | The list currently containing the selected nodes |
| `element` | A dereferenceable iterator identifying one source element |
| `[first, last)` | A half-open range of source elements |

Move one element between two lists:

{% highlight c++ %}
std::list<int> ready = {10, 20};
std::list<int> waiting = {30, 40, 50};
auto selected = std::next(waiting.begin());  // points to 40

ready.splice(ready.end(), waiting, selected);

// ready:   [10, 20, 40]
// waiting: [30, 50]
// selected remains valid and now refers to 40 in ready
{% endhighlight %}

Move a half-open range:

{% highlight c++ %}
std::list<int> destination = {1, 5};
std::list<int> source = {2, 3, 4, 6};

auto before_five = std::next(destination.begin());
auto first = source.begin();               // points to 2
auto last = std::next(source.begin(), 3);  // points to 6

destination.splice(before_five, source, first, last);

// destination: [1, 2, 3, 4, 5]
// source:      [6]
{% endhighlight %}

The source and destination may be the same list. In that case, `splice` relocates nodes:

{% highlight c++ %}
std::list<int> nums = {10, 20, 30, 40};
auto it = std::next(nums.begin(), 2);     // points to 30

nums.splice(nums.begin(), nums, it);      // [30, 10, 20, 40]
{% endhighlight %}

Here, the object before the dot and the `source` argument are both `nums`. The element at `it` is removed from its current position and placed immediately before `nums.begin()`.

Important contract details:

- Iterators, pointers, and references to moved elements remain valid. After a cross-list splice, they refer to elements in the destination.
- Moving an entire list or one element is constant time.
- Moving a range within the same list is constant time. Moving a range between different lists is linear in the range length.
- Source and destination allocators must compare equal.
- For a same-list range splice, `insert_before` must not lie inside `[first, last)`.

---

## List-Specific Algorithms

`std::list` has bidirectional iterators, not random-access iterators. `std::sort` does not work on it. Use the member function instead.

{% highlight c++ %}
std::list<int> nums = {4, 2, 5, 1, 3};

nums.sort();                              // [1, 2, 3, 4, 5]
nums.reverse();                           // [5, 4, 3, 2, 1]
nums.remove(3);                           // remove every 3
nums.unique();                            // remove adjacent duplicates
{% endhighlight %}

`remove(value)` erases every matching element. `unique()` erases duplicates only when they are adjacent; call `sort()` first if the goal is to remove duplicate values regardless of their original positions. Both functions return the number erased in C++20 and later.

Merge sorted lists:

{% highlight c++ %}
std::list<int> a = {1, 3, 5};
std::list<int> b = {2, 4, 6};

a.merge(b);                               // a: [1, 2, 3, 4, 5, 6], b: empty
{% endhighlight %}

`merge` assumes that both lists are already sorted using the same ordering. It transfers nodes from `b` into `a`, preserves the relative order of equivalent elements, and leaves `b` empty. It does not sort arbitrary input.

---

## LRU Cache Pattern

The list stores `{key, value}` pairs from most-recent to least-recent. The map stores key to list iterator.

{% highlight c++ %}
class LRUCache {
 private:
  using Entry = std::pair<int, int>;

  int capacity_;
  std::list<Entry> items_;
  std::unordered_map<int, std::list<Entry>::iterator> index_;

 public:
  explicit LRUCache(int capacity) : capacity_(capacity) {
    if (capacity <= 0) {
      throw std::invalid_argument("capacity must be positive");
    }
  }

  int get(int key) {
    auto found = index_.find(key);
    if (found == index_.end()) {
      return -1;
    }

    items_.splice(items_.begin(), items_, found->second);
    return found->second->second;
  }

  void put(int key, int value) {
    auto found = index_.find(key);
    if (found != index_.end()) {
      found->second->second = value;
      items_.splice(items_.begin(), items_, found->second);
      return;
    }

    if (static_cast<int>(items_.size()) == capacity_) {
      int old_key = items_.back().first;
      index_.erase(old_key);
      items_.pop_back();
    }

    items_.push_front({key, value});
    index_[key] = items_.begin();
  }
};
{% endhighlight %}

Important points:

- `splice` keeps existing list iterators valid.
- Removing a list node invalidates only the iterator to that node.
- Map lookup gives `O(1)` average access to the list node.
- The list gives `O(1)` movement to the front.
- The constructor establishes the invariant that capacity is positive, so eviction never calls `back()` on an empty list.
- Returning `-1` uses a sentinel that may conflict with a stored value. A reusable cache API would normally return `std::optional<int>` or expose success separately.

---

## Complexity

| Operation | Complexity |
|---|---:|
| Access by index | Not supported |
| Traversal to position | `O(n)` |
| Insert/erase with iterator | `O(1)` |
| Move all nodes or one node with `splice` | `O(1)` |
| Move a range with `splice` | `O(1)` within one list; `O(k)` across lists |
| Search by value | `O(n)` |

---

## Checklist

- Use `list` only when its iterator stability or node movement is needed.
- Use `vector` for most sequential storage.
- Use `splice` for LRU-style recency movement.
- Do not use `std::sort` on `list`; use `list::sort`.
- Sort both inputs with the same ordering before calling `merge`.
- Keep map iterators synchronized when deleting list nodes.

## Further Reading

- [C++ working draft: `list` operations](https://eel.is/c++draft/list.ops)
- [C++ working draft: `list` modifiers](https://eel.is/c++draft/list.modifiers)
