---
layout: single
comments: true
title: "C++ Template Programming: From Generic Functions to Library Design"
date: 2026-08-02 15:00:00-0000
description: "A practical guide to modern C++ template programming, from function and class templates to concepts, specialization, variadic templates, instantiation, and large-scale library design."
categories: C++
tags: [cpp, templates, generic-programming, concepts, metaprogramming, compile-time, library-design]
---

# 1. Start With One Function

Suppose an application needs to return the larger of two integers:

{% highlight c++ %}
int larger(int left, int right) {
  return left < right ? right : left;
}
{% endhighlight %}

The algorithm does not depend on `int`. It needs a type that can be compared and returned. Repeating the function for `double`, `long`, and every domain type would duplicate the same idea.

A function template describes a family of functions:

{% highlight c++ %}
template <typename T>
T larger(const T& left, const T& right) {
  return left < right ? right : left;
}

int count = larger(4, 9);             // T is int
double rate = larger(2.5, 1.8);       // T is double
{% endhighlight %}

`T` is a template parameter: a name that stands for a type. At each call, the compiler deduces a template argument and forms a concrete specialization such as `larger<int>` or `larger<double>`.

The useful mental model is:

```text
template definition + template arguments -> concrete specialization
```

A template is not a runtime object and does not perform runtime dispatch. It is a compile-time recipe for declarations and definitions. The generated specialization is an ordinary C++ function, class, or variable.

That model explains both the strength and the cost of templates:

- One source definition can work with many types.
- Type checking happens for each specialization.
- Compile-time information can remove runtime abstraction cost.
- Each specialization can contribute compilation work and machine code.
- Errors may appear far from the template definition, when a particular use is instantiated.

This guide uses C++20 as its baseline. Features introduced in earlier standards are identified where the history helps explain modern code.

## Generic Programming and Metaprogramming

The terms are related but not identical.

**Generic programming** expresses an algorithm or data structure in terms of the operations it needs rather than one concrete type. `std::sort`, `std::vector<T>`, and the `larger` function are examples.

**Template metaprogramming** uses template instantiation to compute or transform information during compilation. Building a new type with `std::remove_reference_t<T>` is a small example. Older C++ libraries used recursive templates for substantial compile-time computation.

Modern C++ also provides `constexpr`, `consteval`, `if constexpr`, and concepts. As a result, compile-time programming no longer needs to mean deeply recursive type tricks. A principal-level design normally uses the simplest mechanism that expresses the requirement:

| Requirement | Prefer |
|---|---|
| Same algorithm for several types | Function or class template |
| State a valid template interface | Concept and `requires` |
| Select one compile-time branch | `if constexpr` |
| Compute a value at compile time | `constexpr` function |
| Compute or transform a type | Type trait or alias template |
| Reject runtime evaluation | `consteval` function |

# 2. Function Templates and Deduction

## Template Parameters and Arguments

In this declaration, `T` is a template parameter:

{% highlight c++ %}
template <typename T>
T identity(T value) {
  return value;
}
{% endhighlight %}

In `identity<int>(7)`, `int` is the template argument. The compiler substitutes `int` for `T` and forms `identity<int>`.

For a type parameter, `typename` and `class` mean the same thing:

{% highlight c++ %}
template <typename T>
void first(T value);

template <class T>
void second(T value);
{% endhighlight %}

`typename` often reads more directly because the parameter need not be a class.

## Let the Compiler Deduce Arguments

Function template arguments are usually deduced from call arguments:

{% highlight c++ %}
template <typename T>
T square(T value) {
  return value * value;
}

auto a = square(6);       // square<int>
auto b = square(2.5);     // square<double>
{% endhighlight %}

Arguments may also be written explicitly:

{% highlight c++ %}
auto value = square<double>(3);  // converts 3 to double
{% endhighlight %}

Prefer deduction when it expresses the intended type. Explicit arguments are useful when a parameter cannot be deduced or when a deliberate conversion is part of the call.

## One Parameter Means One Deduced Type

The first version of `larger` requires both arguments to produce the same `T`:

{% highlight c++ %}
larger(3, 4);      // T = int
larger(3.0, 4.0);  // T = double
// larger(3, 4.0); // error: T cannot be both int and double
{% endhighlight %}

Template deduction does not generally search for a common conversion that makes deduction succeed. If mixed arguments are meaningful, model that explicitly:

{% highlight c++ %}
#include <type_traits>

template <typename Left, typename Right>
auto larger(Left left, Right right)
    -> std::common_type_t<Left, Right> {
  using Result = std::common_type_t<Left, Right>;
  return left < right ? static_cast<Result>(right)
                      : static_cast<Result>(left);
}

auto value = larger(3, 4.5);  // Result is double
{% endhighlight %}

This is a semantic decision, not merely a syntax fix. The interface now promises that mixed types are supported and that `std::common_type` describes the result.

## How Parameter Form Changes Deduction

These common parameter forms behave differently:

{% highlight c++ %}
template <typename T>
void by_value(T value);

template <typename T>
void by_const_reference(const T& value);

template <typename T>
void by_reference(T& value);
{% endhighlight %}

For a by-value parameter, top-level `const` and references are not part of the deduced `T`. Arrays and functions normally decay to pointers. Reference parameters preserve more information.

{% highlight c++ %}
const int count = 4;
int values[3] = {1, 2, 3};

