---
layout: single
comments: true
title: "Inside a WebSocket Server: Event Loops, Connection State, Backpressure, and Scaling"
date: 2026-08-17 00:00:00+0100
description: "A technical examination of how WebSocket servers maintain long-lived connections using TCP sockets, non-blocking I/O, epoll, per-connection state, backpressure, heartbeats, and distributed gateway routing."
tags: [websockets, networking, cpp, linux, distributed-systems, system-design]
categories: ['Distributed Systems Components']
redirect_from:
  - /blog/2022/ws-copy/
---

# 1. Introduction

## The Question Behind a WebSocket Server

An HTTP server can often treat a request as a short-lived unit of work. A connection arrives, the server reads a request, computes a response, sends it, and eventually releases the resources associated with that exchange. HTTP connections may be reused, but the application is still organized around discrete requests.

A WebSocket server has a different responsibility. After the initial HTTP upgrade, the connection may remain open for minutes, hours, or days. Either endpoint may send at any time. The server must remember which connection belongs to which user, accept messages arriving from many sockets, deliver application events to the correct socket, and remove every piece of state when the connection disappears.

The interesting question is therefore not merely how a client calls `new WebSocket(...)`. It is:

> How can one server maintain tens or hundreds of thousands of mostly idle TCP connections without allocating one operating-system thread to every client?

The short answer is that an idle connection does not require a thread to sit and wait for it. Linux keeps the TCP connection state in the kernel. The application keeps a small userspace object for the client and registers the socket with an I/O event facility such as `epoll`. The event-loop thread sleeps until the kernel reports that one or more sockets can make progress.

Conceptually, a live WebSocket connection is:

```text
kernel TCP socket
    + file descriptor
    + event-loop registration
    + userspace connection state
    + input and output buffers
```

No CPU core continuously maintains the connection. Work happens only when a connection is established, bytes arrive, space becomes available for queued output, a timer expires, or the connection closes.

This article develops that model from the socket upward. It uses simplified C++ and Linux `epoll` examples to expose the machinery that production libraries normally hide. It then extends the single-process design across CPU cores, gateway servers, load balancers, and regions.

## What WebSockets Provide

WebSocket is a persistent, bidirectional application protocol commonly carried over TCP. It begins with an HTTP-compatible opening handshake and then allows the client and server to send independently over the established connection.

That gives an application three useful properties:

- The server can send without waiting for a new HTTP request.
- Repeated updates avoid establishing a new connection for each message.
- Messages in both directions share one long-lived session.

The transport is intentionally narrow in scope. WebSocket does not provide durable storage, replay, consumer groups, application acknowledgements, or exactly-once processing.

```text
WebSocket = persistent bidirectional transport
WebSocket != durable message broker
```

If an order update must survive a gateway crash and be replayed after reconnection, another part of the architecture must retain that update. The socket only carries it while the connection is usable.

## Why the Server Becomes Stateful

Suppose a user opens an order-tracking page. The gateway authenticates the connection and records:

```text
user 42 -> connection 817 -> file descriptor 96
```

When the order service later publishes `ORDER_DISPATCHED`, the gateway needs that mapping to find the correct socket. A different gateway cannot send through file descriptor `96`; file descriptors and socket objects are local to the process that owns them.

This makes connection placement part of the system's state. At larger scale, the question becomes:

> Which gateway currently owns the connection for user 42?

That routing problem is the distributed-systems layer built around WebSockets. Before reaching it, we need to understand what a single server actually owns.

---

# 2. What Actually Remains Open

## From a Client to a Process

A simplified connection path looks like this:

<div>
    <center>{% include figure.html path="assets/img/websockets/connection_layers.svg" alt="A WebSocket connection across the client, network, Linux kernel, event loop, and application state" caption="A live WebSocket connection spans kernel state and userspace state; no dedicated thread is required while it is idle." %}</center>
</div>

The client first creates a TCP connection. If the URL uses `wss://`, a TLS session is established over it. The client then sends the WebSocket opening handshake. After the server accepts the upgrade, the same underlying connection remains in use.

For the common HTTP/1.1 upgrade path, the transition is approximately:

```text
TCP connection
    -> optional TLS session
    -> HTTP Upgrade request and response
    -> WebSocket traffic
```

The HTTP upgrade does not create a second TCP connection. It changes how both endpoints interpret subsequent bytes on the existing connection.

## Kernel State

For every established TCP connection, the kernel tracks transport state such as:

- local and remote IP addresses and ports
- TCP sequence and acknowledgement numbers
- retransmission state
- congestion-control state
- receive and send queues
- negotiated TCP options
- connection lifecycle state such as `ESTABLISHED` or `CLOSE_WAIT`

The connection is identified on the network by the protocol and its endpoint tuple:

```text
(protocol, local IP, local port, remote IP, remote port)
```

Thousands of clients can connect to the same server port because their remote endpoints differ.

The kernel holds bytes received from the network in a socket receive buffer until the application calls `recv()`. It holds bytes accepted from the application in a socket send buffer while TCP transmits and acknowledges them. The sizes of these buffers are dynamic and operating-system dependent; treating their configured maxima as committed memory per connection produces misleading capacity estimates.

## The File Descriptor

When the server accepts a connection, Linux returns a file descriptor: a small integer that indexes an entry in the process's descriptor table.

```cpp
int clientFd = ::accept4(
    listenFd,
    nullptr,
    nullptr,
    SOCK_NONBLOCK | SOCK_CLOEXEC
);
```

The descriptor is not the connection itself. It is the process-local handle through which the application refers to the underlying open socket. The kernel object can outlive one descriptor when descriptors are duplicated, and descriptor numbers can be reused after `close()`.

That reuse creates an important correctness rule: a delayed task should not identify a logical connection by file descriptor alone. If descriptor `96` is closed and later reused for a different client, stale work for the old client must not be delivered to the new one. Production systems commonly pair the descriptor with a monotonically increasing connection ID or generation.

## Userspace State

The kernel knows how to transport bytes. It does not know which authenticated user owns a socket, which rooms the user joined, which application messages are partially decoded, or which updates are queued for delivery.

The server keeps that information in a connection object:

```cpp
struct Connection {
    ConnectionId id;
    int fd;

    Phase phase;
    std::optional<UserId> userId;
    std::vector<std::byte> input;
    std::deque<PendingWrite> output;
    std::size_t queuedBytes = 0;

    Clock::time_point lastRead;
    Clock::time_point heartbeatDeadline;
};
```

While the connection is idle, this object and the kernel's TCP state are most of what remains. There is no requirement for a thread to block in `read()` for this particular client.

## The Resource Model

An idle connection is cheap relative to an active one, but it is not free. It consumes some combination of:

```text
one file descriptor
+ kernel TCP state
+ socket-buffer memory
+ optional TLS state
+ userspace Connection object
+ application subscription state
+ event-loop registration
```

The dominant term depends on configuration and workload. A server with small connection objects can still consume substantial memory through queued output. A TLS-heavy gateway may spend more memory per connection on library state. A presence service with thousands of subscriptions per user may spend more outside the socket layer than inside it.

