---
layout: single
comments: true
title: "Understanding C++ Concurrency: Locks, Conditions, and Queues"
date: 2026-08-02 12:00:00-0000
last_modified_at: 2026-08-02 00:00:00-0000
categories: C++
tags: [cpp, concurrency, mutex, condition-variable, deadlock, producer-consumer, interview]
---

> New to C++ concurrency syntax? Start with [Part 1: Threads, Mutexes, and Condition Variables]({% post_url cpp-reference/2026-08-02-cpp-threads-data-races-mutexes %}). It introduces `std::thread`, `join()`, mutex lock types, condition-variable predicates, `wait()`, and notifications line by line before this broader guide combines them.

## Start With One Picture

Imagine several people working around one shared whiteboard. The people are threads; the whiteboard is shared memory.

| C++ tool | Picture to remember | Job |
|---|---|---|
| `std::mutex` | One key to a room | Only one thread enters |
| `std::shared_mutex` | A library reading room | Many readers or one writer |
| `std::condition_variable` | A doorbell | Sleep until something may have changed |
| `std::atomic` | A tally counter | Safely change one simple value |
| Thread-safe queue | A conveyor belt | Producers pass work to consumers |

Concurrency becomes manageable when every tool has one job. For each example, ask three questions:

1. What data is shared?
2. Who may access it at the same time?
3. What event lets a waiting thread continue?

## Starting and Joining a Thread

A `std::thread` runs a function independently from the thread that created it.

{% highlight c++ %}
#include <iostream>
#include <thread>

void doWork() {
  std::cout << "worker is running\n";
}

int main() {
  std::thread worker(doWork);

  // main and worker may now run concurrently.

  worker.join();
}
{% endhighlight %}

`join()` means: **wait here until this thread finishes**. A joinable `std::thread` must be joined or detached before it is destroyed; otherwise the program calls `std::terminate`.

For normal application code, prefer a clear ownership scope and join the thread. Detached threads make lifetime and shutdown harder to reason about.

## Data Races and Race Conditions

A **data race** occurs when two threads access the same memory concurrently, at least one access is a write, and the accesses are not synchronized. In C++, a data race is undefined behavior.

{% highlight c++ %}
int requests = 0;

void recordRequest() {
  ++requests;  // unsafe when multiple threads call this
}
{% endhighlight %}

`++requests` looks like one operation, but it is roughly:

1. Read `requests`.
2. Add one.
3. Write the new value.

Starting from zero, this can happen:

| Step | Thread A | Thread B | Stored value |
|---|---|---|---:|
| 1 | Reads `0` | | `0` |
| 2 | | Reads `0` | `0` |
| 3 | Writes `1` | | `1` |
| 4 | | Writes `1` | `1` |

Two requests occurred, but the final count is one. This is a **lost update**.

A **race condition** is broader: the program's result depends on timing. A program can be free of data races and still have a logical race, such as checking that a queue is non-empty under one lock and trying to remove an item under a later lock.

The goal is not merely to lock individual reads and writes. Protect the complete decision that must behave as one operation.

## `std::mutex`: One Thread at a Time

**Memory hook: a mutex is the only key to a room.**

A thread takes the key, enters the protected section, and returns the key when it leaves. Other threads wait for the key.

{% highlight c++ %}
#include <mutex>

class RequestCounter {
 public:
  void increment() {
    std::lock_guard<std::mutex> lock(mutex_);
    ++requests_;
  }

  int value() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return requests_;
  }

 private:
  mutable std::mutex mutex_;
  int requests_{0};
};
{% endhighlight %}

`std::lock_guard` acquires the mutex in its constructor and releases it in its destructor. The lock is released on normal return and during exception unwinding.

In this example, `mutex_` protects `requests_`. Reading needs the same lock as writing; otherwise a read could happen during an update.

Prefer RAII lock types over manual `lock()` and `unlock()` calls:

| Lock type | Use it when |
|---|---|
| `std::lock_guard` | The mutex stays locked for one scope |
| `std::unique_lock` | The lock must be released/reacquired, moved, or used with a condition variable |
| `std::shared_lock` | A reader needs shared ownership of a `std::shared_mutex` |
| `std::scoped_lock` | One scope needs one or several mutexes, especially several at once |

The mutex belongs with the data it protects. Documenting that relationship is more useful than scattering unrelated global mutexes through the program.

## `std::mutex` vs. `std::shared_mutex`

**Memory hook: many people may read a library notice board, but someone updating it needs everyone else to step away.**