by_value(count);             // T is int
by_const_reference(count);   // T is int; parameter is const int&
by_reference(count);         // T is const int
by_value(values);            // T is int*
by_reference(values);        // T is int[3]
{% endhighlight %}

Choose the parameter form from the function's ownership and mutation semantics first. Its deduction behavior should follow that design.

## Overload Resolution Still Applies

Templates participate in the normal overload set:

{% highlight c++ %}
#include <string>

template <typename T>
std::string describe(const T&) {
  return "generic";
}

std::string describe(int) {
  return "integer";
}

auto first = describe(7);      // non-template overload
auto second = describe(2.5);   // template with T = double
{% endhighlight %}

The compiler first determines viable candidates, then ranks them. A non-template function is not always preferred; it wins only when the conversion sequences and overload rules make it the better candidate. Template API design is therefore overload-set design. Adding one overload or conversion can change existing calls.

# 3. Class, Alias, Variable, and Value Templates

## Class Templates

A class template describes a family of types:

{% highlight c++ %}
#include <cstddef>
#include <utility>

template <typename T>
class Box {
 public:
  explicit Box(T value) : value_(std::move(value)) {}

  const T& get() const noexcept {
    return value_;
  }

 private:
  T value_;
};

Box<int> count_box(7);
Box<double> rate_box(2.5);
{% endhighlight %}

`Box<int>` and `Box<double>` are distinct types. They have the same template origin, but neither implicitly converts to the other unless the class defines such a conversion.

A member defined outside the class repeats the template parameter list and names the specialization pattern:

{% highlight c++ %}
template <typename T>
class Holder {
 public:
  explicit Holder(T value);

 private:
  T value_;
};

template <typename T>
Holder<T>::Holder(T value) : value_(std::move(value)) {}
{% endhighlight %}

## Class Template Argument Deduction

Since C++17, constructor arguments can sometimes determine class template arguments:

{% highlight c++ %}
Box count_box(7);       // Box<int>
Box rate_box(2.5);      // Box<double>
{% endhighlight %}

This is **class template argument deduction**, usually abbreviated CTAD. The compiler forms deduction candidates from constructors and any user-defined deduction guides.

Use a deduction guide when the public deduction rule differs from the raw constructor parameter types:

{% highlight c++ %}
#include <string>

template <typename T>
class NamedValue {
 public:
  NamedValue(std::string name, T value)
      : name_(std::move(name)), value_(std::move(value)) {}

 private:
  std::string name_;
  T value_;
};

NamedValue(const char*, const char*) -> NamedValue<std::string>;

NamedValue item("region", "eu-west");  // NamedValue<std::string>
{% endhighlight %}

Deduction guides affect construction syntax, not conversions between already formed class specializations. Keep them unsurprising; an opaque deduction guide makes the declared type difficult to infer during review.

## Non-Type Template Parameters

Templates can take compile-time values as arguments:

{% highlight c++ %}
#include <array>
#include <cstddef>

template <typename T, std::size_t Capacity>
class FixedBuffer {
 public:
  constexpr std::size_t capacity() const noexcept {
    return Capacity;
  }

 private:
  std::array<T, Capacity> values_{};
};

FixedBuffer<int, 16> small;
FixedBuffer<int, 1024> large;
{% endhighlight %}

`Capacity` is a non-type template parameter. Its value is part of the type, so `FixedBuffer<int, 16>` and `FixedBuffer<int, 1024>` are unrelated types. This enables compile-time layout and optimization, but it also means capacity changes propagate into type identity, overloads, symbols, and ABI.

Use a runtime constructor argument when callers need one stable type with a runtime-selected value. Use a non-type template parameter when the value must influence type behavior, layout, validation, or compile-time computation.

## Alias Templates

An alias template gives a readable name to a family of types:

{% highlight c++ %}
#include <string>
#include <unordered_map>

template <typename Value>
using StringMap = std::unordered_map<std::string, Value>;

StringMap<int> counters;
{% endhighlight %}

An alias does not create a new type. `StringMap<int>` is exactly the corresponding `std::unordered_map` specialization.

Alias templates are especially useful for exposing the result of a type transformation:

{% highlight c++ %}
#include <type_traits>

template <typename T>
using RemoveCvRef = std::remove_cv_t<std::remove_reference_t<T>>;
{% endhighlight %}

C++20 provides this particular transformation as `std::remove_cvref_t<T>`.

## Variable Templates

A variable template describes a family of variables:

{% highlight c++ %}
template <typename T>
inline constexpr bool is_byte_sized = sizeof(T) == 1;

static_assert(is_byte_sized<char>);
{% endhighlight %}

The standard library's `_v` traits, such as `std::is_integral_v<T>`, use this style. `inline` permits one logical variable definition to appear through a header in multiple translation units.

# 4. Instantiation and the Compilation Model

## A Template Is Checked in Stages

Compilers can diagnose syntax and non-dependent errors when a template is defined. Expressions that depend on a template parameter may not be valid or invalid until a specialization is formed.

{% highlight c++ %}
template <typename T>
auto area(const T& value) {
  return value.width() * value.height();
}
{% endhighlight %}

The parser can check the structure immediately. It cannot determine whether `width()` and `height()` exist until it knows `T`.

{% highlight c++ %}
struct Rectangle {
  int width() const { return 4; }
  int height() const { return 3; }
};

auto valid = area(Rectangle{});  // forms a valid specialization
// auto invalid = area(7);       // area<int> is ill-formed
{% endhighlight %}