This is why connection capacity must be measured on the deployed server rather than inferred from a single fixed number such as "each socket costs 10 KB."

---

# 3. The Blocking Model

## One Connection, One Blocking Loop

The easiest server to understand accepts a client and gives it a blocking loop:

```cpp
void handleClient(int fd) {
    if (!performWebSocketHandshake(fd)) {
        ::close(fd);
        return;
    }

    while (true) {
        auto message = readWebSocketMessage(fd);
        if (!message) {
            break;
        }

        processMessage(fd, *message);
    }

    ::close(fd);
}
```

In blocking mode, `readWebSocketMessage()` eventually calls `recv()`. If no bytes are available, the calling thread sleeps inside the kernel. This produces a natural programming model: execution for one client resumes when that client sends something.

The problem appears when the server implements concurrency with one operating-system thread per connection:

```cpp
while (true) {
    int fd = ::accept(listenFd, nullptr, nullptr);
    std::thread(handleClient, fd).detach();
}
```

A thread has a stack, scheduler state, thread-local data, and synchronization costs. Large numbers of idle clients produce large numbers of mostly idle threads. The scheduler must still manage them, and broadcasts can wake many of them around the same time.

## The Real Distinction

The important distinction is not between "threaded code" and "event-driven code" at the source level. It is between logical concurrency and operating-system threads.

Coroutines, fibers, and virtual-thread runtimes can present code that looks blocking while multiplexing many logical tasks over a smaller number of kernel threads. Libraries such as Boost.Asio can also express asynchronous operations with callbacks or C++ coroutines. Underneath, a scalable runtime still needs a mechanism such as `epoll`, `kqueue`, or IOCP to discover which sockets can make progress.

The raw event loop is therefore worth understanding even when production code uses a higher-level abstraction.

---

# 4. Non-Blocking Sockets

## Returning Instead of Sleeping

A socket can be placed into non-blocking mode when it is accepted:

```cpp
int fd = ::accept4(
    listenFd,
    nullptr,
    nullptr,
    SOCK_NONBLOCK | SOCK_CLOEXEC
);
```

The same state can be enabled later through `fcntl()`:

```cpp
int flags = ::fcntl(fd, F_GETFL, 0);
if (flags == -1 || ::fcntl(fd, F_SETFL, flags | O_NONBLOCK) == -1) {
    throw std::system_error(errno, std::generic_category());
}
```

With `O_NONBLOCK`, an operation that would otherwise wait normally returns `-1` and sets `errno` to `EAGAIN` or `EWOULDBLOCK`.

```cpp
ssize_t count = ::recv(fd, buffer, capacity, 0);

if (count == -1 &&
    (errno == EAGAIN || errno == EWOULDBLOCK)) {
    // No bytes are available now. Try again after readiness notification.
}
```

The event-loop thread can move on to other connections instead of sleeping on this one.

## Non-Blocking Does Not Mean Complete

Network operations do not preserve the application boundaries that programmers often imagine.

A call to `recv()` may return:

- part of the HTTP upgrade request
- several application messages together
- part of one message
- `0`, meaning the peer performed an orderly shutdown
- `-1` with `EAGAIN`, meaning no more bytes are currently available
- `-1` with another error

A call to `send()` may accept fewer bytes than requested. A successful return value says how many bytes were copied into the socket's send path, not that the remote application received or processed them.

The connection object must therefore retain partial input and partial output across event-loop iterations.

## Why Scanning Does Not Scale

Non-blocking mode by itself does not tell the application which socket is ready. The server could repeatedly call `recv()` on every descriptor, but most calls would return `EAGAIN` when most connections are idle.

```text
for every connection:
    attempt recv()
    usually discover that nothing is available
```

With 200,000 idle sockets, that loop burns CPU rediscovering the absence of work. The server needs the kernel to maintain the readiness set and return only sockets whose state changed or whose operations can currently make progress.

On Linux, that facility is commonly `epoll`.

---

# 5. The `epoll` Reactor

## Interest and Readiness

An `epoll` instance contains two important conceptual sets:

- The **interest set** contains the file descriptions the application asked to monitor.
- The **ready set** contains registered objects that are currently ready for requested I/O.

The application creates an instance and registers the listening socket:

```cpp
constexpr std::uint64_t kListenerToken = 0;

int epollFd = ::epoll_create1(EPOLL_CLOEXEC);
if (epollFd == -1) {
    throw std::system_error(errno, std::generic_category());
}

epoll_event listenerEvent{};
listenerEvent.events = EPOLLIN;
listenerEvent.data.u64 = kListenerToken;

if (::epoll_ctl(
        epollFd,
        EPOLL_CTL_ADD,
        listenFd,
        &listenerEvent) == -1) {
    throw std::system_error(errno, std::generic_category());
}
```

It then waits:

```cpp
std::array<epoll_event, 1024> events;

int ready = ::epoll_wait(
    epollFd,
    events.data(),
    static_cast<int>(events.size()),
    -1
);
```

If no registered object is ready, `epoll_wait()` blocks the event-loop thread. When packets arrive, a connect attempt reaches the listening socket, a send buffer gains space, or an error occurs, the kernel makes the relevant registration ready and wakes the waiter.

The thread sleeps once on the `epoll` instance rather than once per client.

## The Reactor Pattern

The resulting architecture is usually called a reactor:

<div>
    <center>{% include figure.html path="assets/img/websockets/epoll_reactor.svg" alt="Linux epoll reactor accepting sockets and dispatching readable, writable, and timer events to connection objects" caption="The kernel reports readiness; the event loop invokes a short non-blocking handler on the owning connection." %}</center>
</div>

The kernel reports that an operation can probably make progress. The application reacts by invoking the appropriate handler.

```cpp
while (running_) {
    int ready = ::epoll_wait(
        epollFd_,
        events.data(),
        static_cast<int>(events.size()),
        -1
    );

    if (ready == -1) {
        if (errno == EINTR) {
            continue;
        }
        throw std::system_error(errno, std::generic_category());
    }

    for (int i = 0; i < ready; ++i) {
        const epoll_event& event = events[i];

        if (event.data.u64 == kListenerToken) {
            acceptReadyConnections();
            continue;
        }

        dispatchConnectionEvent(event);
    }
}
```

`epoll` does not parse HTTP, decode WebSocket messages, authenticate users, or move bytes between application buffers. It only reports readiness. The application must still call `accept4()`, `recv()`, and `send()` and handle their results correctly.

## Accepting Every Ready Connection

More than one connection may be waiting by the time the listener becomes readable. The accept path should normally continue until `EAGAIN`:

```cpp
void EventLoop::acceptReadyConnections() {
    while (true) {
        int fd = ::accept4(
            listenFd_,
            nullptr,
            nullptr,
            SOCK_NONBLOCK | SOCK_CLOEXEC
        );

        if (fd >= 0) {
            addConnection(fd);
            continue;
        }

        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            return;
        }

        if (errno == EINTR) {
            continue;
        }

        recordAcceptError(errno);
        return;
    }
}
```

The new descriptor is inserted into the connection table and registered for input and peer-shutdown events:

```cpp
void EventLoop::addConnection(int fd) {
    ConnectionId id = nextConnectionId_++;

    auto [it, inserted] = connections_.try_emplace(id, id, fd, *this);
    if (!inserted) {
        ::close(fd);
        return;
    }

    epoll_event event{};
    event.events = EPOLLIN | EPOLLRDHUP;
    event.data.u64 = id.value();

    if (::epoll_ctl(epollFd_, EPOLL_CTL_ADD, fd, &event) == -1) {
        connections_.erase(it);
        ::close(fd);
    }
}
```

The example reserves token zero for the listening socket and stores a connection ID rather than the raw descriptor for client registrations. This allows event dispatch to validate that the logical connection still exists. A production implementation must also ensure that queued events cannot be mistaken for a newly allocated connection with a reused ID.

## Level-Triggered and Edge-Triggered Operation

`epoll` supports two readiness models.

With **level-triggered** behavior, which is the default, a socket remains ready while the condition remains true. If unread data is still available, a later `epoll_wait()` can report the socket again.

With **edge-triggered** behavior through `EPOLLET`, the application is notified when readiness changes. It must drain the non-blocking operation until `EAGAIN`; otherwise data can remain unread without another edge arriving.

```text
level-triggered:
    ready while data remains

edge-triggered:
    notification when state changes to ready
```

Edge-triggered mode can reduce repeated notifications, but it makes handler correctness less forgiving. The examples in this article drain reads and writes to `EAGAIN`, which is safe for edge-triggered operation, but omit `EPOLLET` to keep the initial reactor level-triggered.

## Fairness

Draining one connection indefinitely can starve every other ready connection. A high-volume client may keep producing data faster than the handler consumes it.

Production loops often impose a work budget:

```text
maximum bytes per callback
maximum messages per callback
maximum handler duration
```

If the budget is exhausted before `EAGAIN`, the loop schedules the connection again or relies on level-triggered readiness. Readiness tells the server that work exists; scheduling policy decides how fairly that work is shared.

---

# 6. The Per-Connection State Machine

## More Than an Integer

Once `accept4()` succeeds, the server needs enough state to advance the connection through every partial operation:

```cpp
enum class Phase {
    HttpHandshake,
    Open,
    Closing
};

struct PendingWrite {
    std::shared_ptr<const std::string> bytes;
    std::size_t offset = 0;
};

class Connection {
public:
    Connection(ConnectionId id, int fd, EventLoop& owner)
        : id_(id), fd_(fd), owner_(owner) {}

    void onReadable();
    void onWritable();
    void onPeerShutdown();
    void enqueue(std::shared_ptr<const std::string> bytes);
    void beginClose(CloseReason reason);

private:
    ConnectionId id_;
    int fd_;
    EventLoop& owner_;
    Phase phase_ = Phase::HttpHandshake;

    std::optional<UserId> userId_;
    std::vector<std::byte> input_;
    std::deque<PendingWrite> output_;
    std::size_t queuedBytes_ = 0;

    bool writeInterestEnabled_ = false;
    bool closeAfterFlush_ = false;

    Clock::time_point lastRead_ = Clock::now();
    Clock::time_point heartbeatDeadline_;
};
```

The precise decoder is deliberately hidden here. A compliant WebSocket library must validate the opening handshake, message encoding, protocol control messages, sizes, and closure rules. Those details matter for implementation, but they do not change how the server owns and schedules connections.

## Lifecycle

The server advances each connection through a small state machine:

```text
Accepted
   |
   v
Reading HTTP upgrade ---- invalid/timeout ----> Closed
   |
   v
Open <---- readable / writable / timer events
   |
   v
Closing ---- flush or deadline ----> Closed
```

During the handshake, the input buffer may contain only part of the HTTP headers. After the upgrade, the same buffer may end with part of an application message. During closure, the server may need to flush a close notification before releasing the descriptor.

Every transition should have a bound:

- maximum handshake bytes
- maximum time to complete authentication
- maximum decoded message size
- maximum queued output
- maximum close-handshake duration

Without bounds, a client can hold resources forever or force unbounded memory growth.

## Ownership Is a Concurrency Rule

The event loop that registered the socket should normally own its `Connection` object. Only that loop calls `recv()`, changes output offsets, modifies `epoll` interest, or destroys the connection.

This rule turns many synchronization problems into ordering problems:

```text
socket state is mutated only by owner event loop
other threads submit commands to that event loop
```

If arbitrary worker threads call `send()` and close the same descriptor, the implementation needs locks around lifecycle state, queues, interest flags, and descriptor reuse. Single-owner mutation makes the hot path simpler and preserves the order in which one connection observes commands.

---

# 7. Reading Without Blocking

## Drain Until the Socket Would Block

A readable handler repeatedly calls `recv()` until it consumes the currently available bytes or reaches a work budget:

```cpp
void Connection::onReadable() {
    constexpr std::size_t maxBytesPerTurn = 256 * 1024;
    std::array<std::byte, 16 * 1024> buffer;
    std::size_t bytesThisTurn = 0;

    while (bytesThisTurn < maxBytesPerTurn) {
        ssize_t count = ::recv(
            fd_,
            buffer.data(),
            buffer.size(),
            0
        );

        if (count > 0) {
            lastRead_ = Clock::now();
            bytesThisTurn += static_cast<std::size_t>(count);

            input_.insert(
                input_.end(),
                buffer.begin(),
                buffer.begin() + count
            );

            if (!consumeAvailableInput()) {
                beginClose(CloseReason::ProtocolError);
                return;
            }

            continue;
        }

        if (count == 0) {
            beginClose(CloseReason::PeerClosedTcp);
            return;
        }

        if (errno == EINTR) {
            continue;
        }

        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            return;
        }

        beginClose(CloseReason::ReadError);
        return;
    }
}
```

The handler never waits for the next byte. When `recv()` reaches `EAGAIN`, it returns control to the event loop. Linux will report readiness again when more input is available.

## Parsing Across Calls

TCP provides an ordered byte stream, not application message boundaries. Even if the client performs one send operation, the server may observe the bytes across multiple `recv()` calls. Conversely, a single `recv()` may contain several logical messages.

The input path is therefore incremental:

```text
recv bytes
    -> append to connection input buffer
    -> consume every complete protocol unit
    -> retain incomplete suffix
```

This is the only protocol-structure fact the event-loop architecture needs. The transport decoder can live behind `consumeAvailableInput()`.

## Do Not Block the Reactor

The event loop is shared by many connections. If one callback performs a blocking database query for 200 milliseconds, none of the other connections owned by that loop can process readiness during those 200 milliseconds.

The event-loop thread should perform short operations:

- read or write non-blocking sockets
- update connection-local state
- perform bounded parsing
- enqueue application work
- apply completed work back to the connection

Blocking storage calls, expensive compression, large JSON transformations, and CPU-heavy authorization checks should run elsewhere when they cannot meet the loop's latency budget.

A common flow is:

```text
event loop reads command
    -> submit work to worker pool
    -> return immediately
worker completes
    -> post result to owning event loop
event loop validates connection generation
    -> enqueue result for sending
```

