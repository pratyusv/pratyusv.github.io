---
layout: single
comments: true
toc: true
toc_sticky: true
title: "C++ Concurrency, Part 1: Threads, Mutexes, and Condition Variables"
date: 2026-08-02 13:00:00-0000
categories: C++
tags: [cpp, concurrency, thread, data-race, mutex, lock-guard, unique-lock, condition-variable, interview]
---

## What This Guide Teaches

This is the first guide in a syntax-first concurrency series. It answers six questions:

1. How do I create a thread?
2. How do I wait for it to finish?
3. What goes wrong when threads change the same variable?
4. How do I create and use a mutex lock correctly?
5. How do I make a thread sleep until a condition becomes true?
6. What exactly do `wait()`, a predicate, and `notify_one()` mean?

We first establish thread and mutex syntax. Then we build a condition variable from those pieces instead of presenting it as one unexplained expression.

## Four Words to Know

| Term | Meaning |
|---|---|
| **Thread** | One independently executing path through the program |
| **Shared state** | Data that more than one thread can access |
| **Critical section** | Code that accesses shared state and must be protected |
| **Mutex** | An object that allows only one thread into a protected section at a time |

Keep one picture in mind: **a mutex is the only key to a room**. A thread takes the key, enters, and returns the key when it leaves.

## Syntax Used Throughout the Guide

You do not need to decode every symbol from memory. These patterns appear repeatedly:

| Syntax | How to read it |
|---|---|
| `std::thread` | `thread` from the C++ standard-library namespace `std` |
| `#include <thread>` | Make the declarations from the standard `<thread>` header available |
| `Type name(arguments);` | Construct an object named `name` |
| `object.function(arguments)` | Call a member function on `object` |
| `Type<OtherType>` | Use a class template with `OtherType` as its type argument |
| `{ ... }` | A scope; local objects are destroyed at its closing brace |
| `!expression` | Logical not; true when `expression` is false |
| `// text` | A comment for the reader, not executable code |

For example:

{% highlight c++ %}
std::lock_guard<std::mutex> lock(mutex);
{% endhighlight %}

means: construct an object named `lock`, using `mutex`, from the standard-library lock-guard template specialized for `std::mutex`.

## Your First `std::thread`

{% highlight c++ %}
#include <iostream>
#include <thread>

void printMessage() {
  std::cout << "worker is running\n";
}

int main() {
  std::thread worker(printMessage);
  worker.join();
}
{% endhighlight %}

Compile it with thread support:

{% highlight bash %}
c++ -std=c++17 -pthread main.cpp
{% endhighlight %}

### Reading the Construction Syntax

Focus on this line:

{% highlight c++ %}
std::thread worker(printMessage);
{% endhighlight %}

Read it in pieces:

| Part | Meaning |
|---|---|
| `std::thread` | The type being created |
| `worker` | The name of the thread object |
| `printMessage` | The function the new thread will execute |

Constructing `worker` starts the new thread. The original thread continues into the next statement, so both execution paths may run concurrently.

Notice that the constructor receives `printMessage`, not `printMessage()`:

- `printMessage` refers to the function so the new thread can call it.
- `printMessage()` would call the function immediately in the current thread and try to pass its return value.

### Waiting With `join()`

{% highlight c++ %}
worker.join();
{% endhighlight %}

Read this as: **the current thread waits here until `worker` finishes**.

`join()` does not stop the worker. It only waits for it. After `join()` returns, `worker` is no longer joinable.

A `std::thread` object must not be destroyed while it is still joinable. Doing so calls `std::terminate`. For beginner code, use this rule:

> Every thread you create must have an obvious `join()`.

## Passing Arguments to a Thread

Arguments follow the function name:

{% highlight c++ %}
#include <iostream>
#include <thread>

void printNumber(int value) {
  std::cout << value << '\n';
}

int main() {
  std::thread worker(printNumber, 42);
  worker.join();
}
{% endhighlight %}