This delayed checking is why unconstrained template errors can surface inside an implementation rather than at the call boundary.

## Definitions Usually Live in Headers

A compiler normally needs the template definition and template arguments together to instantiate a specialization. A declaration in a header and an unconstrained definition hidden in a `.cpp` file usually fail at link time for caller-selected types.

This header is insufficient by itself:

{% highlight c++ %}
// convert.h
template <typename T>
std::string convert(const T& value);  // declaration only
{% endhighlight %}

If another translation unit calls `convert(42)`, it cannot form `convert<int>` without the definition.

Common solutions are:

- define the template in the header,
- place the definition in a file included by the header,
- explicitly instantiate a supported, closed set of specializations,
- hide the template behind a non-template interface.

This is a build architecture decision. Header-defined templates improve openness and inlining opportunities but increase dependency exposure and repeated front-end work.

## Explicit Instantiation

If a library supports a known set of types, it can centralize instantiation.

{% highlight c++ %}
// counter.h
template <typename T>
class Counter {
 public:
  void add(T value);
  T total() const;

 private:
  T total_{};
};

extern template class Counter<int>;
extern template class Counter<double>;
{% endhighlight %}

{% highlight c++ %}
// counter.cpp
#include "counter.h"

template <typename T>
void Counter<T>::add(T value) {
  total_ += value;
}

template <typename T>
T Counter<T>::total() const {
  return total_;
}

template class Counter<int>;
template class Counter<double>;
{% endhighlight %}

The `extern template` declarations tell other translation units not to implicitly instantiate those specializations. The explicit instantiation definitions in `counter.cpp` emit them once.

The tradeoff is deliberate closure: a caller cannot use `Counter<long>` unless its definition is visible or the library provides that specialization. Explicit instantiation is useful for build performance and ABI control when the supported type set is stable.

## The One Definition Rule

Templates do not bypass the one definition rule. Header definitions must be equivalent across translation units. Conditional compilation, configuration macros, or inconsistent generated headers can make the same specialization mean different things in different translation units. Such violations are often difficult to diagnose.

Keep semantic configuration out of template definitions where possible. If behavior must differ by configuration, make that difference an explicit policy, value, or build boundary.

## Dependent Names: `typename` and `template`

When a qualified name depends on a template parameter, the compiler may need help parsing it.

{% highlight c++ %}
template <typename Container>
void print_first(const Container& values) {
  typename Container::const_iterator position = values.begin();
  //        ^ tells the parser this dependent name is a type
}
{% endhighlight %}

Without `typename`, `Container::const_iterator` is not assumed to name a type.

The `template` disambiguator performs a similar job for a dependent member template:

{% highlight c++ %}
template <typename Factory>
auto make_int(Factory& factory) {
  return factory.template create<int>();
  //             ^ create is a dependent template name
}
{% endhighlight %}

These keywords do not change behavior. They make the intended parse explicit before the dependent type is known.

# 5. Specialization and Compile-Time Selection

## Prefer One General Definition

A primary template describes the general case:

{% highlight c++ %}
template <typename T>
struct TypeName {
  static constexpr const char* value = "unknown";
};
{% endhighlight %}

An explicit specialization replaces it for one exact argument list:

{% highlight c++ %}
template <>
struct TypeName<int> {
  static constexpr const char* value = "int";
};
{% endhighlight %}

Specialization is powerful because it can replace an implementation completely. That also makes it a sharp extension mechanism: the specialized version can silently diverge in interface or semantics.

Prefer ordinary implementation techniques, overloads, or constrained branches when the behavior is still conceptually one algorithm. Specialize when a family genuinely has a structurally distinct case.

## Partial Specialization

Class and variable templates can be partially specialized for a pattern of arguments:

{% highlight c++ %}
template <typename T>
struct IsPointer {
  static constexpr bool value = false;
};

template <typename T>
struct IsPointer<T*> {
  static constexpr bool value = true;
};

static_assert(!IsPointer<int>::value);
static_assert(IsPointer<int*>::value);
{% endhighlight %}

The partial specialization matches any pointer type and still deduces the pointed-to `T`.

Function templates cannot be partially specialized. Use overloads or dispatch through a class template instead:

{% highlight c++ %}
template <typename T>
void inspect(const T&) {
  // general overload
}

template <typename T>
void inspect(T*) {
  // pointer overload
}
{% endhighlight %}

## `if constexpr`

Since C++17, `if constexpr` can select a branch during instantiation:

{% highlight c++ %}
#include <string>
#include <type_traits>

template <typename T>
std::string format(const T& value) {
  if constexpr (std::is_same_v<T, bool>) {
    return value ? "true" : "false";
  } else {
    return std::to_string(value);
  }
}
{% endhighlight %}

For `format<bool>`, the non-selected branch is discarded for that specialization. This is different from an ordinary `if`: both branches of a normal `if` must be valid after instantiation.

Use `if constexpr` when one operation has a small number of compile-time implementation paths. Use overloads or separate types when the paths represent different public operations or substantially different algorithms.

## Traits as Compile-Time Data

A type trait maps types to a value or another type. The standard library represents Boolean results with types derived from `std::integral_constant`:

{% highlight c++ %}
#include <type_traits>

static_assert(std::is_integral_v<int>);
static_assert(!std::is_integral_v<double>);

using Value = std::remove_reference_t<int&>;
static_assert(std::is_same_v<Value, int>);
{% endhighlight %}