The generation check matters because the client may disconnect while the worker is running.

---

# 8. How the Server Pushes to a Client

## The Connection Registry

After authentication, the gateway registers the relationship between application identity and local connection identity.

```cpp
using UserConnections = std::unordered_set<ConnectionId>;

std::unordered_map<ConnectionId, Connection> connections;
std::unordered_map<UserId, UserConnections> connectionsByUser;
```

The value is often a set rather than one connection because a user can have multiple browser tabs or devices. The application's delivery policy decides whether an event goes to all sessions, one device, or one designated session.

The registry must be updated atomically with respect to the owning event loop's lifecycle decisions:

```text
authenticate -> register
disconnect   -> unregister
```

If cleanup removes only the socket but leaves `connectionsByUser` unchanged, later events target a stale ID. If cleanup removes a user entry created by a newer reconnection, the new connection silently stops receiving events. Generation-aware removal prevents that race.

## From an Application Event to `send()`

Consider an order service publishing an event for user 42. Inside one gateway, the path is:

<div>
    <center>{% include figure.html path="assets/img/websockets/event_delivery.svg" alt="An application event moving through a broker consumer, cross-thread command queue, event loop, connection output queue, and kernel socket" caption="Application threads route work to the event loop that owns the socket; the owning loop alone mutates connection state." %}</center>
</div>

The broker consumer or application worker should not mutate the connection directly. It creates a command for the owning event loop:

```cpp
struct DeliverToUser {
    UserId userId;
    std::shared_ptr<const std::string> encodedMessage;
};

void Gateway::publishToUser(
    UserId userId,
    std::shared_ptr<const std::string> message) {
    EventLoop& owner = loopFor(userId);
    owner.post(DeliverToUser{userId, std::move(message)});
}
```

The loop drains its command queue and performs the lookup locally:

```cpp
void EventLoop::handle(DeliverToUser command) {
    auto userIt = connectionsByUser_.find(command.userId);
    if (userIt == connectionsByUser_.end()) {
        return;
    }

    for (ConnectionId id : userIt->second) {
        auto connectionIt = connections_.find(id);
        if (connectionIt != connections_.end()) {
            connectionIt->second.enqueue(command.encodedMessage);
        }
    }
}
```

Sharing an immutable encoded message avoids copying the same bytes for every local recipient. Each recipient still needs its own write offset because sockets make progress independently.

## Waking the Owner Loop

The event loop may be asleep inside `epoll_wait()` when another thread posts a command. A Linux `eventfd` can wake it.

```cpp
int wakeFd = ::eventfd(
    0,
    EFD_NONBLOCK | EFD_CLOEXEC
);
```

The loop registers `wakeFd` with `epoll`. A producer pushes a command into a thread-safe queue and increments the event counter:

```cpp
void EventLoop::post(Command command) {
    commandQueue_.push(std::move(command));

    std::uint64_t increment = 1;
    ssize_t ignored = ::write(
        wakeFd_,
        &increment,
        sizeof(increment)
    );
    (void)ignored;
}
```

When `wakeFd` becomes readable, the owner drains the counter and then drains queued commands. A production implementation must handle queue publication ordering, a saturated event counter, shutdown, and redundant wakeups carefully. The architectural point is that cross-thread work becomes another event handled by the same reactor.

## Queue First, Write When Ready

`enqueue()` does not assume that the socket can accept the whole message immediately:

```cpp
void Connection::enqueue(
    std::shared_ptr<const std::string> bytes) {
    owner_.assertInEventLoop();

    if (queuedBytes_ + bytes->size() > maxQueuedBytes_) {
        beginClose(CloseReason::SlowConsumer);
        return;
    }

    queuedBytes_ += bytes->size();
    output_.push_back(PendingWrite{std::move(bytes), 0});

    if (!writeInterestEnabled_) {
        writeInterestEnabled_ = true;
        owner_.updateInterest(*this, EPOLLIN | EPOLLOUT | EPOLLRDHUP);
    }
}
```

Enabling `EPOLLOUT` asks the kernel to report when the socket has send-buffer capacity. The event loop can also attempt an immediate write before enabling interest, but it must retain unsent bytes and fall back to readiness notification.

---

# 9. Partial Writes and Backpressure

## A Writable Socket Has Finite Capacity

When `epoll` reports `EPOLLOUT`, the application may call `send()`. The result can still be smaller than the requested length.

```cpp
void Connection::onWritable() {
    constexpr std::size_t maxBytesPerTurn = 256 * 1024;
    std::size_t bytesThisTurn = 0;

    while (!output_.empty() && bytesThisTurn < maxBytesPerTurn) {
        PendingWrite& pending = output_.front();
        const std::string& bytes = *pending.bytes;

        ssize_t count = ::send(
            fd_,
            bytes.data() + pending.offset,
            bytes.size() - pending.offset,
            MSG_NOSIGNAL
        );

        if (count > 0) {
            std::size_t sent = static_cast<std::size_t>(count);
            pending.offset += sent;
            queuedBytes_ -= sent;
            bytesThisTurn += sent;

            if (pending.offset == bytes.size()) {
                output_.pop_front();
            }
            continue;
        }

        if (count == -1 && errno == EINTR) {
            continue;
        }

        if (count == -1 &&
            (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return;
        }

        beginClose(CloseReason::WriteError);
        return;
    }

    if (output_.empty() && writeInterestEnabled_) {
        writeInterestEnabled_ = false;
        owner_.updateInterest(*this, EPOLLIN | EPOLLRDHUP);

        if (closeAfterFlush_) {
            owner_.destroy(id_);
        }
    }
}
```

`MSG_NOSIGNAL` prevents a closed peer from terminating the process through `SIGPIPE`; the error is returned to the caller instead. An application can alternatively ignore `SIGPIPE` process-wide, depending on its design.

Writable interest should normally be disabled when the queue becomes empty. Stream sockets are often writable during healthy operation. Leaving `EPOLLOUT` permanently enabled can cause the loop to wake repeatedly even though the application has nothing to send.

## The Slow-Consumer Problem

Suppose the application produces 100 KB/s for a client whose network can currently accept only 10 KB/s. The missing 90 KB/s must accumulate somewhere:

```text
application output queue
    -> process memory
kernel send queue
    -> kernel memory
network
    -> slow client
```

TCP backpressure eventually prevents more bytes from entering the kernel send buffer. It does not choose what the application should do with newly generated events. If the userspace queue is unbounded, one slow client can consume memory until the gateway is killed.

For `C` equally slow clients with an average of `Q` queued bytes:

```text
queued payload memory ~= C * Q
```

For example:

```text
200,000 connections * 32 KiB = 6.1 GiB
```

That is only payload memory. It excludes queue nodes, smart-pointer control blocks, allocator fragmentation, connection objects, socket state, and TLS state.

## Backpressure Is a Product Decision

The server needs a bounded policy. Possible actions include:

- **Disconnect:** close clients whose queues exceed a limit and let them resynchronize.
- **Drop:** discard low-value transient updates such as cursor positions.
- **Coalesce:** replace several pending state updates with the newest state.
- **Sample:** deliver only some high-frequency telemetry points.
- **Persist:** retain durable events elsewhere and send only a cursor or notification.
- **Reduce upstream demand:** pause or slow a source when the entire gateway is overloaded.

No single policy fits every message. Dropping an intermediate typing indicator is usually acceptable. Dropping a payment-state transition may not be.

A useful design classifies outbound data:

```text
ephemeral latest-state event -> coalesce or drop
durable business event       -> persist and replay
control event                -> reserve bounded priority capacity
```

The WebSocket transport exposes pressure; the application decides its semantics.

## Global Pressure

Per-connection limits prevent one client from consuming unlimited memory. They do not protect the server when many clients become slow simultaneously.

The gateway also needs global controls:

- total queued bytes per event loop
- total queued bytes per process
- event-loop lag
- memory high-water marks
- broker-consumer pause thresholds
- load-shedding rules

During a regional network degradation, thousands of sockets may remain technically connected while making little write progress. Global pressure is what turns a network incident into an out-of-memory restart unless the gateway sheds work deliberately.

---

# 10. Heartbeats and Dead Connections

## Why Silence Is Ambiguous

An established TCP connection can remain idle without traffic. Silence may mean:

- the user is connected but has nothing to send
- the browser tab is suspended
- a mobile device moved between networks
- a NAT mapping expired
- an intermediary silently discarded state
- packets are being dropped in one direction
- the client process died without a clean close reaching the server

The server does not necessarily receive a TCP `FIN` or `RST` immediately after every failure. Until traffic is exchanged or a timer expires, a dead path may look like an idle connection.

## Protocol Heartbeats and TCP Keepalive

Two mechanisms are often confused.

**TCP keepalive** is implemented by the TCP stack. When enabled and configured, it probes sufficiently idle connections to determine whether the remote TCP endpoint remains reachable. Operating-system defaults are often too slow for an application that needs prompt failure detection.

**WebSocket heartbeat logic** uses protocol ping/pong behavior or application messages. It can answer a stronger application-specific question: is the remote endpoint processing this connection promptly enough for the service?

```text
TCP keepalive:
    can the TCP peer still be reached?

application heartbeat:
    is this session responsive within our deadline?
```

The server may configure TCP keepalive as a lower-level safety net while using a shorter application deadline for operational decisions.

## Deadline Tracking

Creating one heavyweight timer object per connection is not always necessary. A gateway can track deadlines using:

- a min-heap ordered by expiration time
- a hierarchical timer wheel
- buckets of connections checked at coarse intervals
- a library event loop's timer facility

A timer wheel is attractive when hundreds of thousands of connections use similar heartbeat intervals. Connections are placed into buckets representing future time windows. Advancing the wheel processes only the bucket whose deadline is due.

Heartbeat traffic should include jitter. If 500,000 clients connect during a deployment and all heartbeat exactly every 30 seconds from their connection time, they can create periodic traffic bursts.

## Intermediary Timeouts

The effective lifetime of a WebSocket connection is constrained by every intermediary:

```text
client
  -> NAT or mobile carrier
  -> edge proxy
  -> load balancer
  -> ingress proxy
  -> gateway
```

If a load balancer closes connections after 60 seconds of inactivity, a 90-second heartbeat cannot keep them alive. The application heartbeat interval must be shorter than relevant idle timeouts, with enough margin for scheduling and network delay.

Aggressive heartbeats have a cost. For `C` connections, heartbeat size `B`, and interval `T`:

```text
average heartbeat bandwidth ~= C * B / T
```

They also wake radios on mobile clients, consume event-loop work, and can amplify synchronized bursts. Failure-detection speed is an operational tradeoff, not a free setting.

---

# 11. Closing and Cleanup

## Three Different Endings

A connection can end at several layers:

1. The application decides that the session should end.
2. The WebSocket peers perform the protocol closing handshake.
3. The underlying TCP connection closes or resets.

A clean path attempts to send the protocol close indication, waits for the peer or a deadline, shuts down the socket, removes event-loop interest, unregisters application identity, and closes the descriptor.

An error or attack may require immediate teardown.

## Cleanup Must Be Idempotent

Several events can report the same failure:

- `recv()` returns `0`
- `send()` returns `EPIPE` or `ECONNRESET`
- `epoll` reports `EPOLLERR`
- `epoll` reports `EPOLLRDHUP`
- a heartbeat deadline expires
- the application initiates shutdown

Cleanup should tolerate being requested more than once:

```cpp
void EventLoop::destroy(ConnectionId id) {
    auto it = connections_.find(id);
    if (it == connections_.end()) {
        return;
    }

    Connection& connection = it->second;
    unregisterUserConnection(connection);

    ::epoll_ctl(
        epollFd_,
        EPOLL_CTL_DEL,
        connection.fd(),
        nullptr
    );

    ::close(connection.fd());
    connections_.erase(it);
}
```

The actual implementation must be careful not to erase an object while one of its methods is still executing. It may mark the object for deferred destruction at the end of the event-loop turn.

## Descriptor Reuse and Stale Work

Consider this sequence:

```text
1. user A owns fd 96, connection generation 1041
2. database work starts for user A
3. user A disconnects; fd 96 is closed
4. user B connects; kernel assigns fd 96 again
5. database work for user A completes
```

If the completion contains only `fd = 96`, it can send user A's result to user B. The completion should instead contain a logical connection ID or `(fd, generation)` pair, and the owner loop must validate it before delivery.

This is one of the most important reasons not to expose raw descriptors as durable application identity.

## Graceful Deployment

Closing 100,000 connections at once can cause 100,000 clients to reconnect at once. A graceful gateway shutdown typically:

1. stops accepting new connections
2. becomes unavailable for new load-balancer assignments
3. optionally asks clients to reconnect elsewhere
4. allows a drain interval for existing sessions
5. closes remaining connections in controlled batches
6. exits after cleanup or a hard deadline

Clients need exponential backoff and jitter even when servers drain carefully. Gateways will sometimes crash without sending a warning.

---

# 12. Multi-Core Gateway Architecture

## One Loop Eventually Reaches a Limit

One event-loop thread can own many sockets, but all parsing, queue management, registry lookup, TLS work, and system calls performed on that loop still consume one core. Increasing the connection count does not make the core faster.

A common architecture runs one event loop per core or per small CPU set:

<div>
    <center>{% include figure.html path="assets/img/websockets/multicore_gateway.svg" alt="A multi-core WebSocket gateway with separate event loops, connection shards, cross-thread command queues, and worker pool" caption="Each connection has one owning event loop. Cross-thread results are posted back to the owner rather than mutating socket state directly." %}</center>
</div>

Each connection belongs to exactly one loop for its lifetime:

```text
loop 0 owns connections 0, 4, 8, ...
loop 1 owns connections 1, 5, 9, ...
loop 2 owns connections 2, 6, 10, ...
loop 3 owns connections 3, 7, 11, ...
```

The exact distribution can be round-robin, hash-based, load-aware, or determined by which loop accepts the socket.

