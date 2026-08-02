---
layout: single
comments: true
title: Detect Squares
date: 2026-08-01 13:00:00-0000
categories: C++
tags: [cpp, algorithms, geometry, counting, hashmap, interview]
---

## Problem

Design a data structure that stores points on a two-dimensional integer grid and supports two operations:

- `add(point)`: add one occurrence of `[x, y]`.
- `count(point)`: return the number of axis-aligned squares that use `[x, y]` as one corner and three stored points as the other corners.

Points may be added more than once. Every occurrence is distinct and must contribute to the result. Coordinates satisfy `0 <= x, y <= 1000`.

Example:

```text
add([3, 10])
add([11, 2])
add([3, 2])

count([11, 10]) -> 1

add([11, 2])
count([11, 10]) -> 2
```

The second result is `2` because `[11, 2]` has been added twice.

## Start from the Opposite Corner

Let the query point be:

```text
P = (px, py)
```

Treat each stored point as the possible diagonally opposite corner:

```text
D = (x, y)
```

`P` and `D` form the diagonal of an axis-aligned square only when:

```text
x != px
y != py
abs(x - px) == abs(y - py)
```

The first two conditions reject zero-area shapes. The final condition requires equal horizontal and vertical side lengths.

Once `D` is fixed, the other two corners are determined:

```text
B = (x, py)
C = (px, y)
```

The number of squares contributed by this diagonal occurrence is:

```text
frequency(B) * frequency(C)
```

## Data Structures

Use both:

1. A frequency table for constant-time corner lookup.
2. A list containing every added point occurrence for diagonal enumeration.

Keeping duplicates in the list is intentional. If a diagonal point was added three times, it represents three possible choices for that corner.

## C++ Solution

{% highlight c++ %}
#include <cstdlib>
#include <utility>
#include <vector>

class DetectSquares {
 private:
  int counts[1001][1001] = {};
  std::vector<std::pair<int, int>> points;

 public:
  DetectSquares() = default;

  void add(std::vector<int> point) {
    int x = point[0];
    int y = point[1];

    ++counts[x][y];
    points.push_back({x, y});
  }

  int count(std::vector<int> point) {
    int px = point[0];
    int py = point[1];
    int total_squares = 0;

    for (const auto& [x, y] : points) {
      if (x == px || y == py) {
        continue;
      }

      if (std::abs(x - px) != std::abs(y - py)) {
        continue;
      }

      total_squares += counts[x][py] * counts[px][y];
    }

    return total_squares;
  }
};
{% endhighlight %}

## Why It Is Correct

For every square containing the query point, there is exactly one opposite diagonal corner. The loop eventually examines that stored corner.

The distance check accepts it because both side lengths of a square are equal. The remaining two corners are then uniquely determined as `(x, py)` and `(px, y)`. Multiplying their frequencies counts every possible choice of those stored corner occurrences.

Conversely, every contribution added by the algorithm has four distinct axis-aligned corners and equal side lengths, so it represents a valid square.

Duplicate diagonal points are handled because each occurrence appears separately in `points`. Duplicate side corners are handled by the frequency multiplication. Therefore every valid square occurrence is counted exactly once.

## Complexity

Let `added_points` be the total number of calls to `add`, including duplicates.

- `add`: `O(1)`
- `count`: `O(added_points)`
- Space: `O(1001 * 1001 + added_points)`

The dense frequency table uses approximately four megabytes when `int` is four bytes. This is reasonable because the coordinate range is fixed and small.

## Design Notes

- The query point does not need to have been added previously.
- Squares must be axis-aligned; rotated squares are not counted.
- Keeping duplicate points in the list is required for multiplicity.
- The dense array is a large object. The typical judge creates `DetectSquares` dynamically. For unrestricted coordinates or sparse data, use a hash map instead.