The common standard-library naming convention is:

| Form | Meaning |
|---|---|
| `trait<T>::value` | Value exposed by a trait type |
| `trait_v<T>` | C++17 variable-template shorthand |
| `trait<T>::type` | Type exposed by a transformation |
| `trait_t<T>` | Alias-template shorthand |

Do not create a custom trait when a standard concept or type trait already captures the rule. Shared vocabulary improves interoperability and reduces subtle differences in meaning.

# 6. Variadic Templates

## Parameter Packs

A variadic template accepts zero or more template arguments:

{% highlight c++ %}
template <typename... Types>
struct TypeList {};

TypeList<> empty;
TypeList<int, double, char> three_types;
{% endhighlight %}

`Types` is a template parameter pack. `Types...` expands the pack where the grammar expects a sequence.

A function parameter pack can forward a variable number of values:

{% highlight c++ %}
#include <iostream>

template <typename... Values>
void print_all(const Values&... values) {
  (std::cout << ... << values) << '\n';
}

print_all("status=", 200, ", latency=", 12.5);
{% endhighlight %}

The parenthesized expression is a **fold expression**, introduced in C++17. It applies an operator across a pack.

## Fold Direction and Identity

For a pack `values`, these are different shapes:

{% highlight c++ %}
(values + ...);       // unary right fold
(... + values);       // unary left fold
(0 + ... + values);   // binary left fold with initial value 0
{% endhighlight %}

For associative arithmetic the result may look equivalent, but operator order matters for strings, streams, stateful operators, and non-associative operations.

An initial value also defines behavior for an empty pack:

{% highlight c++ %}
template <typename... Numbers>
auto sum(Numbers... numbers) {
  return (0 + ... + numbers);
}

static_assert(sum() == 0);
static_assert(sum(1, 2, 3) == 6);
{% endhighlight %}

Choose the identity deliberately. `0` may force an unwanted result type for domain-specific numbers, so a production API may require at least one value or accept an explicit initial value.

## Perfect Forwarding

Generic factories often need to preserve whether each argument is an lvalue or rvalue:

{% highlight c++ %}
#include <memory>
#include <utility>

template <typename T, typename... Args>
std::unique_ptr<T> make_unique_like(Args&&... args) {
  return std::unique_ptr<T>(
      new T(std::forward<Args>(args)...));
}
{% endhighlight %}

Here `Args&&...` forms forwarding references because each `Args` is deduced. Reference collapsing and `std::forward` preserve the caller's value categories.

For one argument:

```text
lvalue argument  -> Args deduces as U&  -> forward as lvalue
rvalue argument  -> Args deduces as U   -> forward as rvalue
```

Use forwarding references only when the function genuinely forwards arguments. They accept a very broad set of calls, can interfere with overloads, and can make diagnostics harder. If an API only reads a `Widget`, write `const Widget&`; if it takes ownership, consider `Widget` by value. Do not use `T&&` merely because it appears generic.

For the value-category rules behind forwarding, see [C++ Value Categories: lvalues, rvalues, and std::move]({% post_url cpp-reference/2026-08-02-cpp-value-categories %}).

# 7. From SFINAE to Concepts

## The Interface Problem

This template appears to accept every `T`, but its body has a hidden requirement:

{% highlight c++ %}
template <typename T>
T add(const T& left, const T& right) {
  return left + right;
}
{% endhighlight %}

The actual interface is: `T` must support `left + right`, and that result must be usable as `T`. Without an explicit constraint, the compiler discovers the requirement only while instantiating the body.

Large generic libraries need requirements to be visible at the declaration. This improves overload selection, documentation, and diagnostics.

## SFINAE: Historical Foundation

SFINAE means **substitution failure is not an error**. While substituting deduced arguments into certain parts of a function template declaration, an invalid result can remove that candidate from the overload set instead of rejecting the entire program.

Before concepts, `std::enable_if` was a common way to use this rule:

{% highlight c++ %}
#include <type_traits>

template <typename T,
          std::enable_if_t<std::is_integral_v<T>, int> = 0>
T twice(T value) {
  return value * 2;
}
{% endhighlight %}

This remains relevant when maintaining C++11 through C++17 code. It is not the clearest interface in C++20.

SFINAE is also narrower than the slogan sometimes suggests. An arbitrary error in an instantiated function body is still an error. Only failures in the relevant substitution context remove a candidate.

## Define a Concept

A concept is a named compile-time predicate over template arguments:

{% highlight c++ %}
#include <concepts>

template <typename T>
concept Addable = requires(const T& left, const T& right) {
  { left + right } -> std::convertible_to<T>;
};

template <Addable T>
T add(const T& left, const T& right) {
  return left + right;
}
{% endhighlight %}

The `requires` expression checks whether the expression is valid and whether its result meets the stated type requirement. It does not execute `left + right`.

The same constraint can be written in several forms:

{% highlight c++ %}
template <Addable T>
T first(T value);

template <typename T>
requires Addable<T>
T second(T value);

template <typename T>
T third(T value) requires Addable<T>;
{% endhighlight %}

Use one consistent house style. The long forms are useful when constraints involve several parameters or read better after the function declarator.

## Four Kinds of Requirement

A `requires` expression can state several kinds of requirements:

{% highlight c++ %}
#include <concepts>
#include <cstddef>

