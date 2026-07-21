---
layout: single
comments: true
title: C++ Containers Overview
date: 2026-07-18 10:00:00-0000
categories: C++
tags: [cpp, stl, containers, vector, string, map, queue, stack, priority-queue]
---

## Overview

This is a quick reference for common C++ STL containers. The focus is on the operations that are easy to forget during implementation: insert, delete, lookup, iteration, and common patterns.

---

## Common Includes

{% highlight c++ %}
#include <algorithm>
#include <deque>
#include <functional>
#include <iostream>
#include <map>
#include <queue>
#include <set>
#include <stack>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>
{% endhighlight %}

---

## Container Selection

| Need | Container |
|---|---|
| Dynamic array, index access | `vector` |
| Text manipulation | `string` |
| Key-value lookup, sorted keys | `map` |
| Key-value lookup, average `O(1)` | `unordered_map` |
| Unique values, sorted order | `set` |
| Unique values, average `O(1)` | `unordered_set` |
| FIFO processing | `queue` |
| LIFO processing | `stack` |
| Max/min element access | `priority_queue` |
| Push/pop from both ends | `deque` |

---

## Vector

`std::vector` is the default container for dynamic arrays. It supports fast random access and amortized `O(1)` push at the end.

{% highlight c++ %}
#include <vector>

std::vector<int> nums;
std::vector<int> fixed_size(5, 0);        // [0, 0, 0, 0, 0]
std::vector<int> values = {1, 2, 3};

nums.push_back(10);                       // add at end
nums.emplace_back(20);                    // construct at end
nums.pop_back();                          // remove last

int first = nums[0];                      // no bounds check
int safe = nums.at(0);                    // throws if invalid

int n = static_cast<int>(nums.size());
bool empty = nums.empty();
{% endhighlight %}

Insert and erase:

{% highlight c++ %}
std::vector<int> nums = {1, 2, 4};

nums.insert(nums.begin() + 2, 3);         // [1, 2, 3, 4]
nums.erase(nums.begin() + 1);             // erase value at index 1 -> [1, 3, 4]
nums.erase(nums.begin() + 1, nums.end()); // erase range [1, end)

nums.clear();                             // remove all elements
{% endhighlight %}

Find and remove by value:

{% highlight c++ %}
std::vector<int> nums = {1, 2, 3, 2};

auto it = std::find(nums.begin(), nums.end(), 2);
if (it != nums.end()) {
  nums.erase(it);                         // removes first 2
}

nums.erase(std::remove(nums.begin(), nums.end(), 2), nums.end());
{% endhighlight %}

Sort and reverse:

{% highlight c++ %}
std::sort(nums.begin(), nums.end());
std::sort(nums.rbegin(), nums.rend());    // descending
std::reverse(nums.begin(), nums.end());
{% endhighlight %}

Common patterns:

{% highlight c++ %}
for (int x : nums) {
  // read x
}

for (int& x : nums) {
  x *= 2;                                 // modify elements
}

for (int i = 0; i < static_cast<int>(nums.size()); ++i) {
  // index-based access
}
{% endhighlight %}

Complexity:

- Index access: `O(1)`.
- Push at end: `O(1)` amortized.
- Insert/delete in middle: `O(n)`.
- Search unsorted vector: `O(n)`.

---

## String

`std::string` behaves like a character vector with string-specific helpers.

{% highlight c++ %}
#include <string>

std::string s = "hello";

s.push_back('!');
s.pop_back();
s += " world";

char c = s[0];
int n = static_cast<int>(s.size());
bool empty = s.empty();
{% endhighlight %}

Substring, find, erase, insert:

{% highlight c++ %}
std::string s = "abcdef";

std::string part = s.substr(1, 3);        // "bcd"

size_t pos = s.find("cd");
if (pos != std::string::npos) {
  s.erase(pos, 2);                        // remove "cd"
}

s.insert(2, "XX");                        // insert at index 2
{% endhighlight %}

Convert:

{% highlight c++ %}
int x = std::stoi("123");
long long y = std::stoll("123456789");
std::string text = std::to_string(42);
{% endhighlight %}

Sort characters:

{% highlight c++ %}
std::string s = "dcba";
std::sort(s.begin(), s.end());            // "abcd"
{% endhighlight %}

Frequency array for lowercase letters:

{% highlight c++ %}
std::vector<int> freq(26, 0);

for (char ch : s) {
  ++freq[ch - 'a'];
}
{% endhighlight %}

Common checks:

{% highlight c++ %}
if (s.find("abc") != std::string::npos) {
  // substring exists
}

if (!s.empty() && s.back() == '/') {
  s.pop_back();
}
{% endhighlight %}

---

## Pair

`std::pair` is useful for coordinates, heap states, intervals, and key-value style return values.

Create and access:

{% highlight c++ %}
#include <utility>

std::pair<int, int> p = {2, 5};
std::pair<std::string, int> item = std::make_pair("apple", 3);

int a = p.first;
int b = p.second;

auto [x, y] = p;
{% endhighlight %}

Update values:

{% highlight c++ %}
std::pair<int, int> p = {1, 2};

p.first = 10;
p.second = 20;
{% endhighlight %}