A regular mutex permits one thread inside the protected region. It does not distinguish reading from writing.

A `std::shared_mutex` has two modes:

- A `std::shared_lock` allows multiple readers at the same time.
- A `std::unique_lock` gives one writer exclusive access and excludes all readers.

| Operation | `std::mutex` | `std::shared_mutex` |
|---|---:|---:|
| Readers at the same time | 1 | Many |
| Writers at the same time | 1 | 1 |
| Readers while writing | 0 | 0 |

This can help when reads are frequent, writes are rare, and each read holds the lock long enough for serialization to matter. A configuration map is a natural example: many threads read settings, while an administrator changes them occasionally.

{% highlight c++ %}
#include <mutex>
#include <optional>
#include <shared_mutex>
#include <string>
#include <unordered_map>
#include <utility>

class Configuration {
 public:
  std::optional<std::string> get(const std::string& key) const {
    std::shared_lock<std::shared_mutex> lock(mutex_);

    auto entry = values_.find(key);
    if (entry == values_.end()) {
      return std::nullopt;
    }

    return entry->second;
  }

  void set(std::string key, std::string value) {
    std::unique_lock<std::shared_mutex> lock(mutex_);
    values_[std::move(key)] = std::move(value);
  }

 private:
  mutable std::shared_mutex mutex_;
  std::unordered_map<std::string, std::string> values_;
};
{% endhighlight %}

`get` returns a copy. Returning a reference or iterator would let protected data escape after the lock is released.

Do not replace every mutex with a shared mutex. Reader tracking adds overhead, fairness is implementation-dependent, and writers can wait behind readers. Start with `std::mutex`; use `std::shared_mutex` when the workload is genuinely read-heavy and measurement shows a benefit.

## Condition Variables: Sleep Until State Changes

**Memory hook: a condition variable is a doorbell, not the delivery.**

Suppose a consumer wants to remove work from a queue. Repeatedly checking the queue wastes CPU:

{% highlight c++ %}
while (queue.empty()) {
  // Busy waiting: repeatedly consumes CPU while no work exists.
}
{% endhighlight %}

A `std::condition_variable` lets the consumer sleep. Another thread changes the shared state and rings the doorbell with a notification.

The notification is not the state. Just as a doorbell does not prove that a package is outside, a notification does not prove that work is available. It means: **wake up, lock the mutex, and check the state again**.

Here is the smallest complete pattern:

{% highlight c++ %}
#include <condition_variable>
#include <mutex>

std::mutex mutex;
std::condition_variable ready_changed;
bool ready = false;

void waitUntilReady() {
  std::unique_lock<std::mutex> lock(mutex);

  ready_changed.wait(lock, [] {
    return ready;
  });

  // ready is true and mutex is locked here.
}

void makeReady() {
  {
    std::lock_guard<std::mutex> lock(mutex);
    ready = true;
  }

  ready_changed.notify_one();
}
{% endhighlight %}

The order is important:

1. Lock the mutex.
2. Change the shared state.
3. Unlock the mutex.
4. Notify a waiter.

{% highlight c++ %}
std::unique_lock<std::mutex> lock(mutex);

data_available.wait(lock, [&queue] {
  return !queue.empty();
});
{% endhighlight %}

While waiting, the condition variable releases the mutex and puts the thread to sleep as one coordinated operation. Before `wait` returns, it reacquires the mutex. This requires `std::unique_lock` because `wait` must unlock and relock it.

### Why the Predicate Must Be Rechecked

A waiting thread can wake even when no useful notification occurred. This is a **spurious wakeup**. It can also wake after another consumer has already taken the available item.

Therefore, waking up never means “the condition is definitely true.” It means “check again.”

This is wrong:

{% highlight c++ %}
if (queue.empty()) {
  data_available.wait(lock);
}

// The queue is not guaranteed to contain an item here.
{% endhighlight %}

The correct mental model is a loop:

{% highlight c++ %}
while (queue.empty()) {
  data_available.wait(lock);
}
{% endhighlight %}

The predicate overload expresses that loop directly:

{% highlight c++ %}
data_available.wait(lock, [&queue] {
  return !queue.empty();
});
{% endhighlight %}

It also handles notifications that happen before the consumer starts waiting. The consumer first checks the actual state; if data already exists, it never sleeps.

## A Complete Blocking Queue

**Memory hook: producers place boxes on a conveyor belt; consumers remove them. An empty belt makes consumers sleep instead of stare at it.**

The queue combines the ideas already introduced:

| Member | Responsibility |
|---|---|
| `queue_` | Stores pending work |
| `mutex_` | Protects the queue and shutdown flag |
| `data_available_` | Wakes sleeping consumers |
| `closed_` | Tells consumers that no more work will arrive |

This queue supports multiple producers and consumers. `pop()` sleeps while the queue is empty, and `close()` gives consumers a clean shutdown path.

{% highlight c++ %}
#include <condition_variable>
#include <mutex>
#include <optional>
#include <queue>
#include <stdexcept>
#include <utility>

template <typename T>
class BlockingQueue {
 public:
  void push(T value) {
    {
      std::lock_guard<std::mutex> lock(mutex_);

      if (closed_) {
        throw std::logic_error("push on closed queue");
      }

      queue_.push(std::move(value));
    }

    data_available_.notify_one();
  }

  std::optional<T> pop() {
    std::unique_lock<std::mutex> lock(mutex_);

    data_available_.wait(lock, [this] {
      return closed_ || !queue_.empty();
    });

    if (queue_.empty()) {
      return std::nullopt;
    }

    T value = std::move(queue_.front());
    queue_.pop();
    return value;
  }

  void close() {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      closed_ = true;
    }

    data_available_.notify_all();
  }

 private:
  std::mutex mutex_;
  std::condition_variable data_available_;
  std::queue<T> queue_;
  bool closed_{false};
};
{% endhighlight %}

### How `push` Works

1. Lock the mutex protecting both `queue_` and `closed_`.
2. Reject work after shutdown.
3. Add the item while the queue is protected.
4. Release the lock at the end of the inner scope.
5. Wake one consumer because one new item is available.

Remember it as: **lock, change state, unlock, ring the bell**.

The notification occurs after unlocking. A notified consumer can then acquire the mutex immediately instead of waking only to block on the producer's lock.

### How `pop` Works

The predicate describes both reasons a consumer may proceed:

{% highlight c++ %}
return closed_ || !queue_.empty();
{% endhighlight %}

- If an item exists, remove and return it.
- If the queue is closed but still contains items, drain those items first.
- If the queue is closed and empty, return `std::nullopt` so the consumer can stop.

The check and removal happen under one lock. No other consumer can take the item between them.

Remember the consumer as a waiting-room loop:

1. Is there work, or has the queue closed?
2. If neither is true, sleep.
3. After waking, check again.
4. If work exists, take exactly one item.
5. If the queue is closed and empty, leave.

### Why `close` Uses `notify_all`

Adding one item creates work for one consumer, so `push` uses `notify_one`.

Closing changes the stopping condition for every consumer. No more items may arrive, so `close` uses `notify_all` to let all sleepers observe shutdown.

With multiple producers, a coordinator should call `close()` only after every producer has finished.

## Producer-Consumer Example

One thread produces work. Another sleeps until work becomes available, processes every item, and exits after the queue is closed and drained.

{% highlight c++ %}
#include <iostream>
#include <thread>

int main() {
  BlockingQueue<int> work;

  std::thread producer([&work] {
    for (int value = 1; value <= 5; ++value) {
      work.push(value);
    }

    work.close();
  });

  std::thread consumer([&work] {
    while (std::optional<int> value = work.pop()) {
      std::cout << "processing " << *value << '\n';
    }
  });

  producer.join();
  consumer.join();
}
{% endhighlight %}

`join()` is part of the lifetime design: `main` waits for both threads before destroying the queue. Destroying a joinable `std::thread` calls `std::terminate`.

## Deadlock: Correct Locks in the Wrong Combination

**Memory hook: two people each hold one tool and wait forever for the other tool.**

Imagine this order:

| Thread A | Thread B |
|---|---|
| Locks `first_mutex` | Locks `second_mutex` |
| Waits for `second_mutex` | Waits for `first_mutex` |

Neither thread can continue, and neither releases what it already holds. That is a deadlock.

Four conditions make deadlock possible:

1. A resource can be held by only one thread.
2. A thread holds one resource while waiting for another.
3. Resources cannot be forcibly taken away.
4. The waiting relationships form a cycle.

An account transfer can create exactly this problem. A transfer from A to B locks account A first. A simultaneous transfer from B to A might lock account B first.

Do not acquire several mutexes independently:

{% highlight c++ %}
std::lock_guard<std::mutex> first_lock(first_mutex);
std::lock_guard<std::mutex> second_lock(second_mutex);
{% endhighlight %}

In C++17, acquire them together with `std::scoped_lock`:

{% highlight c++ %}
std::scoped_lock lock(first_mutex, second_mutex);
{% endhighlight %}