template <typename T>
concept BufferLike = requires(T buffer, const T const_buffer,
                              std::size_t index) {
  typename T::value_type;                         // type requirement
  const_buffer.size();                            // simple requirement
  { const_buffer[index] } ->
      std::convertible_to<typename T::value_type>; // compound requirement
  requires std::default_initializable<T>;          // nested requirement
};
{% endhighlight %}

The distinction is less important than the interface it produces. A concept should describe a meaningful capability, not expose every expression used by the implementation.

For example, `SortableRange` communicates intent better than `HasBeginAndEndAndLessThanAndSwap`. Good concepts form domain vocabulary.

## Constraint-Based Overloads

Constraints can order overloads from general to more specific:

{% highlight c++ %}
#include <concepts>
#include <string>

template <typename T>
std::string category(const T&) {
  return "value";
}

template <std::integral T>
std::string category(const T&) {
  return "integral value";
}
{% endhighlight %}

For an `int`, the constrained overload is more specialized. This ordering is based on formal constraint subsumption, not on the compiler proving arbitrary Boolean logic.

Named concepts help the compiler and the reader see shared constraint structure:

{% highlight c++ %}
template <typename T>
concept Integer = std::integral<T>;

template <typename T>
concept SignedInteger = Integer<T> && std::signed_integral<T>;
{% endhighlight %}

Two constraint expressions that happen to be logically equivalent are not necessarily interchangeable for overload ordering. Reuse named concepts instead of spelling similar Boolean expressions repeatedly.

## Constrain the Contract, Test the Implementation

An under-constrained template fails inside its body. An over-constrained template rejects useful types that the implementation could support.

The concept should express the minimum semantic contract promised by the API. Tests should then include:

- representative built-in types,
- user-defined types that satisfy the contract,
- near-miss types that should be rejected,
- types with unusual conversions or proxy references,
- compile-fail tests for important diagnostics.

Concepts validate syntax and stated properties. They cannot prove semantic laws such as associativity, strict weak ordering, or thread safety. Those laws belong in documentation, naming, and tests.

# 8. Compile-Time Programming

## Prefer `constexpr` for Values

Classic template metaprogramming computed values through recursive types:

{% highlight c++ %}
template <int N>
struct Factorial {
  static constexpr int value = N * Factorial<N - 1>::value;
};

template <>
struct Factorial<0> {
  static constexpr int value = 1;
};

static_assert(Factorial<5>::value == 120);
{% endhighlight %}

Modern C++ can express the value computation directly:

{% highlight c++ %}
constexpr int factorial(int value) {
  int result = 1;
  for (int current = 2; current <= value; ++current) {
    result *= current;
  }
  return result;
}

static_assert(factorial(5) == 120);
int runtime_value = factorial(read_count());
{% endhighlight %}

A `constexpr` function may run during compilation when used in a constant-expression context and may also run at runtime. This dual use often makes it clearer than recursive template instantiation.

Use template metaprogramming when the result is a type, when the set of declarations changes, or when template selection itself is the mechanism. Use `constexpr` for ordinary value algorithms.

## `consteval` and `constinit`

A `consteval` function is an immediate function: every potentially evaluated call must produce a compile-time constant.

{% highlight c++ %}
consteval int checked_port(int port) {
  if (port < 1 || port > 65535) {
    throw "invalid port";
  }
  return port;
}

constexpr int port = checked_port(8080);
{% endhighlight %}

Use `consteval` when runtime evaluation would be meaningless or unsafe, such as validating a compile-time format or constructing compile-time metadata.

`constinit` solves a different problem. It requires static or thread-local initialization to be static, but it does not make the variable immutable.

## Type-Level Computation

Some computations must produce types. A simple conditional type illustrates the pattern:

{% highlight c++ %}
#include <type_traits>

template <bool UseWideType>
using CounterType =
    std::conditional_t<UseWideType, long long, int>;

CounterType<false> local_count = 0;  // int
CounterType<true> global_count = 0;  // long long
{% endhighlight %}

Real libraries compose traits to normalize types, inspect callability, choose representations, and adapt interfaces. Keep transformations named and shallow. Long anonymous chains of traits turn compiler output into the only available documentation.

## Compile-Time Work Is Not Free

Moving work from runtime to compile time changes who pays; it does not eliminate cost. Template-heavy code can increase:

- parsing and semantic analysis,
- number of specializations,
- optimizer input size,
- object-file and debug-information size,
- link time,
- memory use in developer and CI builds.

Use compile-time work when it improves correctness, interface quality, or measured runtime behavior. Avoid it when a small runtime table or ordinary function is simpler and sufficiently fast.

# 9. Reusable Template Design Patterns

## Policy-Based Design

A policy parameter makes one dimension of behavior explicit:

{% highlight c++ %}
#include <iostream>
#include <string_view>

struct QuietLog {
  static void write(std::string_view) {}
};

struct ConsoleLog {
  static void write(std::string_view message) {
    std::cout << message << '\n';
  }
};

template <typename LogPolicy = QuietLog>
class Service {
 public:
  void start() {
    LogPolicy::write("service started");
  }
};
{% endhighlight %}

The compiler can inline the policy call, and a quiet specialization carries no runtime strategy pointer. The cost is that every policy combination creates a different type and may create more code.

Use a template policy when the choice is known at compile time, the policy affects representation or optimization, and the number of combinations is controlled. Use runtime polymorphism or a callable value when behavior changes at runtime or when stable ABI and build isolation matter more.

## CRTP and Static Polymorphism