Vector of pairs:

{% highlight c++ %}
std::vector<std::pair<int, int>> intervals = {
    std::make_pair(1, 3),
    std::make_pair(2, 4),
    std::make_pair(6, 8)};

std::sort(intervals.begin(), intervals.end());
{% endhighlight %}

Default sorting is lexicographic:

- Sort by `first`.
- If `first` is equal, sort by `second`.

Custom sort:

{% highlight c++ %}
std::sort(intervals.begin(), intervals.end(),
          [](const auto& left, const auto& right) {
            return left.second < right.second;
          });
{% endhighlight %}

Sort by first ascending, second descending:

{% highlight c++ %}
std::sort(intervals.begin(), intervals.end(),
          [](const auto& left, const auto& right) {
            if (left.first == right.first) {
              return left.second > right.second;
            }
            return left.first < right.first;
          });
{% endhighlight %}

Pair in a map:

{% highlight c++ %}
std::map<std::pair<int, int>, int> grid_count;

grid_count[{1, 2}]++;
grid_count[{3, 4}] = 10;

if (grid_count.find({1, 2}) != grid_count.end()) {
  int count = grid_count[{1, 2}];
}
{% endhighlight %}

Pair in a queue:

{% highlight c++ %}
std::queue<std::pair<int, int>> q;

q.push({0, 0});

auto [row, col] = q.front();
q.pop();
{% endhighlight %}

Pair in a priority queue:

{% highlight c++ %}
using State = std::pair<int, int>;        // {distance, node}

std::priority_queue<State, std::vector<State>, std::greater<State>> pq;

pq.push({0, 1});
pq.push({5, 2});

auto [distance, node] = pq.top();
pq.pop();
{% endhighlight %}

Common uses:

- Coordinates: `{row, col}`.
- Intervals: `{start, end}`.
- Heap state: `{cost, node}`.
- Frequency pairs: `{value, count}`.

---

## Map

`std::map` stores keys in sorted order. Operations are `O(log n)`.

{% highlight c++ %}
#include <map>

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

Useful ordered operations:

{% highlight c++ %}
std::map<int, std::string> mp;
mp[10] = "a";
mp[20] = "b";
mp[30] = "c";

auto it1 = mp.lower_bound(20);            // first key >= 20
auto it2 = mp.upper_bound(20);            // first key > 20
{% endhighlight %}

Note:

- `mp[key]` inserts a default value if `key` does not exist.
- Use `find` or `contains` when you only want to check existence.

{% highlight c++ %}
if (mp.find(key) != mp.end()) {
  // exists
}

// C++20
if (mp.contains(key)) {
  // exists
}
{% endhighlight %}

---

## Unordered Map

`std::unordered_map` is the default hash map. Operations are `O(1)` average-case.

{% highlight c++ %}
#include <unordered_map>

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

When to use:

- Use `unordered_map` for fast lookup when key order does not matter.
- Use `map` when sorted order, predecessor, successor, or range queries matter.

---

## Set

`std::set` stores unique values in sorted order. Operations are `O(log n)`.

{% highlight c++ %}
#include <set>

std::set<int> seen;

seen.insert(5);
seen.insert(2);
seen.insert(5);                           // duplicate ignored

if (seen.find(2) != seen.end()) {
  // exists
}

seen.erase(5);
{% endhighlight %}

Ordered operations:

{% highlight c++ %}
auto it = seen.lower_bound(10);           // first value >= 10
auto jt = seen.upper_bound(10);           // first value > 10
{% endhighlight %}

Iterate sorted:

{% highlight c++ %}
for (int x : seen) {
  // ascending order
}
{% endhighlight %}

---

## Unordered Set

`std::unordered_set` stores unique values with average `O(1)` insert, erase, and lookup.

{% highlight c++ %}
#include <unordered_set>

std::unordered_set<int> seen;

seen.insert(10);
seen.insert(20);

if (seen.find(10) != seen.end()) {
  // exists
}

seen.erase(20);
{% endhighlight %}

Duplicate detection:

{% highlight c++ %}
bool containsDuplicate(const std::vector<int>& nums) {
  std::unordered_set<int> seen;

  for (int x : nums) {
    if (seen.find(x) != seen.end()) {
      return true;
    }
    seen.insert(x);
  }

  return false;
}
{% endhighlight %}

Use `unordered_set` when order does not matter and fast existence checks are needed.

---

## Queue

`std::queue` is FIFO: first in, first out. It is common in BFS.

{% highlight c++ %}
#include <queue>

std::queue<int> q;

q.push(10);
q.push(20);

int front = q.front();
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
- Access only through `front`, `back`, `push`, and `pop`.

---

## Stack

`std::stack` is LIFO: last in, first out. It is useful for DFS, parentheses, monotonic stacks, and backtracking.

{% highlight c++ %}
#include <stack>

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
- Access only through `top`, `push`, and `pop`.

---

## Priority Queue

`std::priority_queue` is a heap-backed container adapter. By default, it is a max heap.

{% highlight c++ %}
#include <queue>
#include <vector>

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

auto [dist, node] = pq.top();
pq.pop();
{% endhighlight %}

