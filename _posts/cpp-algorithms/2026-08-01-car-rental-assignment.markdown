---
layout: single
comments: true
title: Assign Rental Cars to Time-Window Requests
date: 2026-08-01 09:00:00-0000
categories: C++
tags: [cpp, algorithms, scheduling, priority-queue, intervals, greedy, interview]
---

## Problem

A rental service owns `car_count` identical cars with IDs from `0` to `car_count - 1`.

Each request contains:

- a unique request ID,
- a pickup time,
- a return time greater than its pickup time.

Process requests in chronological pickup order. A car can serve another request when its previous return time is less than or equal to the new pickup time.

For each request:

1. Reuse the assigned car with the earliest return time when one is available.
2. Otherwise, allocate the next unused car ID.
3. If all cars are unavailable, assign `-1`.

Return assignments in the same order as the input. Requests with the same pickup time retain their input order.

Example:

```text
car_count = 2

requests = [
  {id: 10, pickup: 1, return: 4},
  {id: 11, pickup: 2, return: 5},
  {id: 12, pickup: 4, return: 7},
  {id: 13, pickup: 4, return: 6}
]

assignments = [
  {request: 10, car: 0},
  {request: 11, car: 1},
  {request: 12, car: 0},
  {request: 13, car: -1}
]
```

Car `0` returns exactly when request `12` begins, so it can be reused. Car `1` is still busy at time `4`, leaving no car for request `13`.

## Min-Heap Invariant

Maintain a min-heap of:

```text
{next_available_time, car_id}
```

The heap contains one entry for every car that has already been introduced. Its top is the assigned car that becomes available first.

For a request at `pickup`:

- If the top return time is at most `pickup`, reuse that car.
- If the top car is still busy but an unused car remains, allocate a new ID.
- If the top car is busy and every car has been introduced, no car is available.

Only one available car needs to be removed because each request requires exactly one car.

## C++ Solution

{% highlight c++ %}
#include <algorithm>
#include <cstddef>
#include <functional>
#include <numeric>
#include <queue>
#include <utility>
#include <vector>

struct Request {
  int id;
  int pickup;
  int return_time;
};

struct Assignment {
  int request_id;
  int car_id;
};

class Solution {
 public:
  std::vector<Assignment> assignCars(
      int car_count, const std::vector<Request>& requests) {
    std::vector<std::size_t> processing_order(requests.size());
    std::iota(processing_order.begin(), processing_order.end(), 0);

    std::stable_sort(
        processing_order.begin(),
        processing_order.end(),
        [&requests](std::size_t left_index, std::size_t right_index) {
          return requests[left_index].pickup < requests[right_index].pickup;
        });

    using Availability = std::pair<int, int>;  // {return_time, car_id}
    std::priority_queue<
        Availability,
        std::vector<Availability>,
        std::greater<Availability>>
        availability;

    std::vector<Assignment> assignments(
        requests.size(), Assignment{-1, -1});
    int next_car_id = 0;

    for (std::size_t request_index : processing_order) {
      const Request& request = requests[request_index];
      int car_id = -1;

      if (!availability.empty() &&
          availability.top().first <= request.pickup) {
        car_id = availability.top().second;
        availability.pop();
      } else if (next_car_id < car_count) {
        car_id = next_car_id;
        ++next_car_id;
      }

      assignments[request_index] = {request.id, car_id};

      if (car_id != -1) {
        availability.push({request.return_time, car_id});
      }
    }

    return assignments;
  }
};
{% endhighlight %}

## Why It Is Correct

Before each request, the heap stores the next availability time for every car that has been allocated so far. Because it is a min-heap, its top has the earliest return time.

If that time is at most the request's pickup time, the top car is available and can be reused. If it is later than the pickup time, every allocated car is still busy. A new car can then be used only when fewer than `car_count` IDs have been allocated. Otherwise rejection is necessary.

After a successful assignment, pushing the request's return time restores the invariant for the next request. Processing requests chronologically makes the argument valid for the complete sequence.

## Complexity

Let `request_count` be the number of requests.

- Sorting: `O(request_count log request_count)`
- Heap work: `O(request_count log car_count)`
- Total: `O(request_count log request_count)`
- Space: `O(request_count + car_count)`

The result and processing-order arrays account for the `O(request_count)` term.

## Policy Variations

This implementation reuses the car with the earliest return time. The exact assignment policy must be part of the problem contract.

If the requirement is instead to choose the smallest available car ID, use two min-heaps:

1. A busy heap ordered by return time.
2. An available heap ordered by car ID.

Before each request, move every returned car from the busy heap into the available heap, then take the smallest ID.

If the objective is to maximize the number of accepted requests rather than process every request as it arrives, this greedy contract is insufficient. That is a different interval-scheduling optimization problem.
