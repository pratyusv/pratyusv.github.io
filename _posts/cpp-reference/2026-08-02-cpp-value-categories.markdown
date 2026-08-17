---
layout: single
comments: true
title: "C++ Value Categories: lvalues, rvalues, and std::move"
date: 2026-08-02 10:00:00-0000
last_modified_at: 2026-08-17 00:00:00-0000
description: A practical guide to C++ value categories, reference binding, overload selection, std::move, copy elision, and object-return semantics.
toc: true
toc_sticky: true
categories: C++
tags: [cpp, value-categories, lvalue, rvalue, references, move-semantics]
---

## Why Value Categories Matter

Every C++ expression has a type and a value category. The value category affects:

- which reference can bind to the expression,
- which overload is selected,
- whether an object may be moved from,
- when a temporary object is materialized,
- whether copy elision can construct a result directly.

The terms describe **expressions**, not permanent properties of objects. The same object can be reached through expressions with different value categories.

## The Five Categories

Modern C++ defines five related categories:

| Category | Has identity? | Resources may be reused? | Typical example |
|---|---:|---:|---|
| `lvalue` | Yes | No | A named variable |
| `xvalue` | Yes | Yes | `std::move(variable)` |
| `prvalue` | No identity before materialization | Yes | `Widget{}` or `2 + 3` |
| `glvalue` | Yes | Depends | An `lvalue` or `xvalue` |
| `rvalue` | Depends | Yes | A `prvalue` or `xvalue` |

The useful relationships are:

```text
glvalue = lvalue or xvalue
rvalue  = prvalue or xvalue
```

An `xvalue` belongs to both groups: it identifies an existing object, but that object's resources may be reused.

## Direct Examples

{% highlight c++ %}
#include <string>
#include <utility>

int value = 10;
int* pointer = &value;

value;                          // lvalue: names an object
*pointer;                       // lvalue: identifies value through a pointer
std::string{"temporary"};      // prvalue: creates a temporary string
value + 5;                      // prvalue: computes a value
std::move(value);               // xvalue: identifies value as movable
{% endhighlight %}

An lvalue is not necessarily modifiable:

{% highlight c++ %}
const int limit = 10;

// limit is an lvalue expression, but it is const.
// limit = 20;  // error
{% endhighlight %}

This is why “lvalue means something assignable on the left” is not a reliable definition.

## Reference Binding Rules

References expose the practical difference between the categories.

{% highlight c++ %}
#include <string>

std::string name = "vehicle";

std::string& mutable_alias = name;          // non-const lvalue reference
const std::string& read_only_alias = name;  // const lvalue reference
const std::string& temporary_alias =
    std::string{"temporary"};               // also binds to a temporary
std::string&& expiring_alias =
    std::string{"temporary"};               // rvalue reference
{% endhighlight %}

| Reference type | What it normally binds to | Typical purpose |
|---|---|---|
| `T&` | Non-const lvalue | Mutable borrowing |
| `const T&` | Lvalue or rvalue | Read-only borrowing without copying |
| `T&&` | Rvalue | Moving or forwarding |

These rules are simplified for non-template code. A `T&&` inside a deduced function template may be a forwarding reference, which follows additional reference-collapsing rules.

## A Named Rvalue Reference Is an lvalue

The type of a variable and the value category of an expression are different questions.

{% highlight c++ %}
#include <string>
#include <utility>

void receive(const std::string& value);  // lvalue-friendly overload
void receive(std::string&& value);       // rvalue overload

void forwardToReceiver(std::string&& input) {
  receive(input);             // input is a named expression: lvalue
  receive(std::move(input));  // std::move(input) is an xvalue
}
{% endhighlight %}

Although `input` has type `std::string&&`, writing its name produces an lvalue expression. This prevents accidental repeated moves from named variables.

## What `std::move` Does

`std::move` does not transfer memory or resources. It casts its argument to an xvalue so that move-aware overloads may be selected.

Conceptually:

{% highlight c++ %}
#include <type_traits>

template <typename T>
std::remove_reference_t<T>&& move(T&& value) noexcept {
  return static_cast<std::remove_reference_t<T>&&>(value);
}
{% endhighlight %}

The receiving constructor or assignment operator performs the actual move:

{% highlight c++ %}
#include <string>
#include <utility>

std::string source = "telemetry";
std::string destination = std::move(source);
{% endhighlight %}

Afterward, `source` remains alive and valid, but its value is generally unspecified. It can be destroyed or assigned a new value.

## Overload Selection Example

{% highlight c++ %}
#include <iostream>
#include <string>
#include <utility>

void inspect(const std::string&) {
  std::cout << "const lvalue reference\n";
}

void inspect(std::string&&) {
  std::cout << "rvalue reference\n";
}

int main() {
  std::string value = "data";

  inspect(value);             // const lvalue reference
  inspect(std::string{"x"}); // rvalue reference
  inspect(std::move(value));  // rvalue reference
}
{% endhighlight %}

The overload set makes the value category observable. `std::move` changes overload resolution; it does not guarantee that the called code will move anything.

## Pointers, `&`, and `&&`

The symbols depend on context:

{% highlight c++ %}
int value = 10;

int* pointer = &value;  // int*: pointer type; &value: address-of
int& alias = value;     // &: lvalue reference declarator
int&& temporary = 20;  // &&: rvalue reference declarator

*pointer = 30;          // *pointer: dereference; produces an lvalue
{% endhighlight %}

- A pointer stores an address and may be null or reseated.
- A reference is an alias and must be initialized when created.
- An rvalue reference is still a reference; its main role is binding to rvalue expressions.
- Outside a declaration, `&&` may instead mean logical AND.

## Returning Objects: Copy Elision and Move

Returning by value does not imply that a copy or move must occur.

{% highlight c++ %}
class Report {};

Report createDirectly() {
  return Report{};  // guaranteed copy elision since C++17
}

Report createNamed() {
  Report report;
  return report;    // NRVO is permitted; move is the fallback
}
{% endhighlight %}

In `createDirectly`, the result object is constructed directly in the caller's destination. No temporary `Report` needs to be copied or moved.

For a named local, compilers normally apply named return value optimization. If they do not, the return statement can use move construction when available. Writing `return std::move(report);` can prevent NRVO and should usually be avoided.

## Reasoning Checklist

When reasoning about an expression, ask:

1. **Does this expression identify an existing object?** If yes, it is a glvalue.
2. **May its resources be reused?** If yes, it is an rvalue.
3. **Is it a named variable expression?** Named variables are lvalues, even when their type is `T&&`.
4. **Did `std::move` appear?** It creates an xvalue; the receiver decides whether a move occurs.
5. **Is a function returning a fresh prvalue?** C++17 may construct the result directly.

## Related Guides

- [RAII and Deterministic Resource Management]({% post_url cpp-reference/2026-08-02-raii-resource-management %})
- [C++ Value Semantics: Rule of Zero, Copy, and Move]({% post_url cpp-reference/2020-05-29-move %})

## Further Reading

- [Microsoft Learn: Lvalues and Rvalues](https://learn.microsoft.com/en-us/cpp/cpp/lvalues-and-rvalues-visual-cpp?view=msvc-170)
