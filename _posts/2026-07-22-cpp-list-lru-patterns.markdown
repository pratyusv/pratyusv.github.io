---
layout: single
comments: true
title: C++ List and LRU Patterns
date: 2026-07-22 10:00:00-0000
categories: C++
tags: [cpp, stl, containers, list, lru, linked-list]
---

## Overview

`std::list` is a doubly linked list. It is not the default sequence container, but it is useful when stable iterators and constant-time node movement matter.

The common interview use case is an LRU cache: `std::list` stores recency order, and `std::unordered_map` maps keys to list iterators.

---

## Common Includes

{% highlight c++ %}
#include <iterator>
#include <list>
#include <unordered_map>
{% endhighlight %}

---

## Basic Operations

{% highlight c++ %}
std::list<int> nums = {10, 20, 30, 40};

nums.push_front(5);
nums.push_back(50);

auto it = std::next(nums.begin(), 2);     // points to 20
nums.insert(it, 15);                      // insert before it
nums.erase(it);                           // erase element at it
{% endhighlight %}

Prefer computing iterators after the list has the shape you expect.

{% highlight c++ %}
std::list<int> nums = {10, 20, 30, 40};

auto it = std::next(nums.begin(), 2);     // points to 30
nums.insert(it, 25);                      // [10, 20, 25, 30, 40]
{% endhighlight %}

---

## splice

`splice` moves nodes between lists or within the same list by rewiring pointers. It does not copy or move the stored value.

{% highlight c++ %}
std::list<int> nums = {10, 20, 30, 40};
auto it = std::next(nums.begin(), 2);     // points to 30

nums.splice(nums.begin(), nums, it);      // [30, 10, 20, 40]
{% endhighlight %}

This operation is useful when the position is already known.

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

Merge sorted lists:

{% highlight c++ %}
std::list<int> a = {1, 3, 5};
std::list<int> b = {2, 4, 6};

a.merge(b);                               // a: [1, 2, 3, 4, 5, 6], b: empty
{% endhighlight %}

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
  explicit LRUCache(int capacity) : capacity_(capacity) {}

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

---

## Complexity

| Operation | Complexity |
|---|---:|
| Access by index | Not supported |
| Traversal to position | `O(n)` |
| Insert/erase with iterator | `O(1)` |
| Move node with `splice` | `O(1)` |
| Search by value | `O(n)` |

---

## Checklist

- Use `list` only when its iterator stability or node movement is needed.
- Use `vector` for most sequential storage.
- Use `splice` for LRU-style recency movement.
- Do not use `std::sort` on `list`; use `list::sort`.
- Keep map iterators synchronized when deleting list nodes.