Custom comparator:

{% highlight c++ %}
struct Compare {
  bool operator()(const std::pair<int, int>& a,
                  const std::pair<int, int>& b) const {
    return a.second > b.second;           // smaller second value has higher priority
  }
};

std::priority_queue<
    std::pair<int, int>,
    std::vector<std::pair<int, int>>,
    Compare> pq;
{% endhighlight %}

Complexity:

- `top`: `O(1)`.
- `push`: `O(log n)`.
- `pop`: `O(log n)`.

Common uses:

- Dijkstra.
- Top K elements.
- Merge K sorted lists.
- Scheduling by priority.

---

## Deque

`std::deque` supports efficient push and pop from both ends.

{% highlight c++ %}
#include <deque>

std::deque<int> dq;

dq.push_back(10);
dq.push_front(5);

int front = dq.front();
int back = dq.back();

dq.pop_front();
dq.pop_back();
{% endhighlight %}

Sliding window maximum pattern:

{% highlight c++ %}
std::vector<int> maxSlidingWindow(const std::vector<int>& nums, int k) {
  std::vector<int> ans;
  std::deque<int> dq;                     // stores indices

  for (int i = 0; i < static_cast<int>(nums.size()); ++i) {
    while (!dq.empty() && dq.front() <= i - k) {
      dq.pop_front();
    }

    while (!dq.empty() && nums[dq.back()] <= nums[i]) {
      dq.pop_back();
    }

    dq.push_back(i);

    if (i >= k - 1) {
      ans.push_back(nums[dq.front()]);
    }
  }

  return ans;
}
{% endhighlight %}

Use `deque` when both ends matter. Use `queue` when only FIFO behavior is needed.

---

## Sorting Helpers

Ascending:

{% highlight c++ %}
std::sort(nums.begin(), nums.end());
{% endhighlight %}

Descending:

{% highlight c++ %}
std::sort(nums.begin(), nums.end(), std::greater<int>());
{% endhighlight %}

Custom:

{% highlight c++ %}
std::sort(items.begin(), items.end(),
          [](const auto& a, const auto& b) {
            return a.first < b.first;
          });
{% endhighlight %}

Stable sort:

{% highlight c++ %}
std::stable_sort(items.begin(), items.end());
{% endhighlight %}

---

## Binary Search Helpers

Use these on sorted ranges.

{% highlight c++ %}
std::vector<int> nums = {1, 2, 4, 4, 5};

bool exists = std::binary_search(nums.begin(), nums.end(), 4);

auto lower = std::lower_bound(nums.begin(), nums.end(), 4); // first >= 4
auto upper = std::upper_bound(nums.begin(), nums.end(), 4); // first > 4

int first_index = static_cast<int>(lower - nums.begin());
int after_last_index = static_cast<int>(upper - nums.begin());
int count = after_last_index - first_index;
{% endhighlight %}

---

## Common Erase Patterns

Erase from vector by index:

{% highlight c++ %}
nums.erase(nums.begin() + index);
{% endhighlight %}

Erase from vector by value:

{% highlight c++ %}
nums.erase(std::remove(nums.begin(), nums.end(), value), nums.end());
{% endhighlight %}

Erase from map/set:

{% highlight c++ %}
mp.erase(key);
seen.erase(value);
{% endhighlight %}

Erase while iterating:

{% highlight c++ %}
for (auto it = mp.begin(); it != mp.end();) {
  if (should_delete(it->first, it->second)) {
    it = mp.erase(it);
  } else {
    ++it;
  }
}
{% endhighlight %}

---

## Complexity Summary

| Container | Access | Insert | Delete | Lookup |
|---|---:|---:|---:|---:|
| `vector` | `O(1)` by index | `O(1)` end, `O(n)` middle | `O(1)` end, `O(n)` middle | `O(n)` |
| `string` | `O(1)` by index | `O(1)` end, `O(n)` middle | `O(1)` end, `O(n)` middle | `O(n)` |
| `map` | - | `O(log n)` | `O(log n)` | `O(log n)` |
| `unordered_map` | - | `O(1)` avg | `O(1)` avg | `O(1)` avg |
| `set` | - | `O(log n)` | `O(log n)` | `O(log n)` |
| `unordered_set` | - | `O(1)` avg | `O(1)` avg | `O(1)` avg |
| `queue` | front/back only | `O(1)` | `O(1)` | - |
| `stack` | top only | `O(1)` | `O(1)` | - |
| `priority_queue` | top only | `O(log n)` | `O(log n)` | - |
| `deque` | `O(1)` by index | `O(1)` ends | `O(1)` ends | `O(n)` |

---

## Interview Checklist

- Use `vector` when index access and traversal are central.
- Use `unordered_map` or `unordered_set` when fast lookup is central and order does not matter.
- Use `map` or `set` when sorted order or range queries matter.
- Use `queue` for BFS.
- Use `stack` for iterative DFS and monotonic stack patterns.
- Use `priority_queue` for repeated best/min/max extraction.
- Prefer `empty()` over `size() == 0`.
- Be careful: `map[key]` and `unordered_map[key]` insert default values when the key is missing.
