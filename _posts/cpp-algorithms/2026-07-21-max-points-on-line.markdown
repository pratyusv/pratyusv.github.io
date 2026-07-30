---
layout: single
comments: true
title: Max Points on a Line
date: 2026-07-21 10:00:00-0000
categories: C++
tags: [cpp, algorithms, geometry, hashmap, gcd]
---

## Problem

Given an array of points where `points[i] = [xi, yi]` represents a point on the X-Y plane, return the maximum number of points that lie on the same straight line.

Example:

```text
points = [[1,1], [2,2], [3,3]]
answer = 3
```

## Line Equation Approach

A line is often written as:

```text
y = mx + c
```

In that representation, `m` is the slope and `c` is the y-intercept. Two points belong to the same non-vertical line when they share both values.

This is a valid mathematical model, but it is not the most convenient representation for this problem:

- Vertical lines have `dx = 0`, so slope division fails.
- Slopes like `1 / 3` cannot be represented exactly with integer division.
- Floating-point slope comparisons can be fragile.
- Tracking both slope and intercept is more state than the counting problem requires.

Instead of globally identifying each line by slope and intercept, we can fix one point at a time and only compare directions from that point.

## Anchor Point Intuition

Pick one point as an anchor, call it `Pi`.

Now look at every other point `Pj` from the perspective of `Pi`.

Every `Pj` creates a vector:

```text
dx = xj - xi
dy = yj - yi
```

If two points create the same direction vector after simplification, then they lie on the same line through `Pi`.

For example, with anchor `(1,1)`:

```text
Pj = (3,5) gives dx = 2, dy = 4
Pk = (5,9) gives dx = 4, dy = 8
```

Both represent the same direction:

```text
(2,4) -> (1,2)
(4,8) -> (1,2)
```

So `(1,1)`, `(3,5)`, and `(5,9)` are on the same line.

The useful shift is:

```text
When the anchor is fixed, the intercept is already fixed.
Only the direction matters.
```

## What One Anchor Can See

One confusing part is this: an anchor only counts lines that pass through that anchor.

If `Pi` is not on the line containing points `C, D, E, F, G`, then the vectors from `Pi` to those points will fan out in different directions. That anchor will not discover the `C-D-E-F-G` line.

That is fine because the algorithm tries every point as the anchor.

When the outer loop eventually chooses `C` as the anchor, the vectors from `C` to `D, E, F, G` all have the same reduced direction. At that moment the line is counted.

Rule:

```text
Any line with k points will be discovered when any one of those k points is used as the anchor.
```

## Reduced Vector as the Hash Key

Instead of storing slope as a `double`, store the direction as a reduced integer pair.

For each pair of points:

```text
dx = xj - xi
dy = yj - yi
g = gcd(abs(dx), abs(dy))
key = (dx / g, dy / g)
```

Examples:

```text
(dx, dy) = (2, 4)   -> gcd = 2 -> key = (1, 2)
(dx, dy) = (4, 8)   -> gcd = 4 -> key = (1, 2)
(dx, dy) = (3, 0)   -> gcd = 3 -> key = (1, 0)
(dx, dy) = (0, 5)   -> gcd = 5 -> key = (0, 1)
```

This avoids both integer truncation and floating-point precision.

## The Sign Problem

A line has no direction, but a vector does.

From the same anchor:

```text
point to the upper-right -> key = (1, 1)
point to the lower-left  -> key = (-1, -1)
```

Those are the same geometric line, but a hash map sees two different keys.

So we need one canonical direction for every line.

The convention is:

```text
Make every vector point right.
If the line is vertical, make it point up.
```

![Vector sign normalization](/assets/img/algos/max_line_on_points_condition.png)

With arbitrary point order, this is the normalization:

{% highlight c++ %}
if (dx < 0 || (dx == 0 && dy < 0)) {
  dx = -dx;
  dy = -dy;
}
{% endhighlight %}

This condition means:

- If `dx < 0`, the vector points left, so flip it.
- If `dx == 0 && dy < 0`, the vector is vertical and points down, so flip it up.
- If `dy == 0`, no special case is needed. A horizontal vector with negative `dx` is already handled by `dx < 0`.

![Vertical line normalization](/assets/img/algos/max_points_on_line_vertical_lines.svg)

## Algorithm

For every point:

1. Treat it as the anchor.
2. Create a fresh map from reduced vector to count.
3. Compare it with every later point.
4. Reduce `(dx, dy)` by `gcd(dx, dy)`.
5. Normalize the reduced vector so it points right, or up if vertical.
6. Increment the count for that normalized vector.
7. The best count for this anchor plus one gives the number of points on that line.

The `+1` is for the anchor itself.

## C++ Solution

{% highlight c++ %}
#include <algorithm>
#include <cstdlib>
#include <map>
#include <utility>
#include <vector>

class Solution {
public:
  int getGCD(int a, int b) {
    a = std::abs(a);
    b = std::abs(b);

    while (b != 0) {
      int temp = b;
      b = a % b;
      a = temp;
    }

    return a;
  }

  int maxPoints(std::vector<std::vector<int>>& points) {
    int n = static_cast<int>(points.size());
    if (n <= 2) {
      return n;
    }

    int answer = 1;

    for (int i = 0; i < n; ++i) {
      std::map<std::pair<int, int>, int> direction_count;
      int best_from_anchor = 0;

      for (int j = i + 1; j < n; ++j) {
        int dx = points[j][0] - points[i][0];
        int dy = points[j][1] - points[i][1];
        int g = getGCD(dx, dy);

        dx /= g;
        dy /= g;

        if (dx < 0 || (dx == 0 && dy < 0)) {
          dx = -dx;
          dy = -dy;
        }

        std::pair<int, int> direction = {dx, dy};
        ++direction_count[direction];

        best_from_anchor = std::max(best_from_anchor, direction_count[direction]);
      }

      answer = std::max(answer, best_from_anchor + 1);
    }

    return answer;
  }
};
{% endhighlight %}

## Why the Map Resets for Each Anchor

The map must be created inside the outer loop.

Slope or direction is relative to the current anchor. A direction key like `(1, 2)` only means "a line with this direction passing through this anchor."

The same direction from a different anchor may describe a parallel but different line.

So this is correct:

{% highlight c++ %}
for (int i = 0; i < n; ++i) {
  std::map<std::pair<int, int>, int> direction_count;
  ...
}
{% endhighlight %}

This is wrong:

{% highlight c++ %}
std::map<std::pair<int, int>, int> direction_count;

for (int i = 0; i < n; ++i) {
  ...
}
{% endhighlight %}

because counts from one anchor would leak into another anchor.

## Complexity

The nested loops check every pair of points, which costs `O(n^2)`.

The GCD calculation costs `O(log C)`, where `C` is the coordinate range.

Using `std::map`, each update costs `O(log n)`, so the total time is:

```text
O(n^2 (log C + log n))
```

The extra space is `O(n)` for the map used by one anchor.

If we replace `std::map` with `std::unordered_map` and a custom hash for the pair, the average time becomes `O(n^2)`.

## Key Takeaway

The main idea is the invariant:

```text
Represent every line through an anchor using one canonical direction.
```

Canonical direction is enforced by sign normalization:

```text
right, or up if vertical
```
