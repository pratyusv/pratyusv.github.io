---
layout: single
comments: true
title: C++ Queue, Stack, and Priority Queue
date: 2026-07-21 10:00:00-0000
last_modified_at: 2026-08-17 00:00:00-0000
description: A practical guide to C++ queue, stack, and priority_queue adapters, including operation contracts, underlying containers, comparators, and complexity.
toc: true
toc_sticky: true
categories: C++
tags: [cpp, stl, containers, queue, stack, priority-queue]
---

## Overview

`std::queue`, `std::stack`, and `std::priority_queue` are container adapters. They expose a restricted interface over an underlying container. Use them when the restricted interface matches the algorithm.

An adapter does not expose iterators to its underlying container. The restriction is part of the abstraction: clients can observe and mutate only the end or priority position allowed by the adapter.

---

## Common Includes

{% highlight c++ %}
#include <functional>
#include <list>
#include <queue>
#include <set>
#include <stack>
#include <string>
#include <utility>
#include <vector>
{% endhighlight %}

---

## queue

`std::queue` is FIFO: first in, first out. It is common in BFS and stream processing.

{% highlight c++ %}
std::queue<int> q;

q.push(10);
q.push(20);

int front = q.front();
int back = q.back();
q.pop();

bool empty = q.empty();
int n = static_cast<int>(q.size());
{% endhighlight %}

`front()` and `back()` return references to elements. `pop()` removes the front element and returns nothing, so read or move the value before popping when it is needed. Calling `front()`, `back()`, or `pop()` on an empty queue is invalid.

BFS skeleton:

{% highlight c++ %}
std::vector<int> bfs(const std::vector<std::vector<int>>& adj, int start) {
  std::vector<int> order;
  std::vector<bool> visited(adj.size(), false);
  std::queue<int> q;

  q.push(start);
  visited[start] = true;

  while (!q.empty()) {
    int node = q.front();
    q.pop();
    order.push_back(node);

    for (int next : adj[node]) {
      if (!visited[next]) {
        visited[next] = true;
        q.push(next);
      }
    }
  }

  return order;
}
{% endhighlight %}

Important:

- `queue` does not support iteration.
- Access is through `front`, `back`, `push`, and `pop`.
- The default underlying container is `std::deque`.

---

## stack

`std::stack` is LIFO: last in, first out. It is useful for iterative DFS, parentheses validation, monotonic stacks, and backtracking.

{% highlight c++ %}
std::stack<int> st;

st.push(10);
st.push(20);

int top = st.top();
st.pop();

bool empty = st.empty();
int n = static_cast<int>(st.size());
{% endhighlight %}

`top()` returns a reference to the most recently pushed element. `pop()` removes it without returning it. Calling either operation on an empty stack is invalid.

Parentheses pattern:

{% highlight c++ %}
bool isValid(const std::string& s) {
  std::stack<char> st;

  for (char ch : s) {
    if (ch == '(' || ch == '[' || ch == '{') {
      st.push(ch);
    } else {
      if (st.empty()) {
        return false;
      }

      char open = st.top();
      st.pop();

      if ((ch == ')' && open != '(') ||
          (ch == ']' && open != '[') ||
          (ch == '}' && open != '{')) {
        return false;
      }
    }
  }

  return st.empty();
}
{% endhighlight %}

Important:

- `stack` does not support iteration.
- Access is through `top`, `push`, and `pop`.
- The default underlying container is `std::deque`.

---

## priority_queue

`std::priority_queue` is a heap-backed adapter. By default, it is a max heap.

{% highlight c++ %}
std::priority_queue<int> max_heap;

max_heap.push(5);
max_heap.push(1);
max_heap.push(10);

int largest = max_heap.top();             // 10
max_heap.pop();
{% endhighlight %}

`top()` exposes the highest-priority element as a const reference. It cannot be modified in place because changing it could violate the heap invariant. `pop()` removes it and returns nothing. Both require a non-empty priority queue.

Min heap:

{% highlight c++ %}
std::priority_queue<int, std::vector<int>, std::greater<int>> min_heap;

min_heap.push(5);
min_heap.push(1);
min_heap.push(10);

int smallest = min_heap.top();            // 1
{% endhighlight %}

Read the full type as:

{% highlight c++ %}
std::priority_queue<Element, Storage, Compare>
{% endhighlight %}

- `Element` is the stored value type.
- `Storage` is the underlying random-access sequence, normally `std::vector<Element>`.
- `Compare` defines which values have lower priority. `std::greater<Element>` therefore places the smallest value at `top()`.

Heap of pairs:

{% highlight c++ %}
using State = std::pair<int, int>;        // {distance, node}

std::priority_queue<State, std::vector<State>, std::greater<State>> pq;

pq.push({0, 1});
pq.push({5, 2});

auto [distance, node] = pq.top();
pq.pop();
{% endhighlight %}