The new thread eventually calls something equivalent to:

{% highlight c++ %}
printNumber(42);
{% endhighlight %}

By default, `std::thread` stores copies or moved versions of its arguments. Passing a shared object by reference requires explicit syntax, which we will use after creating a safe counter.

## The Shared-Counter Problem

Now let two threads update one variable:

{% highlight c++ %}
int requests = 0;

void recordRequest() {
  ++requests;
}
{% endhighlight %}

This is unsafe. `++requests` is roughly three operations:

1. Read the old value.
2. Add one.
3. Write the new value.

Starting from zero, the threads can interleave like this:

| Step | Thread A | Thread B | Stored value |
|---|---|---|---:|
| 1 | Reads `0` | | `0` |
| 2 | | Reads `0` | `0` |
| 3 | Writes `1` | | `1` |
| 4 | | Writes `1` | `1` |

Two increments occurred, but the result is one. One update was lost.

This is a **data race**: multiple threads access the same memory concurrently, at least one access is a write, and there is no synchronization. A data race is undefined behavior in C++.

## Creating a Mutex

Declare a mutex next to the data it protects:

{% highlight c++ %}
#include <mutex>

int requests = 0;
std::mutex requests_mutex;
{% endhighlight %}

The name communicates the relationship:

```text
requests_mutex protects requests
```

A mutex does not protect a variable automatically. The programmer must lock the correct mutex on every path that accesses the protected state.

## Manual Locking: Understand It, Then Avoid It

The direct mutex operations are:

{% highlight c++ %}
requests_mutex.lock();
++requests;
requests_mutex.unlock();
{% endhighlight %}

This means:

1. Wait until the mutex is available and take ownership of it.
2. Update the protected value.
3. Release the mutex so another thread can continue.

The problem is that an early return or exception can skip `unlock()`. The mutex then remains locked and other threads may wait forever.

Use a lock object whose destructor releases the mutex automatically.

## Creating a `std::lock_guard`

The normal scoped-lock syntax is:

{% highlight c++ %}
std::lock_guard<std::mutex> lock(requests_mutex);
{% endhighlight %}

Read it from right to left:

| Part | Meaning |
|---|---|
| `requests_mutex` | The mutex to lock |
| `lock` | The local object managing that lock |
| `std::lock_guard<std::mutex>` | A guard designed to manage a `std::mutex` |

`lock_guard` is a class template. The angle-bracket argument `<std::mutex>` tells it which mutex type it will manage.

The constructor locks `requests_mutex`. The destructor unlocks it.

{% highlight c++ %}
void recordRequest() {
  std::lock_guard<std::mutex> lock(requests_mutex);
  ++requests;
}
{% endhighlight %}

When the function returns, local variables are destroyed. Destroying `lock` releases the mutex automatically.

## Lock Lifetime Follows Scope

Braces define how long a lock is held:

{% highlight c++ %}
void processRequest() {
  parseRequest();  // no lock needed

  {
    std::lock_guard<std::mutex> lock(requests_mutex);
    ++requests;
  }  // requests_mutex is unlocked here

  writeResponse();  // no lock needed
}
{% endhighlight %}

Only the shared-state update belongs inside the critical section. Parsing and response I/O do not need the counter mutex.

Holding a mutex during slow work prevents other threads from using the protected state. Keep critical sections small, but large enough to protect the complete invariant.

## Complete Example: Two Safe Workers

{% highlight c++ %}
#include <iostream>
#include <mutex>
#include <thread>

int requests = 0;
std::mutex requests_mutex;

void addRequests(int amount) {
  for (int count = 0; count < amount; ++count) {
    std::lock_guard<std::mutex> lock(requests_mutex);
    ++requests;
  }
}

