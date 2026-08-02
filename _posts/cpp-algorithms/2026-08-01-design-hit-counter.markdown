---
layout: single
comments: true
title: Design a Hit Counter
date: 2026-08-01 10:00:00-0000
categories: C++
tags: [cpp, algorithms, design, ring-buffer, time-window, interview]
---

## Problem

Design a counter that records events and reports how many occurred during the most recent five minutes.

Implement:

- `hit(timestamp)`: record one event at `timestamp`.
- `getHits(timestamp)`: return the number of events in the inclusive time range `[timestamp - 299, timestamp]`.

Assume timestamps are positive integer seconds and method calls arrive in non-decreasing timestamp order. Multiple events may occur during the same second.

Example:

```text
hit(1)
hit(1)
hit(300)

getHits(300) -> 3
getHits(301) -> 1
```

At time `301`, events from timestamp `1` have left the 300-second window.

## Fixed-Size Ring Buffer

Only 300 distinct seconds can contribute to an answer. Map every timestamp to one of 300 slots:

```text
slot = timestamp % 300
```

Each slot stores:

- the exact timestamp currently represented by that slot,
- the number of hits recorded during that second.

When a timestamp maps to a slot:

- If the slot already represents that timestamp, increment its count.
- Otherwise, the previous timestamp in that slot is at least 300 seconds older under the chronological-call contract. Replace both the timestamp and count.

Storing the exact timestamp is necessary because timestamps separated by 300 seconds map to the same slot.

## C++ Solution

{% highlight c++ %}
#include <array>

class HitCounter {
 public:
  void hit(int timestamp) {
    const int slot = timestamp % kWindowSeconds;

    if (timestamps_[slot] == timestamp) {
      ++counts_[slot];
      return;
    }

    timestamps_[slot] = timestamp;
    counts_[slot] = 1;
  }

  int getHits(int timestamp) const {
    int total = 0;

    for (int slot = 0; slot < kWindowSeconds; ++slot) {
      const int age = timestamp - timestamps_[slot];
      if (counts_[slot] != 0 && age >= 0 && age < kWindowSeconds) {
        total += counts_[slot];
      }
    }

    return total;
  }

 private:
  static constexpr int kWindowSeconds = 300;
  std::array<int, kWindowSeconds> timestamps_{};
  std::array<int, kWindowSeconds> counts_{};
};
{% endhighlight %}

## Why It Is Correct

Every hit is stored in the unique slot determined by its timestamp modulo 300. Hits from the same second share a timestamp and are accumulated in that slot.

When a slot is reused, its previous timestamp cannot belong to the current 300-second window, so replacing it cannot remove a relevant hit. During a query, a slot contributes exactly when its stored timestamp has an age between `0` and `299` seconds.

Summing those counts therefore returns exactly the hits in `[timestamp - 299, timestamp]`.

## Complexity

- `hit`: `O(1)`
- `getHits`: `O(300)`, which is constant for a fixed five-minute window
- Space: `O(300)`

The design does not grow with traffic volume. One million events in the same second still consume one slot.

## Design Boundaries

- The implementation assumes calls are chronological. Supporting arbitrary out-of-order writes requires a different retention strategy.
- The class is not thread-safe. Concurrent writers need synchronization or partitioned counters.
- Changing the reporting window requires changing the ring size.
- A queue of timestamp/count pairs is an alternative when the active window is sparse or configurable.
