---
layout: single
comments: true
title: Number of Islands
date: 2026-08-01 12:00:00-0000
categories: C++
tags: [cpp, algorithms, graph, dfs, grid, interview]
---

## Problem

Given a rectangular grid containing `'1'` for land and `'0'` for water, return the number of islands.

An island is a maximal group of land cells connected horizontally or vertically. Diagonal cells are not connected. Water surrounds the outer boundary of the grid.

Example:

```text
grid = [
  ['1', '1', '0', '0'],
  ['1', '0', '0', '1'],
  ['0', '0', '1', '1']
]

answer = 2
```

The land in the upper-left forms one island. The three connected cells on the right form the second.

## Connected-Component View

Treat every land cell as a vertex in a graph. Two vertices share an edge when their cells are adjacent in one of four directions.

The question then becomes:

```text
How many connected components does the land graph contain?
```

Scan every cell. When an unvisited land cell is found:

1. Increment the island count.
2. Run DFS from that cell.
3. Mark every reachable land cell as visited.
4. Continue scanning for the next unvisited component.

## Reusing the Grid as Visited State

A separate `visited` matrix is unnecessary when mutating the input is allowed. Change a land cell from `'1'` to `'0'` before visiting its neighbours. If DFS reaches that cell again, it immediately returns.

## C++ Solution

{% highlight c++ %}
#include <vector>

class Solution {
 public:
  void dfs(std::vector<std::vector<char>>& grid,
           int r,
           int c,
           int rows,
           int cols) {
    if (r < 0 || c < 0 || r >= rows || c >= cols ||
        grid[r][c] == '0') {
      return;
    }

    grid[r][c] = '0';

    dfs(grid, r - 1, c, rows, cols);
    dfs(grid, r + 1, c, rows, cols);
    dfs(grid, r, c - 1, rows, cols);
    dfs(grid, r, c + 1, rows, cols);
  }

  int numIslands(std::vector<std::vector<char>>& grid) {
    if (grid.empty() || grid[0].empty()) {
      return 0;
    }

    const int rows = static_cast<int>(grid.size());
    const int cols = static_cast<int>(grid[0].size());
    int count = 0;

    for (int r = 0; r < rows; ++r) {
      for (int c = 0; c < cols; ++c) {
        if (grid[r][c] == '1') {
          dfs(grid, r, c, rows, cols);
          ++count;
        }
      }
    }

    return count;
  }
};
{% endhighlight %}

## Why It Is Correct

Every DFS starts from land that was not reached by an earlier traversal, so it represents a new island.

The DFS follows every horizontal and vertical land connection from its starting cell. It therefore visits the entire island and no cell outside that island. Marking visited cells as water prevents any cell in that component from starting another traversal.

The outer scan eventually examines every grid cell, so every island is counted exactly once.

## Complexity

Each cell is visited at most once.

- Time: `O(rows * columns)`
- Auxiliary space: `O(rows * columns)` in the worst case for recursion

The input grid is modified. If the caller requires the original grid, use a separate visited matrix or pass a copy. For very large grids, replace recursive DFS with an explicit stack or queue to avoid call-stack exhaustion.

## Edge Cases

- Empty grid: return `0`.
- All water: return `0`.
- All land: return `1`.
- A single land cell: return `1`.
- Diagonally adjacent land cells: count them as separate islands.