int main() {
  std::thread first(addRequests, 10'000);
  std::thread second(addRequests, 10'000);

  first.join();
  second.join();

  std::cout << requests << '\n';  // 20000
}
{% endhighlight %}

The apostrophe in `10'000` is a digit separator. It improves readability and does not change the value; `10'000` and `10000` are the same integer.

For each increment:

1. One worker constructs its `lock_guard` and acquires the mutex.
2. The other worker waits if it reaches the same line.
3. The owner increments `requests`.
4. The guard leaves scope and unlocks the mutex.
5. Another worker may acquire it.

The final read in `main` is safe because both workers have been joined. Neither can still be modifying `requests`.

## Put the Mutex Beside the Data

Global variables make the first example short, but a class makes the protection rule explicit:

{% highlight c++ %}
#include <mutex>

class RequestCounter {
 public:
  void increment() {
    std::lock_guard<std::mutex> lock(mutex_);
    ++value_;
  }

  int value() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return value_;
  }

 private:
  mutable std::mutex mutex_;
  int value_{0};
};
{% endhighlight %}

In this class:

- `public:` introduces operations callers may use.
- `private:` introduces implementation details callers cannot access directly.
- The trailing underscore in `mutex_` and `value_` is only a naming convention for data members.
- `{0}` initializes `value_` to zero.

Both reading and writing lock the same mutex. A reader racing with a writer is still a data race.

`value()` is `const`, but locking changes the internal state of the mutex. `mutable` permits that synchronization detail without allowing `value_` itself to be changed by the `const` function.

The function returns a copy of the integer. For containers, do not return a reference, pointer, or iterator to protected data if the lock will be released before the caller uses it.

## Passing a Shared Object by Reference

`std::thread` copies its arguments by default. Use `std::ref` when a function really requires a reference:

{% highlight c++ %}
#include <functional>
#include <thread>

void incrementOnce(RequestCounter& counter) {
  counter.increment();
}

int main() {
  RequestCounter counter;

  std::thread worker(incrementOnce, std::ref(counter));
  worker.join();
}
{% endhighlight %}

`std::ref(counter)` tells `std::thread` to pass the original `counter`, not a copy.

The referenced object must outlive the thread. Here, `counter` remains alive until after `worker.join()`, so the lifetime is safe. The class handles synchronization internally.

## `std::unique_lock` Syntax

`std::lock_guard` is the simplest choice when a mutex stays locked for one scope. Some operations need to unlock and relock it. For those cases, use `std::unique_lock`:

{% highlight c++ %}
std::unique_lock<std::mutex> lock(requests_mutex);
{% endhighlight %}

The construction syntax has the same shape:

| Part | Meaning |
|---|---|
| `requests_mutex` | The mutex to lock |
| `lock` | The object that owns the lock |
| `std::unique_lock<std::mutex>` | A movable lock with explicit lock/unlock operations |

Unlike `lock_guard`, a `unique_lock` can be controlled during its lifetime:

{% highlight c++ %}
std::unique_lock<std::mutex> lock(requests_mutex);  // locked

updateSharedState();

lock.unlock();                                    // unlocked
performSlowWork();
lock.lock();                                      // locked again
verifySharedState();
{% endhighlight %}

Do not unlock and relock without a reason: the protected state may change while the mutex is unlocked.

Condition variables require `unique_lock` because waiting must temporarily unlock the mutex so another thread can change the condition. The next section traces that operation line by line.

## Why a Condition Variable Is Needed

Imagine a consumer thread that must wait until a message is ready.

It could repeatedly check a boolean:

{% highlight c++ %}
while (!ready) {
  // Keep checking.
}
{% endhighlight %}

This is **busy waiting**. The thread consumes CPU even though it has no useful work.

It also cannot solve the problem by locking a mutex and keeping that mutex while it waits:

{% highlight c++ %}
std::lock_guard<std::mutex> lock(mutex);

while (!ready) {
  // Wrong: the producer cannot acquire mutex to set ready.
}
{% endhighlight %}

The consumer needs one coordinated operation:

> Release the mutex and sleep. When awakened, acquire the mutex again and recheck the condition.

That is the job of `std::condition_variable`.

## The Three Objects

Start with these declarations:

{% highlight c++ %}
#include <condition_variable>
#include <mutex>

std::mutex mutex;
std::condition_variable condition;
bool ready = false;
{% endhighlight %}

| Object | Responsibility |
|---|---|
| `ready` | The actual state the consumer cares about |
| `mutex` | Protects every access to `ready` |
| `condition` | Lets the consumer sleep and later wake up |

The condition variable does not contain the condition. It does not know what `ready` means. Despite its name, it is only a waiting and notification mechanism.

**Memory hook: `ready` is the fact; `condition` is the doorbell.**

## Waiting Syntax

The consumer writes:

{% highlight c++ %}
std::unique_lock<std::mutex> lock(mutex);

condition.wait(lock, [] {
  return ready;
});
{% endhighlight %}

There are two separate statements. Understand each one before combining them.

### Statement 1: Create the Lock

{% highlight c++ %}
std::unique_lock<std::mutex> lock(mutex);
{% endhighlight %}

This constructs a local object named `lock` and immediately locks `mutex`.

We cannot use `lock_guard` here. `wait()` must temporarily unlock and relock the mutex, and `unique_lock` provides that capability.

### Statement 2: Wait for the Predicate

{% highlight c++ %}
condition.wait(lock, [] {
  return ready;
});
{% endhighlight %}

Read it in pieces:

| Part | Meaning |
|---|---|
| `condition` | The condition-variable object |
| `.wait(...)` | Sleep until the supplied condition becomes true |
| `lock` | The lock that `wait` may release and reacquire |
| `[] { return ready; }` | A function that checks the actual state |

The small function passed to `wait` is called a **predicate**. A predicate returns `true` or `false`.

## Reading the Lambda Predicate

{% highlight c++ %}
[] {
  return ready;
}
{% endhighlight %}

This is a C++ lambda: a small unnamed function.

| Syntax | Meaning |
|---|---|
| `[]` | Capture list; this example needs no local variables because `ready` is global |
| `{ ... }` | Function body |
| `return ready;` | Return whether the consumer may continue |

Inside a class, the same predicate commonly uses `[this]` so it can read a data member:

{% highlight c++ %}
condition_.wait(lock, [this] {
  return ready_;
});
{% endhighlight %}

`[this]` captures the current object pointer. The lambda can then access `ready_` and other members of that object.

## What `wait()` Actually Does

This call:

{% highlight c++ %}
condition.wait(lock, [] {
  return ready;
});
{% endhighlight %}

behaves conceptually like:

{% highlight c++ %}
while (!ready) {
  condition.wait(lock);
}
{% endhighlight %}

The complete sequence is:

1. The consumer owns `mutex` through `lock`.
2. `wait` checks the predicate: is `ready` true?
3. If true, `wait` returns immediately.
4. If false, `wait` releases `mutex` and puts the consumer to sleep.
5. A notification may wake the consumer.
6. Before continuing, `wait` acquires `mutex` again.
7. It checks the predicate again.
8. It returns only when the predicate is true.

When `wait` returns, the consumer owns the mutex again. Protected state can therefore be inspected safely.

The critical operation is step 4: releasing the mutex and entering the wait are coordinated so the producer can take the mutex and change `ready`.

## Notification Syntax

The producer changes the state first:

{% highlight c++ %}
{
  std::lock_guard<std::mutex> lock(mutex);
  ready = true;
}

condition.notify_one();
{% endhighlight %}

Read this in order:

1. Construct `lock` and acquire `mutex`.
2. Change the protected state to `ready = true`.
3. Leave the scope, destroying `lock` and releasing `mutex`.
4. Call `notify_one()` to wake one waiting thread.

`notify_one()` takes no lock argument. It sends a notification through `condition`; the awakened consumer uses its own `unique_lock` to reacquire `mutex`.

Remember the producer sequence as:

> **Lock → change the state → unlock → notify.**

Notifying while still holding the mutex is allowed, but notifying after unlocking often avoids waking a consumer only for it to block immediately on the same mutex.

## Complete Example: One Message

{% highlight c++ %}
#include <condition_variable>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>

std::mutex mutex;
std::condition_variable condition;
std::string message;
bool ready = false;

void consumeMessage() {
  std::unique_lock<std::mutex> lock(mutex);

  condition.wait(lock, [] {
    return ready;
  });

  std::cout << message << '\n';
}

void produceMessage() {
  {
    std::lock_guard<std::mutex> lock(mutex);
    message = "work is ready";
    ready = true;
  }

  condition.notify_one();
}

int main() {
  std::thread consumer(consumeMessage);
  std::thread producer(produceMessage);

  producer.join();
  consumer.join();
}
{% endhighlight %}

Both `message` and `ready` are shared state protected by the same mutex. The consumer reads `message` only after the predicate is true and while it owns the mutex.

## Execution Timeline

One possible execution is:

| Consumer | Mutex | Producer |
|---|---|---|
| Creates `unique_lock` | Owned by consumer | |
| Checks `ready`: false | Owned by consumer | |
| Calls `wait`; unlocks and sleeps | Available | |
| | Owned by producer | Creates `lock_guard` |
| | Owned by producer | Writes `message` and sets `ready` |
| | Available | Destroys guard and calls `notify_one()` |
| Wakes and reacquires mutex | Owned by consumer | |
| Checks `ready`: true | Owned by consumer | |
| Prints `message` | Owned by consumer | |
| Function ends | Available | |

The producer might run first instead. That is still correct:

1. The producer sets `ready = true` and notifies.
2. The consumer later acquires the mutex.
3. The predicate is already true.
4. `wait` returns immediately without sleeping.

Correctness comes from the stored state `ready`, not from remembering a notification.

## Why `if` Is Wrong

This code is unsafe:

{% highlight c++ %}
if (!ready) {
  condition.wait(lock);
}

useMessage();
{% endhighlight %}

A thread may wake even when no useful state change occurred. This is called a **spurious wakeup**. With several consumers, another consumer might also take the available work before this one reacquires the mutex.

Waking means **check again**, not **the condition is definitely true**.

Use either an explicit loop:

{% highlight c++ %}
while (!ready) {
  condition.wait(lock);
}
{% endhighlight %}

or, preferably, the predicate overload:

{% highlight c++ %}
condition.wait(lock, [] {
  return ready;
});
{% endhighlight %}

The predicate overload performs the repeated check for you.

## `notify_one()` vs. `notify_all()`

| Function | Meaning | Typical use |
|---|---|---|
| `notify_one()` | Wake one waiting thread | One new work item became available |
| `notify_all()` | Wake every waiting thread | Shutdown or a state change relevant to everyone |

A notification does not transfer mutex ownership and does not carry data. It only gives waiting threads a reason to reacquire the mutex and test their predicates.

## From a Boolean to a Queue

The same syntax works when the condition is “the queue has an item”:

{% highlight c++ %}
std::unique_lock<std::mutex> lock(mutex);

data_available.wait(lock, [&queue] {
  return !queue.empty();
});

int value = queue.front();
queue.pop();
{% endhighlight %}

Here `[&queue]` captures the local variable `queue` by reference so the predicate can inspect it.

The mapping is direct:

| One-message example | Queue example |
|---|---|
| `ready` | `!queue.empty()` |
| `message` | `queue.front()` |
| Producer sets `ready` | Producer pushes an item |
| Consumer reads message | Consumer pops an item |

## Is `std::queue` Thread-Safe?

No. `std::queue` provides ordinary single-threaded operations:

{% highlight c++ %}
queue.push(value);   // add at the back
queue.front();       // inspect the front item
queue.pop();         // remove the front item; returns void
queue.empty();       // test whether the queue is empty
{% endhighlight %}

If several threads call these operations on the same queue, the program needs external synchronization.

There is another important detail: reading and removing are two separate operations. `std::queue::pop()` does not return the removed value, so code normally reads `front()` and then calls `pop()`. Both operations must happen under the same lock.

## A Thread-Safe Queue Interface

This wrapper provides four useful operations:

| Operation | Behavior |
|---|---|
| `push(value)` | Add one item and wake a waiting consumer |
| `tryPop()` | Return one item immediately, or `std::nullopt` if empty |
| `waitAndPop()` | Sleep until an item exists, then return it |
| `empty()` | Return a momentary snapshot of whether the queue is empty |

### Reading the Class-Template Syntax

The queue works with different value types because it is a class template:

{% highlight c++ %}
template <typename T>
class ThreadSafeQueue {
  // ...
};
{% endhighlight %}

Read it in pieces:

| Syntax | Meaning |
|---|---|
| `template <typename T>` | Introduce `T` as a placeholder for a type |
| `class ThreadSafeQueue` | Define a class template named `ThreadSafeQueue` |
| `ThreadSafeQueue<int>` | Create the version whose `T` is `int` |
| `ThreadSafeQueue<std::string>` | Create the version whose `T` is `std::string` |

Inside the class, `T value` means “a value of whichever type the caller selected.”

{% highlight c++ %}
#include <condition_variable>
#include <mutex>
#include <optional>
#include <queue>
#include <utility>

template <typename T>
class ThreadSafeQueue {
 public:
  void push(T value) {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      queue_.push(std::move(value));
    }

    data_available_.notify_one();
  }

  std::optional<T> tryPop() {
    std::lock_guard<std::mutex> lock(mutex_);

    if (queue_.empty()) {
      return std::nullopt;
    }

    T value = std::move(queue_.front());
    queue_.pop();
    return value;
  }

  T waitAndPop() {
    std::unique_lock<std::mutex> lock(mutex_);

    data_available_.wait(lock, [this] {
      return !queue_.empty();
    });

    T value = std::move(queue_.front());
    queue_.pop();
    return value;
  }

  bool empty() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return queue_.empty();
  }

 private:
  mutable std::mutex mutex_;
  std::condition_variable data_available_;
  std::queue<T> queue_;
};
{% endhighlight %}