## Accept Strategies

There are two common shapes.

With a **central acceptor**, one thread accepts sockets and transfers each descriptor to an event loop. This makes distribution policy explicit but introduces a handoff.

With **`SO_REUSEPORT`**, several listening sockets can bind the same address and port, and the kernel distributes incoming connections among them. Each loop accepts directly into its own shard. This removes the central acceptor but gives the kernel more influence over distribution.

Neither choice guarantees equal ongoing work. Two loops with the same connection count can have very different traffic if one owns active market-data subscribers and another owns idle dashboards. Production gateways often monitor both connection count and event-loop utilization.

## Connection Affinity

Keeping a connection on one loop provides:

- no lock on normal connection state
- stable ordering of per-connection commands
- better cache locality
- simpler destruction
- simpler readiness registration

Moving a live connection requires transferring its descriptor, buffered input, queued output, timers, subscriptions, and pending completions without losing order. Most servers avoid that complexity and balance only when accepting or reconnecting clients.

## Worker Pools

A worker pool can handle bounded CPU or blocking work, but it should not become an unbounded escape hatch.

Suppose every inbound command creates a blocking task and the database slows down. The worker queue grows even though socket reads remain fast. Memory pressure simply moves from connection queues to the worker queue.

Useful controls include:

- bounded worker queues
- per-user outstanding-request limits
- deadlines and cancellation
- overload responses
- backpressure on inbound reads
- circuit breakers for failing dependencies

The event loop makes network waiting scalable. It does not make downstream dependencies infinitely scalable.

## Libraries and the Underlying Model

Production C++ systems normally use a mature library rather than implementing a WebSocket codec and TLS state machine from raw system calls. Examples include Boost.Asio with Boost.Beast, libevent, libuv, Folly, Seastar, and uWebSockets.

Their APIs differ, but the ownership model remains recognizable:

```text
async read requested
    -> runtime registers interest
    -> kernel reports progress
    -> runtime invokes completion
    -> connection state advances
```

Higher-level libraries provide buffer management, cancellation, timers, protocol validation, TLS integration, and coroutine support. Understanding the reactor explains what those abstractions are coordinating.

---

# 13. Scaling Beyond One Gateway

## The Gateway Fleet

Once one process or machine is insufficient, clients connect through a load balancer to a fleet:

<div>
    <center>{% include figure.html path="assets/img/websockets/distributed_gateways.svg" alt="Clients connected through a load balancer to multiple WebSocket gateways with a distributed connection directory and event broker" caption="A distributed directory locates the owning gateway; the gateway's local registry then locates the socket." %}</center>
</div>

The load balancer chooses a gateway when the connection is established. After that, the connection remains attached to the chosen gateway until it closes. An event generated elsewhere must reach that exact gateway.

The routing path becomes two-level:

```text
distributed lookup: user -> gateway
local lookup:       user -> connection IDs
```

## The Distributed Connection Directory

A directory entry might contain:

```text
userId:       42
gatewayId:    gateway-eu-17
connectionId: 1041
sessionId:    browser-tab-8
generation:   73
expiresAt:    2026-08-17T12:01:30Z
```

The directory can be maintained in a shared store, a partitioned routing service, broker subscriptions, or a combination. It is usually not perfectly synchronous with socket reality.

For example:

```text
1. gateway A registers user 42
2. gateway A crashes
3. user 42 reconnects to gateway B
4. stale entry for gateway A has not expired
```

Generation numbers, leases, heartbeats, and conditional updates help the system prefer the newest session. Even then, routing should tolerate stale results because process failure and network delay create windows of disagreement.

## Broker Routing Patterns

One naive design makes every gateway consume every event and discard irrelevant ones. It is operationally simple but scales network and broker work with the size of the entire fleet.

Better patterns include:

- one topic or queue per gateway
- a partition computed from the user ID
- a routing service that forwards to the owning gateway
- hierarchical topics for rooms or tenants
- regional brokers with local gateway subscriptions

The right design depends on fanout shape.

For a one-to-one notification:

```text
event -> owning gateway -> user's local connections
```

For a room with 100,000 participants:

```text
event -> gateways with at least one participant
      -> local participants on each gateway
```

The second design avoids sending one broker copy per user while also avoiding broadcast to gateways with no interested connections.

## Sticky Sessions Are Not Event Routing

Load-balancer affinity can make the same reconnecting client more likely to return to the same gateway. It does not let an order service discover where the current live socket resides. It also does not preserve a connection through gateway failure.

Sticky sessions may improve cache locality or simplify local session state, but the event path still needs one of:

- a connection directory
- gateway-specific broker routing
- broadcast with filtering
- an architecture that derives ownership deterministically

Affinity is a placement hint, not a durability or routing guarantee.

## Presence Is Eventually Consistent

Presence often appears boolean:

```text
user is online
user is offline
```

The underlying evidence is not boolean. A user may have several connections, a gateway may have crashed without removing its leases, and one region may not yet know about a session established in another.

A more accurate model is:

```text
online = at least one sufficiently fresh session lease is known
```

Presence displays can tolerate brief staleness. Authorization and financial decisions usually cannot. The system should not silently use eventually consistent presence as a strong security fact.

---

# 14. Delivery Semantics and Reconnection

## What a Successful Send Means

Consider the following chain:

```text
application creates event
    -> gateway queues bytes
    -> send() copies bytes toward kernel
    -> TCP transmits bytes
    -> peer TCP acknowledges bytes
    -> browser runtime reads bytes
    -> application callback runs
    -> application updates durable/local state
```

A successful `send()` covers only an early part of this chain. It does not prove that the browser application processed the event.

If the gateway removes an event from durable storage immediately after `send()` succeeds, a failure later in the chain can lose the event from the user's perspective.

## Reconnection Creates a New Session

When a TCP connection dies, it cannot be resumed by attaching a new socket to the old kernel state. The client creates a new connection, repeats authentication, and reconstructs application subscriptions.

If missing events matter, the application needs a replay position:

```text
client last processed sequence: 1842
server current sequence:         1847
replay required:                 1843..1847
```

The durable event store might retain:

```json
{
  "stream": "orders:user:42",
  "sequence": 1843,
  "eventId": "01J5...",
  "type": "ORDER_DISPATCHED",
  "payload": {}
}
```

The WebSocket gateway transports replayed events in the same way as live events. Durability and cursor management belong to the application or messaging layer.

## Duplicates Are Normal Under Retry

Suppose the client processes event `1843`, but its acknowledgement is lost before reaching the server. On reconnect, the server may replay `1843`. Avoiding loss has created a duplicate.

The client can make processing idempotent by recording the last applied sequence or a set of recent event IDs.

```text
if sequence <= lastApplied:
    ignore duplicate
else if sequence == lastApplied + 1:
    apply and advance
else:
    detect gap and request replay
```

This is application-level at-least-once delivery. Exactly-once effects across a browser, gateway, broker, and database require more than a WebSocket connection and are usually expressed as idempotent state transitions rather than literal one-time packet delivery.

## Reconnect Storms