The curiously recurring template pattern passes a derived type to a base template:

{% highlight c++ %}
template <typename Derived>
class Printable {
 public:
  void print() const {
    static_cast<const Derived&>(*this).print_impl();
  }
};

class Invoice : public Printable<Invoice> {
 public:
  void print_impl() const {
    // Print the invoice.
  }
};
{% endhighlight %}

CRTP can provide static interfaces, mixin behavior, and compile-time customization without a virtual call. It does not provide ordinary runtime substitutability: `Printable<Invoice>` and `Printable<Receipt>` are different types.

Modern concepts can express many static interface requirements without inheritance. Use CRTP when shared implementation or derived-type access is genuinely required, not merely to imitate an object-oriented hierarchy.

## Tag Dispatch

Before `if constexpr`, libraries often selected implementations by passing a tag type:

{% highlight c++ %}
#include <iterator>

template <typename Iterator>
void move_forward(Iterator& iterator, int distance,
                  std::random_access_iterator_tag) {
  iterator += distance;
}

template <typename Iterator>
void move_forward(Iterator& iterator, int distance,
                  std::input_iterator_tag) {
  while (distance-- > 0) {
    ++iterator;
  }
}
{% endhighlight %}

The public function can obtain `iterator_category` and pass an instance of that tag. More specific iterator tags inherit from more general tags, so overload resolution selects the strongest supported implementation.

Tag dispatch remains useful when working with existing type taxonomies or when overload separation is clearer. For local two-way selection, a concept and `if constexpr` are usually easier to read.

## Customization Boundaries

A library sometimes needs user-defined behavior for external types. Possible mechanisms include:

- an explicit callable or policy parameter,
- a constrained member operation,
- a customization point object,
- overloads found through argument-dependent lookup,
- permitted specialization of a documented library template.

Prefer an explicit, narrow customization boundary. Unqualified-call protocols and specialization rules interact with lookup, namespace ownership, and overload resolution. They are suitable for infrastructure libraries but need precise documentation and compile-time tests.

Never invite users to specialize arbitrary implementation templates. State exactly which template may be specialized, for which user-defined types, and which members and semantic laws the specialization must provide.

# 10. Lookup, Overloads, and Subtle Failure Modes

## Two-Phase Lookup

Names in templates are broadly divided into non-dependent and dependent names. Non-dependent names are looked up around the template definition. Dependent names may be resolved later, using information available when the specialization is instantiated.

{% highlight c++ %}
void log(int);

template <typename T>
void process(const T& value) {
  log(0);      // non-dependent lookup
  handle(value); // dependent call; ADL may find an associated overload
}
{% endhighlight %}

Code that relies on accidental include order or permissive compiler behavior may fail when moved between toolchains. Qualify non-customizable calls. For intentional argument-dependent lookup, make the protocol explicit and test it on all supported compilers.

## Hidden Friends

A friend function defined inside a class template creates a non-template function for each specialization and is normally found through argument-dependent lookup:

{% highlight c++ %}
template <typename T>
class Point {
 public:
  Point(T x, T y) : x_(x), y_(y) {}

  friend bool operator==(const Point& left, const Point& right) {
    return left.x_ == right.x_ && left.y_ == right.y_;
  }

 private:
  T x_;
  T y_;
};
{% endhighlight %}

This keeps the operator associated with `Point<T>` and gives it access to private state. It also means ordinary qualified lookup behaves differently from lookup through an expression. Hidden friends are useful, but the lookup model should be intentional.

## Accidental Catch-All Overloads

A forwarding-reference constructor or function can accept more than expected:

{% highlight c++ %}
class Label {
 public:
  template <typename T>
  explicit Label(T&& value);
};
{% endhighlight %}

This constructor may compete with copy construction, accept nonsensical types, and fail deep in its implementation. Constrain it to the intended operation:

{% highlight c++ %}
#include <concepts>
#include <string>
#include <utility>

class Label {
 public:
  template <typename T>
  requires std::constructible_from<std::string, T>
  explicit Label(T&& value)
      : value_(std::forward<T>(value)) {}

 private:
  std::string value_;
};
{% endhighlight %}

Broad templates should earn their breadth through a clear contract.

## Specialization Is Not Overloading

Function template specializations do not participate in overload resolution as independent candidates. First the compiler selects a primary function template or non-template overload; then it uses the applicable specialization of the selected template.

This can make explicit function specializations surprising when overloads are added later. Prefer a normal overload for function behavior. Reserve explicit specialization for cases where its selection model is understood and required.

## Lifetime Is Still a Runtime Concern

Templates can preserve types perfectly and still produce dangling references:

{% highlight c++ %}
#include <vector>

template <typename Container>
const auto& first(const Container& values) {
  return values.front();
}

// const int& value = first(std::vector<int>{1, 2, 3});
// value dangles after the temporary vector is destroyed.
{% endhighlight %}

Generic code amplifies an interface across many types; it does not make an unsafe ownership model safe. Encode borrowing and ownership clearly, use range borrowing rules where appropriate, and test calls involving temporaries.

# 11. Performance, Code Size, and ABI

## Zero-Overhead Is an Opportunity, Not a Guarantee

Templates expose concrete types to the optimizer. That can enable inlining, constant propagation, devirtualization, vectorization, and removal of unused branches. It does not guarantee faster code.

A templated operation may be slower because it creates too much code for instruction caches, prevents build-wide optimization across boundaries, or chooses a representation that is efficient only for one workload.