The queue, mutex, and condition variable belong to one object. Callers cannot access the underlying `std::queue` without going through the synchronized interface.

The private members read as follows:

| Declaration | Meaning |
|---|---|
| `mutable std::mutex mutex_;` | Mutex protecting the queue; `mutable` permits locking inside `const` operations |
| `std::condition_variable data_available_;` | Notification mechanism for sleeping consumers |
| `std::queue<T> queue_;` | FIFO storage containing values of type `T` |

`bool empty() const` promises not to change the queue's logical contents. Locking still changes the mutex's internal bookkeeping, so the mutex is `mutable`. The earlier `RequestCounter::value()` method uses the same pattern.

### Why the Code Uses `std::move`

{% highlight c++ %}
queue_.push(std::move(value));
{% endhighlight %}

`value` is a local object owned by `push`. After placing it in the queue, `push` no longer needs its contents. `std::move(value)` allows the queue to move those contents instead of requiring another copy when `T` supports moving.

`std::move` does not move anything by itself. It marks `value` as an object whose resources may be transferred by the receiving operation.

## How `push()` Works

{% highlight c++ %}
void push(T value) {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    queue_.push(std::move(value));
  }

  data_available_.notify_one();
}
{% endhighlight %}

The producer:

1. Locks `mutex_`.
2. Moves the value into `queue_`.
3. Unlocks when the inner scope ends.
4. Wakes one consumer.

