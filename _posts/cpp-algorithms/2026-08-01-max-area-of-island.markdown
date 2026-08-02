---
layout: single
comments: true
title: Max Area of Island
date: 2026-08-01 11:00:00-0000
categories: C++
tags: [cpp, algorithms, graph, dfs, grid, interview]
---

## Problem

Given a rectangular binary grid, return the largest area of any island.

A value of `1` represents land and `0` represents water. An island consists of land cells connected horizontally or vertically. Its area is the number of cells in that connected component.

Return `0` when the grid contains no land.

Example:

```text
grid = [
  [0, 1, 1, 0],
  [0, 1, 0, 0],
  [1, 0, 1, 1]
]

answer = 3
```

The upper island contains three cells, the lower-left island contains one, and the lower-right island contains two.

## DFS Area Calculation

The outer scan finds the start of each island. A depth-first traversal then consumes that island and returns its area.

For one land cell:

```text
area = 1 + area(up) + area(down) + area(left) + area(right)
```

The `1` counts the current cell. Omitting it causes every traversal to return zero.

As with Number of Islands, the grid can serve as visited state by changing each visited `1` to `0`.

## C++ Solution

{% highlight c++ %}
#include <algorithm>
#include <vector>

class Solution {
 public:
  int dfs(std::vector<std::vector<int>>& grid,
          int r,
          int c,
          int rows,
          int cols) {
    if (r < 0 || c < 0 || r >= rows || c >= cols ||
        grid[r][c] == 0) {
      return 0;
    }

    grid[r][c] = 0;

    return 1 +
           dfs(grid, r - 1, c, rows, cols) +
           dfs(grid, r + 1, c, rows, cols) +
           dfs(grid, r, c - 1, rows, cols) +
           dfs(grid, r, c + 1, rows, cols);
  }

  int maxAreaOfIsland(std::vector<std::vector<int>>& grid) {
    if (grid.empty() || grid[0].empty()) {
      return 0;
    }

    const int rows = static_cast<int>(grid.size());
    const int cols = static_cast<int>(grid[0].size());
    int max_area = 0;

    for (int r = 0; r < rows; ++r) {
      for (int c = 0; c < cols; ++c) {
        if (grid[r][c] == 1) {
          max_area = std::max(
              max_area, dfs(grid, r, c, rows, cols));
        }
      }
    }

    return max_area;
  }
};
{% endhighlight %}

## Why It Is Correct

`dfs` returns zero for water or an invalid coordinate. For a land cell, it counts the current cell, marks it visited, and adds the areas reachable through all four valid directions. The result is therefore exactly the number of cells in that island.

The outer scan invokes the traversal once for each previously unvisited island. Taking the maximum of those returned areas produces the largest island area. If no traversal runs, the answer remains zero.

## Complexity

Each cell is visited at most once.

- Time: `O(rows * columns)`
- Auxiliary space: `O(rows * columns)` in the worst case for recursion

The recursive form is concise and suitable when grid dimensions are bounded. For very large grids, use an explicit stack or queue to avoid exhausting the call stack.

## Edge Cases

- Empty grid: return `0`.
- All water: return `0`.
- All land: return the total number of cells.
- Long, narrow island: recursion depth may approach the number of cells.
- Diagonal cells: do not combine them into one island.