### Comparator Objects and `operator()`

A class that overloads `operator()` is a function object, or functor. An instance can be called with normal function-call syntax:

{% highlight c++ %}
struct Compare {
  bool operator()(int left, int right) const {
    return left < right;
  }
};

bool result = Compare{}(2, 5);            // true
{% endhighlight %}

The container stores an instance of `Compare` and invokes it when ordering elements. The comparator must define a strict weak ordering. In particular:

- `compare(value, value)` must be false.
- If `compare(a, b)` is true, `compare(b, a)` must be false.
- The ordering must be transitive.

The comparator has the same formal meaning across ordered standard-library components: `compare(a, b)` means that `a` is ordered before `b`. What changes is the element each interface exposes.

- `std::set` iterates from the first element in that ordering.
- `std::priority_queue::top()` returns the last element in that ordering.

This produces the following behavior:

| Comparator expression | `std::set` iteration | `std::priority_queue::top()` |
|---|---|---|
| `left < right` | Ascending | Largest element |
| `left > right` | Descending | Smallest element |

For a priority queue, an equivalent operational model is: if `compare(a, b)` returns true, `a` has lower priority than `b` and should not be above it in the heap.

The same comparator type can therefore be used with both containers:

{% highlight c++ %}
struct Compare {
  bool operator()(int left, int right) const {
    return left < right;
  }
};

std::vector<int> values{3, 1, 4, 2};

std::set<int, Compare> ordered(values.begin(), values.end());
int first = *ordered.begin();             // 1

std::priority_queue<int, std::vector<int>, Compare> pending;
for (int value : values) {
  pending.push(value);
}
int highest = pending.top();              // 4
{% endhighlight %}

For `std::priority_queue`, a useful review question is: **Does `left` have lower priority than `right`?** Returning true places `left` below `right`.

### Comparator for Structured Values

The same rule applies to structured values:

{% highlight c++ %}
struct Compare {
  bool operator()(const std::pair<int, int>& a,
                  const std::pair<int, int>& b) const {
    return a.second > b.second;           // smaller second has higher priority
  }
};

std::priority_queue<
    std::pair<int, int>,
    std::vector<std::pair<int, int>>,
    Compare> pq;
{% endhighlight %}

Here, a pair with a larger `second` value has lower priority, so the pair with the smallest `second` value reaches `top()`.

The comparator can also be supplied to `std::set`, but there is an additional constraint: ordered sets use comparator equivalence to determine uniqueness. Two values are equivalent when both `compare(a, b)` and `compare(b, a)` are false. The comparator above would therefore treat every pair with the same `second` value as equivalent, regardless of `first`.

Add a tie-breaker when both fields are required to identify distinct set elements:

{% highlight c++ %}
struct SetCompare {
  bool operator()(const std::pair<int, int>& a,
                  const std::pair<int, int>& b) const {
    if (a.second != b.second) {
      return a.second < b.second;
    }
    return a.first < b.first;
  }
};
{% endhighlight %}

For more ordered-set operations, see [C++ Sets and Multisets]({% post_url cpp-reference/2026-07-20-cpp-sets %}).

Common uses:

- Dijkstra.
- Top K elements.
- Merge K sorted lists.
- Scheduling by priority.

### Underlying Containers and Invalidation

The underlying container is a template argument:

{% highlight c++ %}
std::queue<int, std::list<int>> linked_queue;
std::stack<int, std::vector<int>> vector_stack;
{% endhighlight %}

The container must supply the operations required by the adapter. `queue` needs access and mutation at opposite ends; `stack` needs access and mutation at the back; `priority_queue` needs random-access iteration and back insertion/removal.

Reference and pointer invalidation follows the underlying container's operations. The adapters do not expose iterators, but references obtained from `front()`, `back()`, or `top()` can still become invalid after a later mutation.

---

## Complexity

| Container | Access | Push | Pop |
|---|---:|---:|---:|
| `queue` | `O(1)` front/back | `O(1)` | `O(1)` |
| `stack` | `O(1)` top | `O(1)` | `O(1)` |
| `priority_queue` | `O(1)` top | `O(log n)` | `O(log n)` |

---

## Checklist

- Use `queue` for level-order traversal and BFS.
- Use `stack` for explicit DFS and nested-structure parsing.
- Use `priority_queue` when repeatedly extracting the current best element.
- Do not try to iterate `queue`, `stack`, or `priority_queue`.
- For a min heap, use `std::greater<T>` with an underlying `std::vector<T>`.
- Check `empty()` when an adapter may have no element before calling its access or removal functions.
- Do not retain element references across mutations without applying the underlying container's invalidation rules.

## Further Reading

- [C++ working draft: container adapters](https://eel.is/c++draft/container.adaptors)
- [C++ working draft: queue and priority queue](https://eel.is/c++draft/queue)