One pushed item can satisfy one waiting consumer, so `notify_one()` is appropriate.

## Why `tryPop()` Combines Three Operations

{% highlight c++ %}
if (queue_.empty()) {
  return std::nullopt;
}

T value = std::move(queue_.front());
queue_.pop();
{% endhighlight %}

`queue_.front()` refers to the current front object. Moving from it constructs the local `value`; `queue_.pop()` then removes the moved-from queue element. Both statements remain inside the same lock scope.

The empty check, front access, and removal all occur while one `lock_guard` owns the mutex. No other consumer can remove the front item between those steps.

`tryPop()` uses `std::optional<T>` because an empty queue has no value to return:

{% highlight c++ %}
if (std::optional<int> value = work.tryPop()) {
  process(*value);
}
{% endhighlight %}

Read the syntax in order:

1. `work.tryPop()` returns a `std::optional<int>`.
2. `std::optional<int> value = ...` creates a local optional named `value`.
3. Because the declaration appears inside `if`, the body runs only when the optional contains an integer.
4. `*value` accesses the contained integer.

`std::nullopt` is the standard value meaning “this optional contains nothing.” The optional is useful because every integer, including zero and negative values, can remain a valid queue item.

## How `waitAndPop()` Uses the Condition Variable

{% highlight c++ %}
std::unique_lock<std::mutex> lock(mutex_);