Measure the generated behavior that matters:

- request latency and throughput,
- instruction count and branch behavior,
- code and object size,
- compiler time and peak memory,
- link time and debug experience.

## Code Bloat

Every distinct specialization is a potential copy of code:

{% highlight c++ %}
serialize<int>(value);
serialize<long>(value);
serialize<unsigned>(value);
serialize<MyId>(value);
{% endhighlight %}

Compilers and linkers may merge equivalent output, but a design should not depend on that. To control growth:

- move type-independent work into a non-template function,
- normalize equivalent input types before reaching a large implementation,
- explicitly instantiate a closed set of common specializations,
- keep small adapters templated and large algorithms type-erased,
- inspect symbol and binary-size reports rather than counting source lines.

A useful architecture is a thin generic edge around a non-generic core:

```text
typed validation and adaptation -> stable erased representation -> shared implementation
```

This retains a type-safe caller interface while limiting instantiation of the expensive body.

## Build-Time Boundaries

Public template definitions expose their includes and implementation structure to every consumer. A small edit can rebuild a large dependency graph.

At library scale:

- minimize includes in high-fanout template headers,
- separate small declarations and concepts from heavy implementation machinery,
- avoid including a large framework for one trait,
- use forward declarations only where the language permits them safely,
- consider modules or explicit instantiation where the toolchain and distribution model support them,
- track build-time regressions with the same discipline as runtime regressions.

Do not reduce compile time by making the interface cryptic. A clear concept and a focused header are better than a maze of macro-generated declarations.

## ABI and Distribution

Templates are commonly instantiated in the consumer's build. This changes library evolution:

- implementation changes in a header normally require consumer recompilation,
- compiler flags and macros can affect generated specializations,
- inline definitions are part of the effective delivered interface,
- different standard-library or compiler ABIs may make binary exchange unsafe,
- class layout changes affect every specialization and its users.

If a component needs a stable binary interface across independently deployed teams, plugins, or toolchains, place a non-template boundary around it. Templates can remain on one side as adapters, but the boundary should use stable concrete types, an abstract interface, a C API, or another deliberately versioned representation.

## Runtime Polymorphism, Variants, and Templates

These mechanisms solve different problems:

| Mechanism | Type set | Dispatch | Main tradeoff |
|---|---|---|---|
| Template or concept | Open at compile time | Compile time | Recompilation and specialization growth |
| `std::variant` | Closed at compile time | Runtime by active alternative | All alternatives known centrally |
| Virtual interface | Open at runtime | Virtual dispatch | Indirection and ownership design |
| Type erasure | Open at runtime | Erased wrapper dispatch | Implementation complexity and possible allocation |

Choose from the variability model. If new types must arrive without recompiling the host, a template-only abstraction cannot provide that runtime openness.

# 12. Designing Templates for Large Codebases

## Begin With the Semantic Contract

Before choosing syntax, write down:

1. What operation is common across types?
2. Which properties are syntactic, and which are semantic laws?
3. Is the set of supported types open or closed?
4. Is selection compile-time or runtime?
5. Does the type affect layout, ownership, or performance?
6. What diagnostics should an invalid caller see?
7. Who pays for compilation and generated code?

The template parameter list should follow from those answers.

## Keep the Public Surface Smaller Than the Machinery

A mature template implementation may need traits, helper types, detection logic, and compiler workarounds. Most of that should remain private.

Expose:

- a small number of named concepts,
- clear function or class templates,
- documented customization points,
- stable result and ownership semantics.

Hide:

- detection helpers,
- overload-priority tags,
- implementation traits,
- normalization steps,
- compiler-specific branches.

Users should reason about capabilities such as `Sequence` or `Serializable`, not internal expressions such as `has_begin_end_v<T>`.

## Use Concepts as Architecture Vocabulary

A concept becomes a dependency shared by algorithms and types. Treat it as an interface, not a convenient Boolean.

A strong concept has:

- a name that reflects a domain capability,
- minimal syntactic requirements,
- documented semantic expectations,
- deliberate refinement relationships,
- representative positive and negative tests.

Avoid concepts that simply mirror one concrete class. If every requirement mentions members unique to `DatabaseClient`, accepting a template parameter may provide no meaningful generality.

## Control the Specialization Matrix

Template dimensions multiply. A component with four value types, three storage policies, two error policies, and two threading modes has up to 48 combinations before callers add their own types.

Ask which combinations are meaningful and supported. Then reduce the matrix:

- combine policies that cannot vary independently,
- make low-value choices runtime values,
- provide named aliases for supported combinations,
- explicitly instantiate common combinations,
- reject nonsensical combinations with constraints and focused assertions,
- test representative intersections rather than assuming each axis composes perfectly.

Compile-time configurability is an API commitment. Every accepted combination becomes a potential behavior, build, and support surface.

## Design Diagnostics

Invalid use is part of the user experience of a generic library.

Prefer a constraint at the public boundary:

{% highlight c++ %}
#include <concepts>
#include <vector>

template <typename T>
requires std::totally_ordered<T>
void sort_values(std::vector<T>& values);
{% endhighlight %}

This is usually clearer than letting a missing comparison fail several layers inside an algorithm.

Use `static_assert` for an invariant that cannot be expressed naturally as overload viability or when a tailored message adds value:

{% highlight c++ %}
template <typename T, std::size_t Capacity>
class RingBuffer {
  static_assert(Capacity > 0,
                "RingBuffer capacity must be greater than zero");
};
{% endhighlight %}

Do not place a `static_assert` in a candidate when normal constraint failure should allow another overload to win.

## Test at Three Levels

Template libraries need more than runtime unit tests.

**Compile-time contract tests** verify traits, deduction, overload selection, concepts, and result types with `static_assert`.

**Compile-fail tests** verify that invalid use is rejected at the intended boundary and, where tooling permits, that the diagnostic contains useful context.

**Runtime tests** verify behavior, ownership, exceptions, concurrency, and performance for representative specializations.

Also build with every supported compiler. Lookup, diagnostics, compile-time resource use, and implementation limits vary even when runtime behavior agrees.

## Review Checklist

For a production template, review these questions:

- Does a template solve real compile-time variability?
- Can the contract be named with an existing standard concept?
- Are constraints neither weaker nor stronger than the implementation?
- Could a forwarding reference capture unintended calls?
- Are ownership and lifetime rules visible?
- Are customization points explicit and documented?
- Is the overload set coherent after implicit conversions?
- How many specializations will normal use create?
- Can type-independent code move out of the header?
- Is explicit instantiation appropriate?
- Does the design cross an ABI or plugin boundary?
- Are diagnostics tested for invalid callers?
- Is compile time measured in high-fanout consumers?
- Would a runtime strategy, `variant`, or type-erased interface be simpler?

# 13. Common Mistakes

## Making Everything a Template

If a function works only for `UserId`, naming its parameter `T` does not make the design generic. It removes information from the interface and delays errors. Start concrete; generalize when multiple types share a stable semantic contract.

## Treating Duck Typing as a Complete Contract

An expression being valid does not establish its meaning. A type can provide `<` without defining a strict weak ordering. Concepts describe checkable structure; documentation and tests carry semantic laws.

## Exposing Implementation Traits

A public API constrained by `detail::has_member_save_v<T>` couples callers to one detection technique. Expose `Persistable<T>` and keep detection private.

## Using Specialization as a Patch Panel

Many isolated specializations often indicate that the primary abstraction is wrong. Revisit whether behavior belongs in overloads, a policy object, a concept-refined algorithm, or the type itself.

## Ignoring Mixed-Type Operations

`Matrix<int> + Matrix<double>` forces decisions about result type, conversions, precision, and allocation. Either support mixed types deliberately or reject them clearly. Do not let incidental deduction rules define the domain semantics.

## Assuming Header-Only Means Free

Header-only distribution is convenient, but consumers pay parsing, instantiation, optimization, and diagnostic costs. Treat high-fanout headers as performance-critical build infrastructure.

## Optimizing for Cleverness

A shorter metaprogram is not necessarily a better one. Prefer named intermediate concepts and aliases, shallow instantiation, direct `constexpr` algorithms, and ordinary control flow where possible. The reader debugging a production build is part of the audience.

# 14. A Practical Progression

Template programming is easier to learn in layers:

1. Write function templates and understand deduction.
2. Use class templates and recognize that each argument list names a distinct type.
3. Learn non-type parameters, aliases, and variable templates.
4. Understand why definitions are visible at instantiation and when explicit instantiation helps.
5. Use standard type traits and `if constexpr` for small compile-time choices.
6. Learn variadic packs, folds, and forwarding only when an interface needs them.
7. State public requirements with C++20 concepts.
8. Study specialization, lookup, and SFINAE to maintain existing libraries and diagnose edge cases.
9. Measure compile time, code size, runtime behavior, and diagnostics at system scale.
10. Choose consciously between static and runtime polymorphism at architectural boundaries.

The beginner's model remains valid at the final step:

```text
template definition + arguments -> concrete specialization
```

The principal engineer adds the surrounding questions: how many specializations, who instantiates them, what contract selects them, what boundary contains them, how users diagnose failures, and whether compile-time variability is the correct system design.

# 15. Conclusion

C++ templates are a mechanism for producing type-safe families of declarations from compile-time arguments. Function and class templates remove duplication. Deduction makes them usable. Concepts turn hidden requirements into interfaces. Traits, `if constexpr`, packs, and `constexpr` provide controlled compile-time adaptation.

The advanced skill is not writing the most expressive metaprogram. It is choosing where static variation is valuable and containing its costs. A well-designed template presents a small semantic contract, generates a controlled set of useful specializations, produces understandable failures, and stops at boundaries where runtime polymorphism or a stable binary interface is the better tool.

## Further Reading

- [C++ draft: Templates](https://eel.is/c++draft/temp)
- [C++ draft: Template constraints](https://eel.is/c++draft/temp.constr)
- [C++ draft: Template instantiation and specialization](https://eel.is/c++draft/temp.spec)
- [C++ Core Guidelines: Templates and generic programming](https://isocpp.github.io/CppCoreGuidelines/CppCoreGuidelines#S-templates)
- [WG21 P0898R3: Constraints and concepts](https://wg21.link/P0898R3)

## Related Guides

- [C++ Value Categories: lvalues, rvalues, and std::move]({% post_url cpp-reference/2026-08-02-cpp-value-categories %})
- [C++ Value Semantics: Rule of Zero, Copy, and Move]({% post_url cpp-reference/2020-05-29-move %})
- [RAII and Deterministic Resource Management]({% post_url cpp-reference/2026-08-02-raii-resource-management %})
