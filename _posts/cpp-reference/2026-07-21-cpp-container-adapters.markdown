---
layout: single
comments: true
title: C++ Queue, Stack, and Priority Queue
date: 2026-07-21 10:00:00-0000
categories: C++
tags: [cpp, stl, containers, queue, stack, priority-queue]
---

## Overview

`std::queue`, `std::stack`, and `std::priority_queue` are container adapters. They expose a restricted interface over an underlying container. Use them when the restricted interface matches the algorithm.

---

## Common Includes

{% highlight c++ %}
#include <functional>
#include <queue>
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

Min heap:

{% highlight c++ %}
std::priority_queue<int, std::vector<int>, std::greater<int>> min_heap;

min_heap.push(5);
min_heap.push(1);
min_heap.push(10);

int smallest = min_heap.top();            // 1
{% endhighlight %}

Heap of pairs:

{% highlight c++ %}
using State = std::pair<int, int>;        // {distance, node}

std::priority_queue<State, std::vector<State>, std::greater<State>> pq;

pq.push({0, 1});
pq.push({5, 2});

auto [distance, node] = pq.top();
pq.pop();
{% endhighlight %}

Custom comparator:

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

Common uses:

- Dijkstra.
- Top K elements.
- Merge K sorted lists.
- Scheduling by priority.

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
- For min heaps, remember `std::greater<T>` and the full template type.