data_available_.wait(lock, [this] {
  return !queue_.empty();
});
{% endhighlight %}

The queue predicate is `!queue_.empty()`:

1. If an item already exists, `wait` returns immediately.
2. If the queue is empty, `wait` unlocks `mutex_` and sleeps.
3. `push()` adds an item and calls `notify_one()`.
4. The consumer wakes, reacquires `mutex_`, and checks again.
5. Only then does it read `front()` and call `pop()`.

The predicate uses `[this]` because `queue_` is a member of the current `ThreadSafeQueue` object.

## Complete Multithreaded Queue Example

The consumer starts first and sleeps because the queue is empty. The producer then pushes five values. Each push gives the consumer work to process.

{% highlight c++ %}
#include <iostream>
#include <thread>

int main() {
  ThreadSafeQueue<int> work;

  std::thread consumer([&work] {
    for (int count = 0; count < 5; ++count) {
      int value = work.waitAndPop();
      std::cout << "consumed " << value << '\n';
    }
  });

  std::thread producer([&work] {
    for (int value = 1; value <= 5; ++value) {
      work.push(value);
    }
  });

  producer.join();
  consumer.join();
}
{% endhighlight %}

The thread bodies are lambdas:

{% highlight c++ %}
[&work] {
  // thread body
}
{% endhighlight %}