The important idea is not “always lock the first argument first.” `std::scoped_lock` uses a deadlock-avoiding algorithm for the mutexes as a group.

For older C++, `std::lock` performs deadlock-avoiding acquisition and `std::adopt_lock` transfers responsibility to RAII guards:

{% highlight c++ %}
std::lock(first_mutex, second_mutex);

std::lock_guard<std::mutex> first_lock(
    first_mutex, std::adopt_lock);
std::lock_guard<std::mutex> second_lock(
    second_mutex, std::adopt_lock);
{% endhighlight %}

`std::adopt_lock` means “this mutex is already locked; manage and unlock it, but do not lock it again.” Use it only when the current thread already owns the mutex.

## Dining Philosophers

Dining Philosophers is the same two-tool problem arranged in a circle:

- Each philosopher is a thread.
- Each fork is a mutex.
- Eating requires two mutexes.

If everyone takes the left fork and then waits for the right fork, every philosopher holds one resource while waiting for the next. The final wait points back to the first philosopher and completes a cycle.

Possible prevention strategies are:

- Acquire both fork mutexes with `std::scoped_lock`.
- Give every fork a global order and always acquire the lower-numbered fork first.
- Use a coordinator that limits how many philosophers may try to eat at once.
- Avoid holding one resource while waiting indefinitely for another.

The general interview lesson is not the dinner story. It is to identify circular wait and break it through atomic multi-lock acquisition, consistent ordering, or coordination.

## Where Atomics Fit

**Memory hook: an atomic is a thread-safe tally counter, not a lock for an entire room.**

An atomic is suitable when one independent value needs indivisible operations:

{% highlight c++ %}
#include <atomic>

std::atomic<int> completed{0};

void recordCompletion() {
  ++completed;
}
{% endhighlight %}

Multiple threads can increment this counter without losing updates.

Atomics are not a drop-in replacement for a mutex. Making a map size atomic does not make map mutation safe, and making several fields individually atomic does not make a compound invariant atomic.

The default memory ordering is the clearest starting point. Weaker memory orders such as `memory_order_relaxed` are an advanced optimization and require a specific, reviewed justification.

## Practical Review Checklist

Before approving concurrent code, ask:

1. Which data is shared, and which mutex or atomic protects it?
2. What invariant must remain true across multiple operations?
3. Can a reference, pointer, or iterator escape after its lock is released?
4. Does every condition-variable wait use a predicate?
5. Is there an explicit shutdown condition for sleeping threads?
6. Can two code paths acquire the same mutexes in different orders?
7. Is unknown code, a callback, blocking I/O, or expensive work called while holding a lock?
8. Are all joinable threads joined before their owners are destroyed?
9. Is `std::shared_mutex` supported by workload measurements rather than assumption?

## Interview Reconstruction

For a producer-consumer question, rebuild the design in this order:

1. Put the queue and shutdown flag behind one mutex.
2. Use `std::unique_lock` in the consumer because `wait` must unlock it.
3. Wait for `closed || !queue.empty()`.
4. Check and pop while still holding the mutex.
5. Notify one waiter after a push.
6. Notify all waiters during shutdown.
7. Join every thread before shared state is destroyed.

For a deadlock question, ask: “Which thread holds what, what is it waiting for, and can those waits form a cycle?”

## Five Memory Hooks

If the syntax fades after a few weeks, reconstruct it from these pictures:

1. **Mutex:** one key, one thread inside.
2. **Shared mutex:** many readers or one writer.
3. **Condition variable:** change the state, then ring the doorbell; waking means check again.
4. **Blocking queue:** producers add work, consumers sleep until work or shutdown.
5. **Deadlock:** each thread holds something the other needs; break the cycle.

## Related Guides

- [RAII: Deterministic Resource Management in C++]({% post_url cpp-reference/2026-08-02-raii-resource-management %})
- [C++ Container Adapters: stack, queue, and priority_queue]({% post_url cpp-reference/2026-07-21-cpp-container-adapters %})

## Further Reading

- [Microsoft Learn: `<shared_mutex>`](https://learn.microsoft.com/en-us/cpp/standard-library/shared-mutex?view=msvc-170)
- [Microsoft Learn: `<condition_variable>`](https://learn.microsoft.com/en-us/cpp/standard-library/condition-variable?view=msvc-170)
- [C++ Core Guidelines: Concurrency](https://isocpp.github.io/CppCoreGuidelines/CppCoreGuidelines#S-concurrency)
