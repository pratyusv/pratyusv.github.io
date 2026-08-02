---
layout: single
comments: true
title: "Next Greater Element II: Circular Monotonic Stack"
date: 2026-08-01 14:00:00-0000
categories: C++
tags: [cpp, algorithms, monotonic-stack, circular-array, interview]
---

## Problem

Given a circular integer array `nums`, return the next greater value for every position.

The next greater value is the first value encountered while moving forward that is strictly greater than the current value. Because the array is circular, moving past the last position continues from index `0`.

Return `-1` for a position that has no greater value.

Example:

```text
nums   = [1, 2, 1]
answer = [2, -1, 2]
```

- The first `1` finds `2` to its right.
- `2` has no greater value anywhere in the circle.
- The final `1` wraps around and finds `2`.

## Start with the Non-Circular Problem

Ignore the circular requirement temporarily and scan from left to right.

When a value is visited, some earlier elements may still be waiting for their next greater value. Keep their indices in a stack.

For each current value:

1. While the current value is greater than the value at the top index, resolve that index.
2. Remove every resolved index from the stack.
3. Add the current index because it must now wait for a greater value.

This is the standard forward monotonic-stack pattern.

## The Waiting Room Model

Think of the stack as a waiting room for indices whose answers are not known yet.

```text
Who enters the waiting room?
An index whose next greater value has not appeared.

When does an index leave?
When a strictly greater current value appears.
```

For `nums = [2, 1, 3]`:

```text
Read 2: index 0 waits.
Read 1: index 1 waits above index 0.
Read 3: 3 resolves index 1, then resolves index 0.
Read 3: index 2 waits, but nothing greater arrives.
```

The unresolved values in the stack are non-increasing from bottom to top. When a larger value arrives, it may resolve several smaller values at once.

## Store Indices, Not Values

The answer must be written into the correct result position. Storing an index gives access to both pieces of information:

```text
nums[index]   -> the value waiting for an answer
result[index] -> where its answer belongs
```

Duplicate values also remain distinct because their indices are different.

## Simulating the Circular Array

Conceptually duplicate the array:

```text
[1, 2, 1] -> [1, 2, 1, 1, 2, 1]
```

There is no need to allocate that second copy. Loop `2 * n` times and map each iteration back to the original array:

```text
current_index = iteration % n
```

The first pass adds every original index to the waiting room. The second pass only supplies wrapped values that may resolve the remaining indices.

Do not push indices during the second pass. They are duplicate views of positions already processed and do not need another answer.

## C++ Solution

{% highlight c++ %}
#include <stack>
#include <vector>

class Solution {
 public:
  std::vector<int> nextGreaterElements(std::vector<int>& nums) {
    int n = static_cast<int>(nums.size());
    std::vector<int> result(n, -1);
    std::stack<int> waiting;

    for (int iteration = 0; iteration < 2 * n; ++iteration) {
      int current_index = iteration % n;
      int current_value = nums[current_index];

      while (!waiting.empty() &&
             nums[waiting.top()] < current_value) {
        int resolved_index = waiting.top();
        waiting.pop();
        result[resolved_index] = current_value;
      }

      if (iteration < n) {
        waiting.push(current_index);
      }
    }

    return result;
  }
};
{% endhighlight %}

## Trace: `[1, 2, 1]`

The result starts as `[-1, -1, -1]`.

| Iteration | Current value | Waiting indices before | Action | Result |
|---:|---:|---|---|---|
| `0` | `1` | `[]` | Push index `0` | `[-1, -1, -1]` |
| `1` | `2` | `[0]` | Resolve index `0`; push `1` | `[2, -1, -1]` |
| `2` | `1` | `[1]` | Push index `2` | `[2, -1, -1]` |
| `3` | `1` | `[1, 2]` | No value is resolved | `[2, -1, -1]` |
| `4` | `2` | `[1, 2]` | Resolve index `2` | `[2, -1, 2]` |
| `5` | `1` | `[1]` | No value is resolved | `[2, -1, 2]` |

Index `1`, whose value is `2`, remains in the waiting room because no strictly greater value exists. Its result correctly stays `-1`.

## Why the Comparison Uses `<`

The problem asks for a strictly greater value.

```c++
nums[waiting.top()] < current_value
```

This condition is already correct. Equal values must not resolve each other.

For `nums = [1, 1]`, neither `1` is greater than the other, so both answers remain `-1`. Changing the comparison to `<=` would incorrectly treat an equal value as greater.

## Why It Is Correct

An index remains in the stack until the first strictly greater value encountered during forward circular traversal appears. At that moment, the while loop removes the index and records the current value.

No earlier value could have been its answer; otherwise the index would already have been removed. The doubled scan exposes every value that can be reached within one complete wrap. Therefore every resolved answer is the first greater value, and every unresolved index correctly receives `-1`.

## Complexity

Every original index is pushed once and popped at most once.

- Time: `O(n)`
- Space: `O(n)`

The loop runs `2 * n` iterations, but constant factors do not change the asymptotic complexity.

## Reconstructing It in an Interview

Remember three questions rather than the code:

1. **Who is waiting for an answer?** Indices in the stack.
2. **When does their wait end?** When a strictly greater value appears.
3. **How is circular traversal simulated?** Scan twice and use modulo.

That reasoning is enough to rebuild the implementation without memorizing it.

The same waiting-room model applies to Daily Temperatures, Next Smaller Element, Stock Span, and other nearest-greater or nearest-smaller problems.