`[&work]` captures the existing `work` queue by reference. Both threads therefore use the same queue object rather than separate copies. `main` keeps `work` alive until after both `join()` calls.

Because `std::queue` is FIFO, this one-producer example consumes `1, 2, 3, 4, 5` in order. With several producers, each individual push remains safe, but the combined order depends on thread scheduling.

This example knows exactly five items will arrive. A production queue also needs shutdown behavior so consumers do not wait forever after producers finish. The broader concurrency guide adds a `close()` flag and wakes every consumer during shutdown.

## Why `empty()` Is Only a Snapshot

This method is thread-safe by itself:

{% highlight c++ %}
bool empty() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return queue_.empty();
}
{% endhighlight %}

However, its answer may become stale immediately after it returns. Another producer can push, or another consumer can pop.

Do not write logic that assumes a later operation is guaranteed by `empty()`:

{% highlight c++ %}
if (!work.empty()) {
  // Another consumer may remove the item here.
  std::optional<int> value = work.tryPop();
}
{% endhighlight %}

`value` can still be `std::nullopt`. Usually, call `tryPop()` directly and inspect its result. The safe operation is **check and remove together**, not `empty()` followed by a separate assumption.

## Choosing a Lock Type

| Type | Beginner rule |
|---|---|
| `std::lock_guard<std::mutex>` | Default choice for one mutex held for one scope |
| `std::unique_lock<std::mutex>` | Use when an operation must unlock/relock, especially condition-variable waiting |
| `std::scoped_lock` | Use when several mutexes must be acquired together |
| `std::shared_lock<std::shared_mutex>` | Use for shared read access in a reader-writer design |