When a gateway fleet or load balancer fails, many clients detect loss together. Immediate fixed-interval retry produces synchronized load on:

- DNS
- edge proxies
- TLS termination
- accept queues
- authentication services
- connection directories
- subscription stores

Clients should use exponential backoff with random jitter. Servers should cap handshake concurrency, rate-limit abusive sources, and recover capacity gradually. A healthy data path can still fail during recovery if connection establishment overwhelms it.

---

# 15. Network Path and Load Balancing

## Long-Lived Connections Change Load Balancing

For short HTTP requests, a load balancer makes routing decisions frequently. With WebSockets, it commonly chooses a backend once, then forwards traffic for the lifetime of that connection.

This creates several operational consequences:

- adding a new gateway does not move existing connections to it
- removing a gateway requires draining or disconnecting its clients
- connection count can remain uneven long after capacity changes
- requests per second does not describe connection occupancy
- a gateway can be connection-heavy but traffic-light, or the reverse

Balancing only new connections cannot instantly repair historical skew.

## Layer 4 and Layer 7

An L4 load balancer forwards TCP connections without interpreting WebSocket messages. An L7 proxy understands the HTTP handshake and may apply host, path, header, authentication, or rate-limit policies before tunnelling the upgraded connection.

L7 processing provides richer controls but adds protocol configuration, buffering behavior, idle timeouts, and sometimes another TLS boundary. Every component on the path must support long-lived upgraded connections and have compatible timeout policies.

## TLS

With `wss://`, the socket's readiness model remains, but application reads and writes go through a TLS library:

```text
socket readable
    -> TLS state machine consumes encrypted records
    -> decrypted application bytes may become available
```

A TLS operation can require the opposite underlying readiness from the operation the application requested. For example, advancing a TLS read may need to write handshake data. Mature libraries coordinate these state transitions; a raw `recv()`/`send()` loop is not sufficient once TLS is inserted.

TLS also adds per-connection state and handshake CPU. Session resumption can reduce handshake cost, but connection-establishment bursts must still be included in capacity tests.

## Multi-Region Placement

Clients should normally connect to a nearby healthy region because the socket carries latency-sensitive traffic in both directions. Application events may originate elsewhere.

A common shape is:

```text
client connects to nearest region
    -> regional gateway owns socket
global or regional event routing
    -> event reaches owning region
regional broker
    -> event reaches owning gateway
```

During a regional failure, reconnecting clients move to another region and create new directory state. The system must decide whether durable events are globally available, replicated asynchronously, or temporarily unavailable. WebSockets do not answer that data-consistency question.

---

# 16. Security and Resource Protection

## Authenticate the Session

Authentication can happen during the HTTP handshake through cookies or supported headers, through a short-lived connection ticket in the URL, or through the first application exchange after upgrading.

Long-lived sessions create a lifecycle question: what happens when credentials expire or access is revoked after the socket opened? Options include:

- close and require a new authenticated connection
- refresh session state over the existing connection
- periodically revalidate authorization
- allow the connection to remain but authorize every sensitive action independently

Authentication answers who owns the connection. Authorization must still be checked when the user subscribes to a resource or sends a command.

## Bound Every Dimension

An internet-facing gateway should bound at least:

- TCP connections per source and per account
- connection establishment rate
- handshake bytes and handshake duration
- unauthenticated connection lifetime
- inbound message size
- inbound messages per second
- parsing work per event-loop turn
- subscriptions per connection
- outbound bytes queued per connection
- total queued bytes per process
- idle and heartbeat deadlines
- graceful-close duration

These are not only abuse controls. They define the maximum damage from bugs, dependency slowdowns, and unexpected clients.

## Origin Validation

Browsers can attach ambient credentials such as cookies to a WebSocket handshake. A malicious web page may attempt to open a connection to another origin using the victim's browser credentials. Servers that depend on browser cookies should validate the `Origin` header against an allowlist and still use explicit authorization for sensitive actions.

TLS protects bytes in transit. It does not decide whether the initiating web origin should be trusted.

## Slowloris and Handshake Pressure

An attacker can open many TCP connections and send handshake bytes extremely slowly. If the server permits unlimited incomplete handshakes, authenticated users may be crowded out by connections that never become useful.

The handshake phase needs its own:

- short deadline
- byte limit
- connection-rate limit
- global concurrency limit

Because TLS and authentication are more expensive than maintaining an idle established socket, establishment pressure should be observed separately from steady-state connection count.

---

# 17. Capacity Planning

## Connection Capacity

The first approximation is:

```text
process memory ~=
    connections
    * (userspace state
       + average queued output
       + TLS library state
       + allocator overhead)
```

Kernel memory must be measured separately:

```text
kernel memory ~=
    TCP control state
    + allocated receive buffers
    + allocated send buffers
    + epoll registrations
```

Socket buffer autotuning, workload activity, kernel version, TLS library, allocator, and application data structures all affect the result. Load tests should measure resident memory and kernel socket memory at realistic idle and active ratios.

## File Descriptors

The process needs descriptors for more than clients:

```text
required descriptors ~=
    client sockets
    + listening sockets
    + epoll instances
    + eventfd/timer descriptors
    + broker connections
    + database connections
    + logs and other files
    + safety margin
```

The process soft and hard `RLIMIT_NOFILE` values, service-manager limits, and system-wide file-table limits must agree with the target. Raising one limit while leaving another unchanged does not create usable capacity.

## Bandwidth and Fanout

For event rate `R`, average encoded event size `S`, and average number of recipients `F`:

```text
outbound application bandwidth ~= R * S * F
```

Protocol, TLS, TCP, and IP overhead add to this number. Retransmissions add more under packet loss.

Fanout dominates quickly. A 1 KB event published 1,000 times per second to an average of 100 clients produces approximately:

```text
1,000 * 1 KB * 100 = 100 MB/s
```

before transport overhead. Connection count alone says nothing about this cost.

## Establishment Capacity

Steady state and recovery are different workloads.

The server should measure:

- accepted connections per second
- TLS handshakes per second
- authentication requests per second
- directory registrations per second
- subscription restoration per second
- time until a connection becomes ready
- failure rate at each stage

A fleet that comfortably maintains one million sockets may still be unable to recreate them quickly after a coordinated disconnect.

## Event-Loop Health

Useful per-loop measurements include:

- active connections
- callbacks per second
- bytes read and written
- command-queue depth
- queued output bytes
- time spent outside `epoll_wait()`
- scheduling lag for timers
- maximum callback duration
- connections closed as slow consumers

Aggregate CPU can hide a single overloaded loop. If connection affinity prevents migration, one saturated owner loop causes tail latency even while other cores remain available.

---

# 18. End-to-End Example

## Establishing the Connection

Consider a four-core order-tracking gateway.

1. The load balancer selects gateway `eu-17`.
2. One listening socket becomes readable.
3. Event loop 2 calls `accept4()` and receives descriptor `96`.
4. The loop creates connection ID `1041` and registers the descriptor with `epoll`.
5. The client sends the HTTP upgrade over several packets.
6. Readable callbacks accumulate the headers without blocking.
7. The gateway authenticates user 42 and completes the upgrade.
8. Loop 2 registers `(user 42, connection 1041)` locally.
9. The gateway writes a leased directory entry mapping user 42 to `eu-17`.

