---
layout: single
comments: true
title: C++ Vector and String
date: 2026-07-19 10:00:00-0000
categories: C++
tags: [cpp, stl, containers, vector, string]
---

## Overview

`std::vector` is the default sequence container for most C++ interview code. `std::string` has similar indexing and contiguous-storage behavior, with string-specific helpers for search, slicing, and conversion.

---

## Common Includes

{% highlight c++ %}
#include <algorithm>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>
{% endhighlight %}

---

## Vector Initialization

{% highlight c++ %}
std::vector<int> nums;                    // empty
std::vector<int> fixed_size(5);           // [0, 0, 0, 0, 0]
std::vector<int> filled(5, -1);           // [-1, -1, -1, -1, -1]
std::vector<int> values = {10, 20, 30};   // exact values

int rows = 3;
int cols = 4;
std::vector<std::vector<int>> grid(rows, std::vector<int>(cols, 0));
{% endhighlight %}

Be careful with parentheses versus braces:

{% highlight c++ %}
std::vector<int> a(10, 2);                // ten elements, all 2
std::vector<int> b{10, 2};                // two elements: 10 and 2
{% endhighlight %}

Construct from a range:

{% highlight c++ %}
int raw[] = {1, 2, 3};
std::vector<int> nums(std::begin(raw), std::end(raw));
{% endhighlight %}

---

## Basic Vector Operations

{% highlight c++ %}
std::vector<int> nums = {1, 2, 3};

nums.push_back(4);
nums.emplace_back(5);
nums.pop_back();

int first = nums[0];                      // no bounds check
int safe = nums.at(0);                    // throws if invalid

int n = static_cast<int>(nums.size());
bool empty = nums.empty();
{% endhighlight %}

Insert and erase:

{% highlight c++ %}
std::vector<int> nums = {1, 2, 4};

nums.insert(nums.begin() + 2, 3);         // [1, 2, 3, 4]
nums.erase(nums.begin() + 1);             // [1, 3, 4]
nums.erase(nums.begin() + 1, nums.end()); // erase range [1, end)
nums.clear();
{% endhighlight %}

Remove by value:

{% highlight c++ %}
std::vector<int> nums = {1, 2, 3, 2};

auto it = std::find(nums.begin(), nums.end(), 2);
if (it != nums.end()) {
  nums.erase(it);                         // removes first 2
}

nums.erase(std::remove(nums.begin(), nums.end(), 2), nums.end());
{% endhighlight %}

---

## Size, Capacity, and Reallocation

`size()` is the number of live elements. `capacity()` is the number of elements the current allocation can hold before the vector has to allocate again.

{% highlight c++ %}
std::vector<int> nums;

nums.reserve(1000);                       // capacity changes, size stays 0

for (int i = 0; i < 1000; ++i) {
  nums.push_back(i);                      // no reallocation within reserved capacity
}

nums.resize(10);                          // size becomes 10; new ints are 0
{% endhighlight %}

When a vector grows beyond capacity:

1. It allocates a larger contiguous heap buffer.
2. It moves or copies existing elements into the new buffer.
3. It constructs the new element.
4. It destroys the old elements and releases the old buffer.

Pointers, references, and iterators into the old buffer are invalid after reallocation.

{% highlight c++ %}
std::vector<int> nums = {10, 20, 30};
int* ptr = &nums[0];

nums.push_back(40);                       // may reallocate

// ptr may now be dangling
{% endhighlight %}

<div>
    <center>{% include figure.html path="assets/img/containers/vector.png" %}</center>
</div>

---

## push_back vs emplace_back

`push_back` appends an existing object or temporary. `emplace_back` receives constructor arguments and builds the object directly in vector storage.

{% highlight c++ %}
struct Point {
  int x;
  int y;

  Point(int x_value, int y_value) : x(x_value), y(y_value) {}
};

std::vector<Point> points;

points.push_back(Point(1, 2));            // temporary, then move/copy
points.emplace_back(3, 4);                // construct in place
{% endhighlight %}

For simple types such as `int`, `double`, and small strings, the practical difference is usually small. `emplace_back` matters more for non-trivial objects constructed from arguments.

---

## Iteration

{% highlight c++ %}
for (int x : nums) {
  // read x
}

for (int& x : nums) {
  x *= 2;
}

for (const auto& x : nums) {
  // read without copying
}

for (int i = 0; i < static_cast<int>(nums.size()); ++i) {
  nums[i] += 1;
}
{% endhighlight %}

---

## String Basics

`std::string` behaves like a character vector with string-specific helpers.

{% highlight c++ %}
std::string s = "hello";

s.push_back('!');
s.pop_back();
s += " world";

char first = s[0];
int n = static_cast<int>(s.size());
bool empty = s.empty();
{% endhighlight %}

Substring, find, erase, and insert:

{% highlight c++ %}
std::string s = "abcdef";

std::string part = s.substr(1, 3);        // "bcd"

size_t pos = s.find("cd");
if (pos != std::string::npos) {
  s.erase(pos, 2);                        // remove "cd"
}

s.insert(2, "XX");
{% endhighlight %}

Conversions:

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

Frequency array:

{% highlight c++ %}
std::vector<int> freq(26, 0);

for (char ch : s) {
  ++freq[ch - 'a'];
}
{% endhighlight %}

---

## Complexity

| Operation | `vector` | `string` |
|---|---:|---:|
| Index access | `O(1)` | `O(1)` |
| Push at end | `O(1)` amortized | `O(1)` amortized |
| Insert/delete middle | `O(n)` | `O(n)` |
| Search unsorted content | `O(n)` | `O(n)` |

---

## Checklist

- Use `vector` as the default container for indexed sequential data.
- Use `reserve()` when the final number of appended elements is known.
- Use `resize()` when the vector should actually contain that many elements.
- Treat vector reallocation as invalidating existing pointers, references, and iterators.
- Use `const auto&` in loops when elements are expensive to copy.