The later guides will introduce `scoped_lock` and `shared_lock` with complete examples. Do not choose a more flexible lock type when `lock_guard` already expresses the required lifetime.

## Data Race vs. Race Condition

The terms are related but not identical:

- A **data race** is unsynchronized conflicting memory access. In C++, it is undefined behavior.
- A **race condition** is a broader logical error in which timing changes the result.

For example, these two individually locked operations may still form a logical race:

{% highlight c++ %}
if (!queue.empty()) {  // locks internally, then unlocks
  queue.pop();         // locks again later
}
{% endhighlight %}

Another consumer could remove the final item between `empty()` and `pop()`. The complete “check and remove” decision must be one protected operation.

## Common Mistakes

### Locking Only Writers

Readers must use the mutex too if they can overlap a write.

### Manually Calling `lock()` and `unlock()`

An exception or early return can leave the mutex locked. Prefer RAII lock objects.

### Holding a Lock During Slow Work

Avoid sleeping, blocking I/O, callbacks, and expensive computation while holding a mutex unless the invariant requires it.

### Returning Protected References

A reference can outlive the lock that made access safe. Prefer returning a copy or providing an operation that completes under the lock.

### Forgetting `join()`

Destroying a joinable `std::thread` terminates the program. Make thread ownership and shutdown visible in the same scope.

### Assuming One Locked Operation Protects a Workflow

Several individually safe functions can still create a race condition when a decision spans multiple calls.

## Interview Reconstruction

When asked to make shared state thread-safe:

1. Identify the exact shared data.
2. State the invariant the mutex protects.
3. Put the mutex beside that data.
4. Lock every read and write that can overlap.
5. Use `lock_guard` unless unlocking/relocking is required.
6. Keep the protected operation in one lock scope.
7. Join threads before destroying shared state.

For a condition variable:

1. Put the actual state behind a mutex.
2. Create a `unique_lock` in the waiting thread.
3. Pass that lock and a predicate to `wait`.
4. Change the state under the same mutex in the producer.
5. Unlock and call `notify_one()` or `notify_all()`.
6. Treat every wakeup as a reason to recheck the predicate.

## Memory Hooks

- **Thread:** another execution path starts at the function passed to `std::thread`.
- **Join:** wait until that execution path finishes.
- **Data race:** conflicting access to the same memory without synchronization.
- **Mutex:** one key to the protected room.
- **Lock guard:** take the key now and return it automatically at the closing brace.
- **Unique lock:** a lock object that may temporarily return and retake the key.
- **Condition variable:** a doorbell that lets a thread sleep; the predicate is the actual condition.
- **Wait:** check, unlock and sleep, relock, then check again.
- **Notify:** announce that shared state may have changed.

## Related Guides

- [Understanding C++ Concurrency: Locks, Conditions, and Queues]({% post_url cpp-reference/2026-08-02-cpp-concurrency-fundamentals %})
- [RAII: Deterministic Resource Management in C++]({% post_url cpp-reference/2026-08-02-raii-resource-management %})

## Further Reading

- [Microsoft Learn: `<thread>`](https://learn.microsoft.com/en-us/cpp/standard-library/thread?view=msvc-170)
- [Microsoft Learn: `<mutex>`](https://learn.microsoft.com/en-us/cpp/standard-library/mutex?view=msvc-170)
- [C++ Core Guidelines: Concurrency](https://isocpp.github.io/CppCoreGuidelines/CppCoreGuidelines#S-concurrency)