The connection then becomes idle. It has no dedicated thread; loop 2 returns to `epoll_wait()` along with the other loops.

## Delivering an Update

The order service commits a state transition and publishes durable event sequence `1843`.

1. Event routing reads that user 42 currently belongs to gateway `eu-17`.
2. The event reaches a broker consumer thread in the gateway.
3. The consumer identifies loop 2 as the local owner.
4. It places a `DeliverToUser` command into loop 2's concurrent queue.
5. It writes to loop 2's `eventfd`.
6. `epoll_wait()` returns the wake event.
7. Loop 2 finds connection `1041` and appends the encoded event to its output queue.
8. Loop 2 enables `EPOLLOUT` for descriptor `96`.
9. Linux reports the socket writable.
10. `send()` accepts some or all queued bytes.
11. Loop 2 retains any partial suffix and waits for the next writable event.
12. The client processes sequence `1843` and records its replay cursor.

At no point does the broker consumer write to the socket directly. The connection owner preserves lifecycle safety and output order.

## Failure Between Send and Processing

Now suppose event `1844` reaches the kernel send buffer, but the client's network disappears before the application processes it.

1. The gateway initially sees no error.
2. The heartbeat deadline eventually expires or a later write fails.
3. Loop 2 removes connection `1041`, closes descriptor `96`, and unregisters the local user mapping.
4. The directory lease expires or is explicitly removed.
5. The client reconnects to gateway `eu-09` with last processed sequence `1843`.
6. The application replays events beginning at `1844`.
7. If the client actually processed `1844` before losing connectivity but failed to persist or acknowledge it, its idempotency logic handles the duplicate according to the chosen cursor semantics.

This is the boundary between connection management and delivery semantics. The gateway can maintain and observe a socket correctly without knowing whether an application effect occurred on the other side.

## Gateway Failure

If `eu-17` crashes, its local connection registry disappears with the process. The kernel closes its sockets. Clients eventually reconnect, and directory leases remove the stale placement.

Durable events do not disappear if they were retained by the application or broker. Ephemeral events that existed only in the gateway's output queues are lost.

That distinction should be intentional:

```text
connection state: reconstruct after reconnect
durable business state: retain outside gateway
ephemeral live state: allowed to disappear
```

---

# 19. When WebSockets Are the Wrong Tool

WebSockets are useful when both endpoints send frequently or when server-to-client latency matters enough to justify persistent connection infrastructure. They are not automatically the best answer for every changing page.

## Ordinary HTTP

Use ordinary HTTP when updates are request-driven, infrequent, cacheable, or naturally represented as independent operations. It has the simplest operational model and works naturally with common infrastructure.

## Polling

Polling is reasonable when freshness requirements are loose and update frequency is low. It trades extra requests for simple failure recovery and stateless request handling.

## Long Polling

Long polling lets the server delay an HTTP response until an event or timeout. It can work through conservative infrastructure but repeatedly re-establishes request context and needs careful retry handling.

## Server-Sent Events

Server-Sent Events provide server-to-browser streaming over HTTP with built-in event IDs and browser reconnection behavior. They are attractive when traffic is primarily server-to-client and browser clients are the target. Client-to-server commands continue over ordinary HTTP.

## WebSockets

Choose WebSockets when the application benefits from a persistent bidirectional session and the team is prepared to operate:

- connection ownership
- heartbeat and timeout policy
- backpressure
- graceful draining
- reconnection and replay
- distributed event routing
- per-connection resource limits

The protocol reduces repeated request overhead. It moves complexity into the lifecycle of the connection.

---

# 20. Operational Checklist

Before operating a WebSocket gateway at scale, define:

## Connection Lifecycle

- handshake and authentication deadlines
- idle and heartbeat timeouts
- clean-close and hard-close behavior
- graceful deployment procedure
- reconnect backoff and jitter
- stale-session generation rules

## Resource Bounds

- maximum connections per process and per event loop
- file-descriptor limits with headroom
- maximum input and message size
- maximum queued output per connection
- maximum total queued output
- worker-queue bounds
- subscription limits

## Failure Semantics

- which events may be dropped
- which events are durable
- how clients resume from a cursor
- how duplicates are handled
- what happens when the broker is unavailable
- what happens when the directory is stale
- how a regional failover changes event access

## Observability

- active and opening connections
- connection establishment latency and failures
- normal and abnormal disconnects
- event-loop lag by loop
- input and output bytes
- queue depth and queued bytes
- slow-consumer disconnects
- heartbeat round-trip time and expiry
- broker-to-socket delivery latency
- reconnect and resubscription rate

The failure path should be tested, not inferred. Kill a gateway with live connections, pause its broker consumer, saturate a client receive path, expire directory leases, and restart the fleet in stages. Observe whether bounded queues, reconnection, routing, and replay behave as designed.

---

# 21. Conclusion

A WebSocket server does not maintain each connection by assigning a thread to wait beside it. Linux maintains the TCP transport state. The application holds a file descriptor and a small amount of connection-specific state. `epoll` lets one event-loop thread sleep until the kernel reports that one of many sockets can make progress.

The essential single-process model is:

```text
non-blocking sockets
    + readiness notification
    + one owner per connection
    + bounded input and output state
```

The essential distributed model is:

```text
user or subscription
    -> owning gateway
    -> owning event loop
    -> local connection object
    -> kernel socket
```

Everything difficult follows from preserving that path under change. Clients become slow. File descriptors are reused. worker tasks finish after disconnects. Gateways crash. Directory entries become stale. Load balancers drain. Entire client populations reconnect together.

WebSockets solve the bidirectional transport problem. A production WebSocket service must additionally solve ownership, pressure, failure detection, routing, recovery, and delivery semantics. Understanding the socket and event loop makes those larger architectural decisions much easier to reason about.

---

# References

1. IETF, [RFC 6455: The WebSocket Protocol](https://www.rfc-editor.org/rfc/rfc6455)
2. Linux man-pages, [`epoll(7)`](https://man7.org/linux/man-pages/man7/epoll.7.html)
3. Linux man-pages, [`epoll_ctl(2)`](https://man7.org/linux/man-pages/man2/epoll_ctl.2.html)
4. Linux man-pages, [`accept(2)` and `accept4(2)`](https://man7.org/linux/man-pages/man2/accept.2.html)
5. Linux man-pages, [`socket(7)`](https://man7.org/linux/man-pages/man7/socket.7.html)
6. Linux man-pages, [`tcp(7)`](https://man7.org/linux/man-pages/man7/tcp.7.html)
7. Linux man-pages, [`eventfd(2)`](https://man7.org/linux/man-pages/man2/eventfd.2.html)
8. Linux man-pages, [`send(2)`](https://man7.org/linux/man-pages/man2/send.2.html)
9. Boost.Beast, [WebSocket documentation and asynchronous examples](https://www.boost.org/doc/libs/latest/libs/beast/doc/html/beast/using_websocket.html)
