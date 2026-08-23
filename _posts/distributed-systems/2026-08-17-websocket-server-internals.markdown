---
layout: single
comments: true
title: "Inside a WebSocket Server: Connections, Event Loops, Backpressure, and Scaling"
date: 2026-08-17 00:00:00+0100
description: "A first-principles guide to WebSockets: the browser API, opening handshake, messages and frames, closing, reconnection, and the server machinery that supports many live connections."
tags: [websockets, networking, cpp, linux, distributed-systems, system-design]
categories: ['Distributed Systems Components']
redirect_from:
  - /blog/2022/ws-copy/
---

# 1. Why WebSockets Exist

Most web interactions begin with the client asking for something. A browser
sends an HTTP request, the server returns a response, and the exchange ends.
That model is a good fit for loading a page, submitting a form, or fetching a
record.

Some applications need the server to speak first.

Consider an order-tracking page. The browser loads the current order state, but
the next change may originate minutes later in a warehouse service:

```text
PLACED -> PACKED -> DISPATCHED -> DELIVERED
```

The browser could poll every few seconds. Most requests would return no change,
and increasing the polling interval would make the page feel stale. The same
problem appears in chat, collaborative editing, multiplayer games, live market
data, and operational dashboards.

A **WebSocket** is an application protocol for exchanging messages over a
persistent, bidirectional connection. Persistent means the connection remains
available after one message. Bidirectional means either endpoint can initiate
the next message. The **client** is the endpoint that opens the connection; the
**server** is the endpoint that accepts it.

After one opening handshake, either side can send without first creating a new
HTTP request-response exchange.

```text
ordinary HTTP
    client request -> server response

WebSocket after opening
    client <-------- messages --------> server
```

That is the problem WebSockets solve: low-latency, two-way communication over a
long-lived connection.

They solve only the communication problem. WebSocket does not provide durable
storage, event replay, application acknowledgements, user presence, or
exactly-once processing. If an order update must survive a server crash and be
replayed after the client reconnects, another part of the system must retain
it.

```text
WebSocket = persistent bidirectional transport
WebSocket != durable message broker
```

## The Path From Browser to Gateway

For an order update to reach the page without a new HTTP request, the browser
and server establish the following chain:

```text
new WebSocket(url)
    -> DNS, TCP, and TLS
    -> HTTP WebSocket upgrade
    -> text/binary messages and WebSocket frames
    -> ping, pong, and close
    -> listening and connected sockets
    -> file descriptor and Connection object
    -> epoll readiness and event-loop callbacks
    -> application routing and output queues
    -> reconnect, replay, and gateway-fleet routing
```

The browser exposes this chain as one `WebSocket` object. Behind it, TCP keeps
an ordered byte stream, TLS protects that stream, and WebSocket adds message
boundaries and control frames. On the server, an accepted socket is associated
with protocol state, an authenticated session, subscriptions, timers, and
queued output.

Most connections are idle most of the time. Their state remains in memory
while an event loop waits for useful work instead of assigning a waiting
thread to every client. When the order service produces an update, application
routing finds the correct live connection and its owner sends the corresponding
WebSocket message.

If the network changes or a gateway fails, that transport connection cannot be
moved or repaired. The client opens a new one, and the application restores
identity, subscriptions, and missed events from state that lives outside the
WebSocket.

> **What to remember:** WebSocket solves long-lived, two-way message transport.
> It does not by itself make messages durable or reconstruct a session after a
> connection fails.

---

# 2. From a URL to an Open WebSocket

Suppose the order page runs:

```javascript
const socket = new WebSocket("wss://orders.example.com/live");

socket.addEventListener("open", () => {
    console.log("order stream is ready");
});

socket.addEventListener("message", event => {
    renderOrderUpdate(JSON.parse(event.data));
});

socket.addEventListener("close", event => {
    scheduleReconnect(event.code);
});
```

`orders.example.com` is an intentionally fictional hostname used throughout
the explanation; this snippet will not connect as written. To run it, replace
the URL with a WebSocket endpoint you control. The later C++ fragments expose
individual server mechanisms and fit together as one design, but they omit the
TLS library, HTTP parser, WebSocket codec, error plumbing, and build setup
required for a production server.

The constructor returns a JavaScript object immediately while connection setup
continues asynchronously. Its `readyState` moves through four values:

| Browser state | Numeric value | Meaning |
|---|---:|---|
| `CONNECTING` | `0` | DNS, TCP, TLS, or the WebSocket handshake is still in progress. |
| `OPEN` | `1` | The opening handshake succeeded and messages can be exchanged. |
| `CLOSING` | `2` | The closing handshake has started. |
| `CLOSED` | `3` | The connection closed or never opened successfully. |

The browser reports lifecycle through `open`, `message`, `error`, and `close`
events. Calling `send()` while the object is still `CONNECTING` throws an
error; application code normally waits for `open`.

This one constructor starts several protocols underneath the object.
Understanding their order is the foundation for everything the server does
later.

The **kernel** is the privileged core of the operating system. Among other
jobs, it owns network interfaces, the TCP implementation, sockets, and their
buffers. Browser and gateway code run in **userspace** and ask the kernel to
perform network operations through system calls.

The layers are not interchangeable:

| Term | Direct definition |
|---|---|
| DNS | Translates a service name such as `orders.example.com` into network addresses. |
| IP | Moves packets between network interfaces identified by IP addresses. |
| TCP | Creates one reliable, ordered byte stream between two endpoints. |
| TLS | Encrypts that byte stream and authenticates the server. |
| HTTP upgrade | Negotiates changing the application protocol on the existing connection. |
| WebSocket | Frames text, binary data, and control signals over the established stream. |

## Step 1: Find the Service

The URL contains four useful instructions:

```text
wss                       use WebSocket over TLS
orders.example.com        find this service through DNS
443                       use the default wss destination port
/live                     request this WebSocket endpoint
```

**DNS** is the naming system the client uses to obtain one or more IP addresses
for `orders.example.com`. It commonly returns an edge proxy or load balancer—a
front server that accepts or forwards connections—rather than the gateway
process that ultimately handles WebSocket messages.

Several hostnames can share the same edge address. TLS **Server Name
Indication (SNI)** carries the requested hostname during TLS setup. Together
with the later HTTP `Host` header and request path, it helps the edge select the
intended certificate and service.

The URL identifies where and how to start connecting. It does not uniquely
identify the resulting connection: one browser can open several WebSockets to
the same URL.

## Step 2: Establish TCP and TLS

The client asks its operating system to create a **TCP connection** to the
selected address and port. A TCP connection is a kernel-maintained, reliable,
ordered byte stream between two endpoints. TCP handles sequence numbers,
acknowledgements, retransmission, congestion control, and flow control.

Sequence numbers let TCP restore byte order. Retransmission replaces detected
missing data. Flow control prevents a fast sender from overwhelming the
receiver's buffers; congestion control reduces sending pressure when the
network path appears overloaded.

Because the scheme is `wss://`, the endpoints next perform a **TLS handshake**
over that TCP connection. TLS establishes encryption keys, authenticates the
server certificate, and protects subsequent bytes from observation or
modification in transit.

At this point there is still no open WebSocket. There is an encrypted TCP
channel on which the client can attempt the WebSocket opening handshake.

## Step 3: Upgrade the Connection

The connection starts with HTTP because HTTP already provides the web-facing
setup mechanisms the service needs: host and path routing, cookies and other
authentication information, browser `Origin`, and integration with proxies and
load balancers that support WebSocket upgrades. The server can inspect those
details and accept or reject the connection before creating a long-lived
WebSocket session.

HTTP's normal protocol is still request followed by response. It does not let
either endpoint subsequently send a WebSocket frame whenever it wants. Client
and server therefore need an explicit point at which they agree to stop parsing
ordinary HTTP messages and start parsing WebSocket frames.

That agreement is the **HTTP upgrade**. It does not replace TCP, create a second
connection, or add encryption. It changes the application protocol spoken over
the TCP and TLS connection that already exists:

```text
before 101 response   interpret application bytes as HTTP
after 101 response    interpret application bytes as WebSocket frames
```

Without this agreement, one side could send WebSocket frames while the other
continued waiting for an HTTP request, causing the same bytes to be interpreted
under different rules.

For the common HTTP/1.1 WebSocket form, the client makes that request as:

```http
GET /live HTTP/1.1
Host: orders.example.com
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: <random nonce>
Sec-WebSocket-Version: 13
Origin: https://www.example.com
Sec-WebSocket-Protocol: orders.v1
```

The server validates the request, selects the `/live` endpoint, applies origin
and authentication policy, and returns:

```http
HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Accept: <value derived from the client's nonce>
Sec-WebSocket-Protocol: orders.v1
```

The WebSocket-specific headers have separate purposes:

| Header | Purpose |
|---|---|
| `Upgrade: websocket` | Request or confirm the WebSocket protocol transition. |
| `Connection: Upgrade` | Mark `Upgrade` as applying to this connection. |
| `Sec-WebSocket-Version: 13` | State the protocol version defined by RFC 6455. |
| `Sec-WebSocket-Key` | Supply a fresh browser nonce for handshake verification. |
| `Sec-WebSocket-Accept` | Prove the server processed that nonce as a WebSocket server. |
| `Origin` | Tell the server which web origin initiated the browser connection. |
| `Sec-WebSocket-Protocol` | Negotiate one application-level subprotocol. |

The server computes `Sec-WebSocket-Accept` by appending the protocol's fixed
GUID to the client's key, hashing the result with SHA-1, and Base64-encoding the
hash:

```text
accept = Base64(SHA-1(clientKey + WebSocketGUID))
```

The `Sec-WebSocket-Key` proves that the server understood the WebSocket
handshake. It is not a password, session token, or encryption key. User
identity comes from a separate mechanism such as a cookie, a short-lived
connection ticket, or an authenticated first application message.

This constraint matters in browsers: the standard `WebSocket` constructor
accepts a URL and optional subprotocol names, but no arbitrary request-header
map. Browser applications therefore cannot simply attach a custom
`Authorization` header as they might with `fetch()`.

Common browser authentication patterns include:

- an existing secure, same-site cookie sent with the handshake
- a short-lived, single-use connection ticket in the URL
- an authentication message sent immediately after `open`

Long-lived bearer tokens in URLs are risky because URLs can appear in logs and
monitoring systems. Cookie-based servers validate `Origin` because a malicious
page may otherwise ask the victim's browser to open a credentialed WebSocket.

Before sending `101`, the server can reject the request as ordinary HTTP—for
example with `401`, `403`, or `404`. Browser JavaScript generally receives an
`error` and eventual `close`, not unrestricted access to the failed handshake
response.

## Subprotocols and Extensions

WebSocket defines transport messages but not the meaning of an order message.
A **subprotocol** names an application protocol layered on top. The browser can
offer one or more names:

```javascript
const socket = new WebSocket(
    "wss://orders.example.com/live",
    ["orders.v2", "orders.v1"]
);
```

The request lists those names in `Sec-WebSocket-Protocol`. The server selects
at most one offered value and returns it in the response. After opening,
`socket.protocol` contains the selected name. This prevents client and server
from silently speaking incompatible application formats.

An **extension** changes how WebSocket itself operates. Extensions are offered
and accepted through `Sec-WebSocket-Extensions`; the browser exposes the
negotiated result through `socket.extensions`. A common example is
`permessage-deflate`, which compresses message data. Compression reduces
bandwidth but adds CPU, memory, latency, and security considerations, so the
gateway includes it in capacity testing rather than treating it as free.

Status `101 Switching Protocols` is the server's confirmation. Only after the
browser validates that response does the WebSocket become `OPEN`. The complete
stack is now:

```text
TCP connection
    -> TLS session
    -> HTTP Upgrade request and response
    -> WebSocket frames
```

## If the `open` Event Never Fires

The browser deliberately exposes little detail about a failed handshake to
JavaScript, but its developer tools and the server logs can locate the failed
phase. Check in this order:

| Observation | Likely area to inspect |
|---|---|
| No network request appears | Invalid URL, page code did not run, or browser policy blocked it. |
| DNS or connection error | Hostname, port, firewall, listener, or load-balancer routing. |
| TLS or certificate error | Certificate name, trust chain, expiry, or TLS configuration. |
| HTTP response is not `101` | Path, authentication, `Origin`, upgrade headers, or proxy configuration. |
| Server selects no offered protocol | Client and server subprotocol lists do not overlap. |
| `101` succeeds, then immediate close | Invalid first frame, failed first-message authentication, or application policy. |

An HTTPS page should normally use `wss://`; browsers block insecure active
content in secure pages. At a reverse proxy, both the `Upgrade` and
connection-upgrade intent must reach the upstream. On the server, log a
handshake request ID, rejection reason, selected subprotocol, and eventual
Close code without logging credentials or URL tokens.

## Step 4: Exchange Messages

After the `101` response, HTTP parsing stops on this connection. Both endpoints
now interpret the following bytes as WebSocket frames.

The browser API presents **messages**. The wire protocol transports those
messages in one or more **frames**:

```javascript
socket.send(JSON.stringify({ type: "subscribe", orderId: "A123" }));
```

Conceptually, that call follows this path:

```text
JavaScript string
    -> one WebSocket text message
    -> one or more masked client frames
    -> TLS records
    -> TCP byte stream
```

A WebSocket message is either **text** or **binary**. Text must be valid UTF-8.
Binary is an uninterpreted sequence of bytes; the application decides whether
those bytes contain an image, Protocol Buffers, MessagePack, or something else.
In a browser, incoming text is delivered as a string. Incoming binary is a
`Blob` by default, or an `ArrayBuffer` after setting:

```javascript
socket.binaryType = "arraybuffer";
```

### A Frame Header Gives Bytes Meaning

Each frame begins with a compact header. The important fields are:

| Field | What it tells the receiver |
|---|---|
| `FIN` | Whether this is the final frame of the current message. |
| `RSV1–3` | Extension bits; zero unless a negotiated extension defines them. |
| `opcode` | Whether the frame starts text/binary data, continues a message, or carries a control signal. |
| `MASK` | Whether a four-byte masking key is present. |
| payload length | How many payload bytes follow; larger lengths use extra header bytes. |
| masking key | Four bytes used to unmask client-to-server payload. |
| payload | Message data or control-frame data. |

The first two header bytes therefore answer most immediate parsing questions:
is the message finished, what kind of frame is this, is it masked, and how is
its length encoded? The decoder may still need more bytes before the full
header or payload is available.

<div>
    <center>{% include figure.html path="assets/img/websockets/websocket-frame.svg" alt="WebSocket frame showing FIN, RSV, opcode, mask, payload length, optional extended length, masking key, and payload" caption="A frame adds boundaries and type information to TCP's byte stream. The payload length determines how many more bytes the decoder must collect." %}</center>
</div>

The defined opcodes are small but important:

| Opcode | Frame type | Role |
|---:|---|---|
| `0x0` | Continuation | Continue a fragmented text or binary message. |
| `0x1` | Text | Start a UTF-8 text message. |
| `0x2` | Binary | Start a binary message. |
| `0x8` | Close | Begin or acknowledge the closing handshake. |
| `0x9` | Ping | Ask the peer for a Pong. |
| `0xA` | Pong | Reply to a Ping, carrying the same application data. |

Other opcodes are either reserved for future use or invalid unless an
extension defines them.

### Messages Can Span Frames

**Fragmentation** lets one message be divided across frames. A large text
message might arrive as:

```text
text frame, FIN=0          starts the message
continuation, FIN=0        adds more payload
continuation, FIN=1        finishes the message
```

The browser normally hides those boundaries: its `message` event fires after
the complete message has been reassembled. A server decoder, however, must
remember the message type and accumulated data across reads and frames. It
must also enforce a maximum message size while accumulating; otherwise a peer
can consume unbounded memory without ever setting `FIN`.

A Ping, Pong, or Close frame may appear between fragments. These **control
frames** must be handled immediately rather than appended to the data message.
Control frames are never fragmented and their payload is at most 125 bytes.

### Reads Do Not Match Frames

TCP preserves byte order but not WebSocket boundaries. One `recv()` can return:

```text
half of a frame header
one complete frame plus part of the next
several complete frames
```

The server therefore appends received bytes to an input buffer and runs an
incremental decoder. The decoder reads a header only when enough header bytes
exist, waits for the stated payload length, validates the frame, and then
continues with any bytes left in the buffer. Section 6 implements this process.

WebSocket messages from one endpoint arrive in their sent order because they
share one ordered TCP stream. WebSocket does not provide named channels or
message priorities within that stream. A very large message can delay a small
urgent message behind it, so applications that need independent traffic
classes may use separate connections or a carefully designed subprotocol.

### Client Frames Are Masked, Not Encrypted

Every frame sent by a client to a server must have `MASK=1` and use a newly
chosen unpredictable four-byte key. The payload byte at position `i` is XORed
with key byte `i mod 4`. The server reverses the same operation before
interpreting the payload. Frames sent by a server to a client are not masked.

Masking prevents client-controlled bytes from looking like another protocol
to broken intermediaries. It does **not** make the contents secret: the key is
inside the frame. `wss://` and TLS provide confidentiality and integrity.

### Ping, Pong, and Application Heartbeats

Ping and Pong test whether the WebSocket path is responsive. When an endpoint
receives a Ping, it sends a Pong with the same payload. A server can measure
the time between its Ping and the matching Pong and close a connection that
misses a deadline.

Browser JavaScript cannot construct protocol Ping or Pong frames. The browser
handles incoming Ping and the required Pong internally. If browser code needs
to observe liveness itself, the application defines ordinary messages such as
`{"type":"heartbeat"}` and `{"type":"heartbeat_ack"}`. Section 8 explains
why heartbeats are needed even when a socket appears open.

### Closing Is a Protocol Exchange

`socket.close(code, reason)` sends a Close control frame and moves the browser
to `CLOSING`. The peer normally replies with its own Close frame, after which
the TCP connection is closed. The code communicates a broad reason:

| Code | Typical meaning |
|---:|---|
| `1000` | Normal completion. |
| `1001` | Endpoint is going away, such as a page navigation or server drain. |
| `1002` | The peer violated the WebSocket protocol. |
| `1008` | Application policy violation. |
| `1009` | Message is too large. |
| `1011` | Server hit an unexpected condition. |

Not every valid wire-level code can be supplied to the browser's `close()`
method. Browser JavaScript may specify `1000` or an application code from
`3000` through `4999`; servers and browser internals can use other defined
protocol codes such as `1001` or `1002`.

Code `1006` is special: APIs use it to report that the connection ended
abnormally without receiving a Close frame. An endpoint never sends `1006` in
a frame. The browser's `close` event also exposes `reason` and `wasClean`, but
applications should still treat reconnect and state recovery as explicit
work.

### `send()` Can Queue Data

An open socket is not proof that the network can currently keep up. Browser
`send()` queues data and returns; it does not wait for the peer to receive it.
`socket.bufferedAmount` reports application bytes queued by prior `send()`
calls that have not yet been passed to the network. It excludes WebSocket
framing overhead and buffering deeper in the operating system or network.

The browser API has no general `drain` event. A producer that may outrun the
connection must check `bufferedAmount`, pace or coalesce updates, and impose a
limit instead of building an unlimited queue. The server needs the same policy
for its own output queues, as Section 7 shows.

This distinction matters to the server:

```text
TCP                    ordered bytes, no message boundaries
WebSocket               frames and text/binary messages
order-tracking protocol event types, sequence numbers, payloads
```

The network transports bytes. The WebSocket layer validates frames and
reconstructs messages. The order application decides what those messages mean
and whether they require acknowledgement, storage, replay, or deduplication.

<div>
    <center>{% include figure.html path="assets/img/websockets/websocket-opening.svg" alt="The sequence from a browser WebSocket URL through DNS, TCP, TLS, HTTP upgrade, and open WebSocket frames" caption="Each phase builds on the previous one. The HTTP upgrade changes the application protocol without replacing the underlying TCP connection." %}</center>
</div>

> **What to remember:** `new WebSocket(...)` asynchronously builds WebSocket
> framing on top of TCP and usually TLS. The browser exposes complete text or
> binary messages; the wire carries frames, plus Ping, Pong, and Close control
> frames. `send()` queues data—it does not prove delivery.

---

# 3. The Minimum Networking Model: How Connections Stay Separate

Now that a WebSocket has a place in the stack, we can answer a practical
question: if many applications use port `443`, how do their packets reach the
right process and socket?

## Server Ports and Client Ports Have Different Roles

An **IP address** identifies a network interface at the IP layer. A **port** is
a 16-bit TCP or UDP endpoint number on a host. A **socket** is the kernel object
that holds the communication endpoint and its protocol state.

The server binds a **listening socket** to a well-known address and port. A
listening socket waits for new TCP connection attempts; it does not represent
one authenticated WebSocket client. Production `wss://` traffic commonly
arrives on port `443`.

The client normally does not listen on a famous port. When it creates an
outbound connection, its operating system selects a temporary, or ephemeral,
source port.

One laptop might therefore have:

```text
browser:      10.0.0.7:53001 -> 203.0.113.40:443
market app:   10.0.0.7:53002 -> 203.0.113.40:443
market app:   10.0.0.7:53003 -> 203.0.113.40:443
chat app:     10.0.0.7:53001 -> 192.0.2.25:443
```

The chat connection can reuse source port `53001` because its destination is
different. TCP does not identify a connection by one port. A connection is
defined by its two endpoints; for packet demultiplexing it is commonly written
as:

```text
(protocol, source IP, source port, destination IP, destination port)
```

Here, `protocol` means TCP at the IP layer, not WebSocket. WebSocket frames are
inside the TCP byte stream and, for `wss://`, inside TLS as well.

The client kernel uses the complete endpoint pair to find the correct socket.
That socket belongs to a process, which accesses it through a local handle.
Packets do not contain the application name or authenticated user ID.

<div>
    <center>{% include figure.html path="assets/img/websockets/connection-identity.svg" alt="Three applications using ephemeral client ports through a NAT to separate accepted sockets on one server port" caption="The client kernel, NAT, and server each maintain the mapping needed at their layer. A port by itself is never a user or connection identity." %}</center>
</div>

## One Listener, Many Connected Sockets

The listening socket is not used to carry every client's WebSocket messages.
It receives new TCP connection attempts. After the TCP handshake completes,
`accept()` returns a new connected socket for one client while leaving the
listener available for later clients.

```text
listening socket on :443
    |
    +-- accept() -> client A socket -> fd 96
    +-- accept() -> client B socket -> fd 97
    +-- accept() -> client C socket -> fd 98
```

All three connected sockets can have local port `443`; their remote endpoints
differ. The server's kernel therefore knows which received bytes belong in
which socket buffer.

`accept()` is a TCP operation. It does not authenticate the user or complete
the WebSocket upgrade. Those steps happen later on the accepted socket.

The four server operations have direct meanings:

```text
socket()   create a kernel socket and return its file descriptor
bind()     assign the socket a local address and port
listen()   mark it as a listener and create connection queues
accept()   take one completed connection and return a new connected socket
```

In abbreviated C++, listener setup looks like:

```cpp
int listenFd = ::socket(
    AF_INET,
    SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC,
    0
);

::bind(listenFd, serverAddress, serverAddressLength);
::listen(listenFd, SOMAXCONN);

// Later, when a completed connection is queued:
int clientFd = ::accept4(
    listenFd,
    nullptr,
    nullptr,
    SOCK_NONBLOCK | SOCK_CLOEXEC
);
```

`listenFd` continues to represent the service endpoint. `clientFd` represents
one accepted TCP connection. Real code checks every return value and normally
sets options such as address reuse before `bind()`.

In the flags above, `AF_INET` selects IPv4, `SOCK_STREAM` selects byte-stream
semantics used by TCP, `SOCK_NONBLOCK` enables non-blocking operations, and
`SOCK_CLOEXEC` prevents an unrelated program launched with `exec()` from
inheriting the descriptor. `SOMAXCONN` asks Linux for its configured maximum
listener backlog—the queue limit for completed connections waiting to be
accepted.

## NAT and Proxies Change What Each Hop Sees

**Network Address Translation (NAT)** rewrites address and often port fields
while retaining a table that can reverse the translation for reply traffic. A
home router or carrier NAT can translate the browser's private endpoint:

```text
inside:   10.0.0.7:53001
outside:  198.51.100.8:62001
target:   203.0.113.40:443
```

The client kernel sees the inside endpoint. The Internet-facing peer sees the
outside endpoint. The NAT keeps a table so reply traffic returns to the correct
device and socket. That is how many devices can share one public IPv4 address.

A Layer 4, or **L4**, load balancer works mainly with IP and TCP information and
may translate addresses while forwarding a flow. A Layer 7, or **L7**, proxy
understands an application protocol such as HTTP. It can terminate the client
connection and open another connection to the gateway:

```text
client <== TCP connection A ==> proxy <== TCP connection B ==> gateway
```

The gateway may therefore see the proxy as its TCP peer. A trusted proxy can
forward the original address for logging or rate limiting, but that address is
not socket identity and must not be treated as authenticated user identity.

## A Network Change Creates a New Connection

If the laptop moves from Wi-Fi to a mobile network, its source address and NAT
mapping change. Ordinary TCP cannot attach the new address to the established
endpoint pair. The old connection is no longer a usable path.

Neither endpoint necessarily discovers the failure immediately. A socket can
still appear `ESTABLISHED` while packets disappear into the old path. A failed
write, TCP timeout, keepalive probe, or WebSocket heartbeat eventually exposes
the failure.

The client then creates a new TCP connection, negotiates TLS again, repeats the
WebSocket handshake, authenticates, and restores application state. This is a
new transport connection even if the operating system happens to reuse a port
number.

Similarly, changing a DNS record does not move an established connection. DNS
selects an endpoint when a connection is created; a new answer affects a later
connection or reconnect.

## Do Not Collapse the Identity Layers

The running example has several identifiers because each layer answers a
different question:

| Layer | Example | What it identifies | Survives reconnect? |
|---|---|---|---|
| Service | `wss://orders.example.com/live` | Where a new client should connect | Usually |
| TCP endpoint pair | `198.51.100.8:62001 <-> 203.0.113.40:443` | One connection at one TCP hop | No |
| File descriptor | `96` | A handle inside one process | No |
| Connection ID | `1041` | One logical socket lifetime in the gateway | No |
| Session and user | `tab-8`, user 42 | Authenticated application identity | Can |

The network routes packets using addresses. The kernel demultiplexes TCP using
endpoint pairs. The process uses a file descriptor. The gateway uses a
generation-safe connection ID. The order service routes an update using user
or subscription identity.

That separation is the mental model for the rest of the article.

> **Scope:** The implementation below models the common HTTP/1.1 upgrade case,
> where one WebSocket uses one TCP connection. HTTP/2 extended `CONNECT` can
> carry several logical WebSockets as separate streams over one TCP connection.
> In that model, the stream ID adds another demultiplexing layer, but the same
> ownership and backpressure principles still apply.

> **What to remember:** A server port is a meeting point for new connections,
> not a unique connection ID. The kernel distinguishes established TCP
> connections by their complete endpoint pairs and gives the process a separate
> descriptor for each accepted socket.

---

# 4. What a WebSocket Server Keeps Open

Once the upgrade succeeds, no thread needs to execute continuously to keep the
connection alive. State remains in the kernel and in the gateway process.

This is the first important separation between **state** and **execution**. A
connection can continue to exist while no CPU instruction is currently being
executed for it.

## Kernel State

For every TCP connection, the kernel tracks information such as:

- local and remote addresses and ports
- TCP sequence and acknowledgement numbers
- retransmission and congestion-control state
- receive and send buffers
- negotiated TCP options
- lifecycle state such as `ESTABLISHED` or `CLOSE_WAIT`

Received bytes remain in the socket receive buffer until the application reads
them. Bytes accepted from the application remain in the send path while TCP
transmits and acknowledges them.

For the running connection, an incoming packet follows this path:

```text
network interface receives packet
    -> kernel IP and TCP code identifies the endpoint pair
    -> TCP validates ordering and sequence numbers
    -> payload is appended to this socket's receive buffer
    -> socket becomes readable
    -> gateway can copy the available bytes with recv()
```

The kernel does not parse those bytes as JSON or associate them with user 42.
That work belongs to the gateway process after the bytes are read.

## The File Descriptor

Linux exposes the accepted socket to the process as a file descriptor:

```cpp
int clientFd = ::accept4(
    listenFd,
    nullptr,
    nullptr,
    SOCK_NONBLOCK | SOCK_CLOEXEC
);
```

The descriptor is not the network identity or the user. It is a small integer
index in one process's descriptor table. Another process can also have an
unrelated descriptor `96`, and Linux can reuse `96` after this socket closes.

Delayed work must therefore not identify a logical connection by descriptor
alone. A gateway assigns a monotonically increasing connection ID or generation
and validates it before applying asynchronous results.

## The Userspace Connection Object

The kernel transports bytes but knows nothing about users, subscriptions,
partially decoded WebSocket messages, or application delivery policy. The
gateway keeps that state in a connection object:

```cpp
struct Connection {
    ConnectionId id;                  // 1041
    int fd;                           // 96, valid only in this process
    Phase phase;                      // handshake, open, closing
    std::optional<UserId> userId;     // 42 after authentication

    InputBuffer input;
    std::deque<PendingWrite> output;
    std::size_t queuedBytes = 0;

    TimePoint lastActivity;
    TimePoint heartbeatDeadline;
};
```

An idle connection therefore consumes some combination of:

```text
one file descriptor
+ kernel TCP state and socket buffers
+ optional TLS state
+ one userspace Connection object
+ subscriptions and timers
+ event-loop registration
```

Idle is cheaper than active, but not free. Output queued for a slow client can
cost much more memory than the base connection object, so real capacity must be
measured under a realistic workload.

<div>
    <center>{% include figure.html path="assets/img/websockets/connection_layers.svg" alt="A WebSocket connection across the client, network, Linux kernel, event loop, and application state" caption="After opening, the idle connection consists mainly of kernel transport state and userspace protocol and application state; no dedicated thread runs beside it." %}</center>
</div>

> **What to remember:** The kernel owns TCP transport state and byte buffers.
> The gateway owns protocol parsing, authenticated identity, subscriptions,
> timers, and delivery policy. An idle connection retains both kinds of state
> without requiring a dedicated running thread.

---

# 5. How One Server Waits for Many WebSockets

An **event loop** is a thread that repeatedly waits for events and invokes the
short handlers responsible for them. It is a scheduling structure, not a
separate network protocol.

The problem in this section is waiting. A gateway may own 100,000 connections,
while only a few hundred have work at a given instant. How does one thread find
those few without checking every socket continuously?

The first-pass answer is short:

```text
make sockets non-blocking
    -> ask Linux to report which sockets may make progress
    -> handle only those sockets
    -> return to waiting
```

On Linux, the reporting mechanism is `epoll`. The next two subsections explain
why blocking I/O is replaced by non-blocking I/O. That model is enough to
continue to the connection lifecycle in Section 6. Readers who do not need the
system-call details can then skip the subsection explicitly marked as an
optional deep dive.

## The Blocking Model

A **blocking operation** suspends its calling thread until it can make progress.
With TLS and detailed error handling omitted, the simplest server gives every
connection a blocking loop:

```cpp
void handleClient(int fd) {
    performWebSocketHandshake(fd);

    while (auto message = readWebSocketMessage(fd)) {
        processMessage(fd, *message);
    }

    ::close(fd);
}
```

When no bytes are available, the thread sleeps inside `recv()`. This is easy to
reason about for one client: execution resumes when that client sends data.

The expensive version is one operating-system thread per connection. Every
thread needs stack and scheduler state even though most spend nearly all their
time asleep. A burst can also wake thousands of threads and make the scheduler
move among them.

The scalable goal is not to eliminate threads. It is to multiplex many logical
connections over a much smaller number of threads.

## What Non-Blocking Mode Changes

A **non-blocking socket** makes an operation return instead of suspending the
thread when it cannot make progress immediately. Linux reports that case with
`EAGAIN` or `EWOULDBLOCK`:

```cpp
ssize_t count = ::recv(fd, buffer, capacity, 0);

if (count == -1 &&
    (errno == EAGAIN || errno == EWOULDBLOCK)) {
    // Reading now would block. Let another connection run.
}
```

The important `recv()` outcomes are:

| Result | Meaning |
|---|---|
| `count > 0` | This many bytes were copied from the kernel receive buffer. |
| `count == 0` | The peer performed an orderly TCP shutdown. |
| `EAGAIN` | No bytes can be read without waiting right now. |
| another error | The read path failed and the connection usually closes. |

Non-blocking mode prevents one idle socket from stopping the thread. It does
not tell the thread which socket has data.

The server could scan every descriptor:

```text
for each of 100,000 connections:
    call recv()
    usually receive EAGAIN
repeat
```

That spends CPU repeatedly proving that idle sockets are still idle. The kernel
already knows when bytes enter a socket buffer, so the process needs a way to
ask the kernel for only the sockets whose state may allow progress.

## Optional Deep Dive: the `epoll` API and Its Semantics

`epoll` is a Linux readiness-notification interface. It lets a process register
file descriptors and block one thread until the kernel reports that one or more
registered objects may be ready for I/O.

The receptionist analogy captures only the scheduling idea: the event loop does
not call every room; it receives the list that currently needs attention. From
this point onward, the real actors are file descriptors, kernel socket buffers,
an `epoll` ready list, and an event-loop thread.

`epoll` has three core operations:

| Operation | Direct meaning |
|---|---|
| `epoll_create1()` | Create an empty kernel `epoll` instance and return its descriptor. |
| `epoll_ctl()` | Add, modify, or remove a watched descriptor. |
| `epoll_wait()` | Sleep until registrations are ready, then copy their event records to userspace. |

The `epoll` descriptor is itself a process-local file descriptor. It represents
the kernel object that owns the watch set; it is not a client connection.

### Registering Interest

The server first creates an `epoll` instance and registers its listening
socket:

```cpp
constexpr std::uint64_t kListenerToken = 0;

int epollFd = ::epoll_create1(EPOLL_CLOEXEC);

epoll_event listenerEvent{};
listenerEvent.events = EPOLLIN;
listenerEvent.data.u64 = kListenerToken;

::epoll_ctl(
    epollFd,
    EPOLL_CTL_ADD,
    listenFd,
    &listenerEvent
);
```

Read this code as:

```text
create an epoll watch set
    -> add listenFd
    -> report when accepting may make progress
    -> return token 0 with that event
```

When client fd `96` is accepted, the gateway assigns connection ID `1041` and
registers the socket separately:

```cpp
epoll_event clientEvent{};
clientEvent.events = EPOLLIN | EPOLLRDHUP;
clientEvent.data.u64 = 1041;

::epoll_ctl(
    epollFd,
    EPOLL_CTL_ADD,
    96,
    &clientEvent
);
```

The most relevant flags are:

| Flag | What the event means |
|---|---|
| `EPOLLIN` | Reading may make progress without blocking. |
| `EPOLLOUT` | Writing may make progress without blocking. |
| `EPOLLRDHUP` | The stream peer closed its writing side or closed the connection. |
| `EPOLLERR` | An error is pending on the underlying object. |
| `EPOLLHUP` | The object has been hung up. |

`EPOLLOUT` is normally enabled only while the application has queued output.
A healthy stream socket is often writable, so watching it permanently can
produce events even when the application has nothing to send. `EPOLLERR` and
`EPOLLHUP` are reported when present even if the application did not explicitly
request them.

The `data.u64` field is opaque application data returned with the event. Using
connection ID `1041` rather than fd `96` lets dispatch validate the logical
connection generation.

### Interest Set and Ready Set

Suppose the process currently owns:

```text
fd 4   listening socket
fd 8   eventfd used to wake the loop
fd 96  connection 1041 — idle
fd 97  connection 1042 — received bytes
fd 98  connection 1043 — has queued output
```

Its conceptual `epoll` state might be:

```text
interest set
    fd 4  -> EPOLLIN
    fd 8  -> EPOLLIN
    fd 96 -> EPOLLIN | EPOLLRDHUP
    fd 97 -> EPOLLIN | EPOLLRDHUP
    fd 98 -> EPOLLIN | EPOLLOUT | EPOLLRDHUP

ready now
    fd 97 -> EPOLLIN
    fd 98 -> EPOLLOUT
```

The **interest set** describes what the process wants to hear about. The
**ready set** contains registrations whose current state satisfies that
interest. `epoll_wait()` returns records from the ready set, not every watched
descriptor.

### What Happens When a Packet Arrives

For connection `1042`, the path is conceptually:

```text
1. A network packet arrives at the machine.
2. The kernel's IP and TCP code matches its endpoint pair to fd 97's socket.
3. TCP validates and orders the payload, then appends bytes to the receive queue.
4. The socket changes from not-readable to readable.
5. Its epoll registration becomes ready.
6. The kernel wakes a thread sleeping in epoll_wait().
7. epoll_wait() returns { connectionId: 1042, events: EPOLLIN }.
8. The event loop finds Connection 1042 and calls its readable handler.
9. The handler calls recv() until EAGAIN or its work budget is exhausted.
```

`epoll` does not copy WebSocket payload into the application's input buffer.
It only reports readiness. The later `recv()` performs the byte copy, and the
WebSocket decoder interprets those bytes.

<div>
    <center>{% include figure.html path="assets/img/websockets/epoll_reactor.svg" alt="Linux epoll reactor accepting sockets and dispatching readable, writable, and command events" caption="The interest set records what the process watches. The ready set contains only registrations that may currently make progress." %}</center>
</div>

### Waiting and Dispatching

The event loop repeatedly waits and dispatches short handlers:

```cpp
while (running) {
    int count = ::epoll_wait(
        epollFd,
        events.data(),
        static_cast<int>(events.size()),
        -1  // wait indefinitely; timers and eventfd can wake the loop
    );

    if (count == -1) {
        if (errno == EINTR) {
            continue;
        }
        throw std::system_error(errno, std::generic_category());
    }

    for (int i = 0; i < count; ++i) {
        const epoll_event& event = events[i];

        if (event.data.u64 == kListenerToken) {
            acceptReadyConnections();
        } else {
            dispatchConnectionEvent(event);
        }
    }
}
```

`EINTR` means a signal interrupted the wait before it produced a normal result;
the loop can retry. Other errors indicate that the wait itself failed.

A timeout of `-1` means wait indefinitely. Production loops also register a
timer source or calculate a finite timeout so heartbeat and shutdown deadlines
can wake the loop.

### Readiness Is Not Completion

An `EPOLLIN` event does not mean that a complete WebSocket message is waiting.
It means a read may currently make progress. The available bytes could contain
half an HTTP header, one WebSocket frame header, several messages, or a peer
shutdown indication.

Likewise, `EPOLLOUT` does not mean the remote browser received anything. It
means the local socket may currently accept some bytes into its send path.

Readiness can also change between notification and the system call. Handlers
must still process the actual result from `accept4()`, `recv()`, or `send()` and
must always tolerate `EAGAIN`.

### Accept Until `EAGAIN`

One listener notification can correspond to several queued connections. The
handler accepts until none can be obtained without waiting:

```cpp
void EventLoop::acceptReadyConnections() {
    while (true) {
        int fd = ::accept4(
            listenFd_, nullptr, nullptr,
            SOCK_NONBLOCK | SOCK_CLOEXEC
        );

        if (fd >= 0) {
            addConnection(fd);
        } else if (errno == EINTR) {
            continue;
        } else if (errno == EAGAIN || errno == EWOULDBLOCK) {
            return;
        } else {
            recordAcceptError(errno);
            return;
        }
    }
}
```

`addConnection()` assigns a connection ID, stores the `Connection`, and
registers the descriptor. The listener remains registered for future clients.

### Level-Triggered and Edge-Triggered Readiness

By default, `epoll` is **level-triggered**. As long as unread bytes remain, the
socket continues to satisfy readable interest and can be returned by a later
`epoll_wait()`:

```text
level-triggered
    unread bytes remain -> socket remains readable -> another event can arrive
```

With `EPOLLET`, readiness is **edge-triggered**. The application is notified
when the state changes to ready. It must drain the non-blocking operation until
`EAGAIN`; leaving bytes unread may not produce another readiness transition:

```text
edge-triggered
    not ready -> ready -> one edge
    handler must drain until EAGAIN
```

Edge-triggered operation can reduce repeated notifications, but it makes missed
drains more dangerous. A level-triggered loop is easier to introduce and debug.
The handlers in this article drain operations but assume the default
level-triggered registration unless stated otherwise.

### Ownership and Fairness

One event loop should normally own a connection for its entire lifetime. Only
that loop reads its socket, advances parsing state, changes write interest, and
destroys it. Other threads submit commands to the owner instead of mutating the
connection directly. This turns many locking problems into ordered events.

Draining forever would create a different problem: one busy connection could
starve all other ready connections. Production loops impose a budget such as a
maximum number of bytes, messages, or microseconds per callback. If the budget
is exhausted before `EAGAIN`, level-triggered readiness can return the socket
again on a later turn.

> **What to remember:** Non-blocking mode prevents one socket operation from
> suspending the thread. `epoll` tells that thread which registered descriptors
> may currently make progress. The event loop still performs the actual
> `accept4()`, `recv()`, and `send()` operations and must handle their real
> results.

---

# 6. The Server-Side Lifecycle of One Connection

An accepted socket is not immediately ready for application messages. It moves
through a series of partial, failure-prone phases.

A **state machine** represents this lifecycle as a current phase plus the
events that are allowed to move it to another phase. The connection object
stores the phase because one callback may receive only enough bytes to make
part of a transition.

These are gateway phases, not the four browser `readyState` values from
Section 2. In particular, the browser becomes `OPEN` when it accepts the
`101` response. The application may still restrict that WebSocket to one
authentication message before allowing normal subscriptions or commands.

The phases have direct meanings:

| Phase | What the gateway is doing |
|---|---|
| Accepted | The TCP socket exists, but higher-level setup is incomplete. |
| TLS handshake | Negotiate encryption when TLS terminates at this gateway. |
| Opening handshake | Validate HTTP headers, origin, path, and any cookie or ticket authentication; then send `101`. |
| WebSocket open | The WebSocket protocol is active; optionally allow only a first authentication message. |
| Application ready | Identity and permissions are established, so normal application messages may flow. |
| Closing | Exchange close frames or wait for a bounded flush deadline. |
| Closed | Remove all registrations and release the socket and buffers. |

<div>
    <center>{% include figure.html path="assets/img/websockets/connection-lifecycle.svg" alt="Server-side WebSocket lifecycle from accepted through TLS, opening handshake, protocol open, application ready, closing, and closed, with failure exits" caption="Protocol-open and application-ready are distinct when authentication uses the first WebSocket message. Invalid input, denial, timeout, or transport failure converges on one cleanup path." %}</center>
</div>

Cookie or ticket authentication can finish during the opening handshake. With
first-message authentication, the connection enters WebSocket-open but not
application-ready; only the authentication message is accepted until identity
is established. This is one gateway state machine supporting the alternatives
introduced in Section 2.

Every setup phase needs both a byte bound and a deadline. Otherwise a client
can occupy a socket forever by sending a TLS handshake, HTTP header, or
authentication message one byte at a time.

Useful limits include:

- maximum TLS and upgrade duration
- maximum HTTP header bytes
- maximum unauthenticated lifetime
- maximum WebSocket frame and message size
- maximum work per event-loop turn
- maximum queued output
- maximum close-handshake duration

## Incremental Reads

An **input buffer** is userspace memory that retains bytes read from the socket
but not yet consumed by the current protocol decoder. A **decoder** examines
that buffer and reports one of three outcomes: a complete protocol unit,
incomplete input, or invalid input.

TCP exposes an ordered stream, not HTTP, WebSocket-frame, or application-message
boundaries. For example, the upgrade can arrive as:

```text
first recv():   "GET /live HTTP/1.1\r\nHost: orders.exa"
second recv():  "mple.com\r\nUpgrade: websocket\r\n...\r\n\r\n"
```

The first call is successful even though it does not contain a complete HTTP
request. The gateway appends those bytes to the input buffer and waits. After
the second call, the HTTP decoder finds the terminating blank line and can
validate the complete upgrade.

The same rule applies after opening. A 200-byte WebSocket frame might arrive as
60 bytes, then 140 bytes; several small frames might arrive in one read.

A readable handler therefore drains currently available bytes, feeds them to
the decoder, and retains any incomplete suffix:

```text
socket becomes readable
    -> read until EAGAIN or work budget
    -> feed bytes into TLS if this gateway terminates TLS
    -> feed decrypted bytes into HTTP or WebSocket decoder
    -> emit every complete application message
    -> retain incomplete protocol state in Connection
```

The handler never waits for the next byte. It returns to the event loop, and
Linux reports the socket again when more input arrives.

An abbreviated plaintext read loop makes that contract explicit:

```cpp
void Connection::onReadable() {
    constexpr std::size_t kBudget = 256 * 1024;
    std::array<std::byte, 16 * 1024> chunk;
    std::size_t consumedThisTurn = 0;

    while (consumedThisTurn < kBudget) {
        ssize_t count = ::recv(fd_, chunk.data(), chunk.size(), 0);

        if (count > 0) {
            input_.append(chunk.data(), static_cast<std::size_t>(count));
            consumedThisTurn += static_cast<std::size_t>(count);

            if (!decodeEveryCompleteUnit()) {
                beginClose(CloseReason::ProtocolError);
                return;
            }
        } else if (count == 0) {
            beginClose(CloseReason::PeerClosedTcp);
            return;
        } else if (errno == EINTR) {
            continue;
        } else if (errno == EAGAIN || errno == EWOULDBLOCK) {
            return;
        } else {
            beginClose(CloseReason::ReadError);
            return;
        }
    }
}
```

`decodeEveryCompleteUnit()` changes meaning with the phase: TLS records during
TLS setup, HTTP headers during upgrade, and WebSocket frames while open. A
production TLS library owns its own encrypted buffers and non-blocking state,
but the outer rule remains “advance until more socket readiness is required.”

Blocking database queries, expensive compression, and large transformations do
not belong on this shared loop. They run in a bounded worker pool and post their
result back to the owning loop. The completion includes connection `1041`, not
only fd `96`, because the client may disconnect before the work finishes.

## Partial Writes

An **output queue** retains encoded bytes that the application wants to send but
the socket has not yet accepted. Each queued item stores both its immutable
bytes and the offset of the first unsent byte.

Writing has the same incremental shape as reading. Suppose one encoded message
contains 1,000 bytes:

```text
send(fd, bytes[0..999]) returns 400
    -> bytes 0..399 entered the local send path
    -> queue retains offset 400

later EPOLLOUT
send(fd, bytes[400..999]) returns 600
    -> queued item is complete and can be removed
```

A successful `send()` returns the number of bytes accepted into the local
kernel send path. It does not prove that TCP transmitted them, that the peer
acknowledged them, or that the browser application processed them.

If only part of a message can be written, the connection records an offset and
enables writable interest:

```text
output queue has data
    -> attempt non-blocking write
    -> complete: remove written item
    -> partial: retain offset and wait for EPOLLOUT
    -> EAGAIN: wait for EPOLLOUT
    -> error: begin cleanup
```

Writable interest is disabled when the queue becomes empty. Healthy TCP
sockets are frequently writable, so leaving `EPOLLOUT` enabled with no pending
data can wake the loop continuously.

The relevant `send()` outcomes mirror `recv()`:

| Result | Meaning |
|---|---|
| `count > 0` | Advance this connection's write offset by `count`. |
| `EAGAIN` | Keep the unsent suffix and wait for `EPOLLOUT`. |
| `EINTR` | Retry the interrupted system call. |
| another error | Stop writing and begin connection cleanup. |

When the gateway terminates TLS, raw `recv()` and `send()` are replaced by the
TLS library's non-blocking operations. A TLS read can require the socket to
become writable and a TLS write can require it to become readable, so the TLS
state machine determines the next readiness interest.

## Closing and Descriptor Reuse

A clean WebSocket close tries to exchange protocol close frames before closing
TCP. If the server initiates normal shutdown, it queues a Close frame, stops
accepting new data messages, waits only up to a configured deadline for the
peer's Close, and then closes TCP. If the peer initiates, the server validates
the code and UTF-8 reason, replies with a Close if it has not already sent one,
and finishes the transport shutdown. Once a Close frame has been sent, neither
endpoint sends further data frames.

Errors and attacks may require immediate teardown. In either case cleanup
must be idempotent because the same failure can appear through a read result, a
write error, `EPOLLERR`, a heartbeat deadline, and an application command.

Cleanup removes the exact connection generation from user and subscription
registries, removes `epoll` interest, closes the descriptor, releases buffers,
and destroys the connection object.

Consider why the generation matters:

```text
1. user A owns fd 96 as connection 1041
2. work starts for connection 1041
3. user A disconnects; fd 96 is closed
4. user B connects; Linux reuses fd 96 for connection 1042
5. user A's work completes
```

A completion addressed only to fd `96` could leak user A's result to user B.
A completion addressed to connection `1041` is rejected because that generation
no longer exists.

> **What to remember:** Network reads and writes are incremental. A readiness
> event permits an attempt; the system-call result states what actually
> happened. The `Connection` object preserves the protocol phase, incomplete
> input, unsent output, and generation across event-loop turns.

---

# 7. From an Application Event to the Client

Reading client messages is only half the job. The defining feature of the
order-tracking connection is that an event created elsewhere can reach the
browser without a new request.

## The Local Connection Registry

A **connection registry** is an application data structure that maps stable
application identities—users, devices, rooms, or subscriptions—to the local
connections that should receive their events. It is not a kernel table and is
not derived from IP addresses.

After authentication, the gateway records which connections belong to the
user. The value is a collection because user 42 may have several tabs or
devices, potentially owned by different event loops:

```text
user 42
    -> loop 2, connection 1041
    -> loop 5, connection 2088
```

The application decides whether an update goes to every session, one device,
or one designated session. IP addresses and ports play no role in that policy.

The registry entry includes the loop because socket ownership is also a
concurrency rule. A broker-consumer or worker thread must not call `send()` on
fd `96` directly while loop 2 can simultaneously change its queue or close it.
Instead, the producer creates a command:

```cpp
struct DeliverToConnection {
    ConnectionId connectionId;
    std::shared_ptr<const std::string> encodedMessage;
};

loops[2].post(DeliverToConnection{
    .connectionId = ConnectionId{1041},
    .encodedMessage = orderDispatchedBytes
});
```

Loop 2 later validates that connection `1041` still exists and appends the
message to that connection's output queue. Sharing immutable encoded bytes
avoids copying the same payload for every local recipient, while each
connection retains its own write offset.

When the order service publishes `ORDER_DISPATCHED`, the local path is:

```text
application event for user 42
    -> find user 42's local connection routes
    -> post a command to each owning event loop
    -> validate that each ConnectionId still exists
    -> append encoded bytes to its output queue
    -> write when the socket can make progress
```

<div>
    <center>{% include figure.html path="assets/img/websockets/event_delivery.svg" alt="An application event moving through a broker consumer, command queue, event loop, connection output queue, and kernel socket" caption="Application threads route work to the loop that owns the socket. The owning loop alone mutates connection state and write offsets." %}</center>
</div>

### Optional Detail: Waking the Owning Loop

If another thread posts a command while the loop sleeps in `epoll_wait()`, a
Linux `eventfd` can wake it. An **eventfd** is a kernel counter exposed through
a file descriptor. Writing an integer increments the counter and makes the
descriptor readable; reading returns the counter and clears or reduces it.

The event loop registers that descriptor with `epoll`:

```text
producer thread
    -> push DeliverToConnection into thread-safe command queue
    -> write 1 to loop 2's eventfd

kernel
    -> eventfd becomes readable
    -> epoll_wait() wakes with EPOLLIN for fd 8

loop 2
    -> read eventfd counter
    -> drain command queue
    -> enqueue bytes on connection 1041
```

The counter does not contain the command. It is only the wake-up signal; the
thread-safe queue contains the actual work. This converts cross-thread work into
another event handled by the same socket owner.

## Backpressure

**Backpressure** is the condition in which a downstream stage accepts work more
slowly than an upstream stage produces it. The pressure first appears as
growing buffers or failed/partial non-blocking writes.

Suppose the gateway produces 100 KB/s for a client whose network currently
accepts 10 KB/s. The missing 90 KB/s must accumulate somewhere:

```text
application output queue
    -> kernel send buffer
    -> network
    -> client
```

TCP eventually stops accepting more bytes into its send buffer. That is
**transport backpressure**: the receiver's advertised TCP window, congestion
control, or local send-buffer capacity prevents the sender from continuing at
the desired rate. TCP does not decide what the application should do with new
order, presence, or telemetry events that have not entered the socket.

An unbounded userspace queue converts one slow client into a memory leak. For
`C` slow clients with `Q` queued bytes each:

```text
queued payload memory ~= C * Q

200,000 clients * 32 KiB ~= 6.1 GiB
```

That excludes queue nodes, allocators, connection objects, TLS state, and
kernel buffers.

The correct policy depends on the data:

- **Disconnect** a client that exceeds a hard limit and let it resynchronize.
- **Drop** transient updates such as old cursor positions.
- **Coalesce** many state changes into the newest value.
- **Sample** high-frequency telemetry.
- **Persist and replay** business events that must not disappear.
- **Slow the source** when the entire gateway is overloaded.

Per-connection limits contain one slow consumer. Process-wide and per-loop
limits protect against thousands of clients becoming slow together during a
network incident. Useful signals include total queued bytes, event-loop lag,
memory high-water marks, broker-consumer lag, and slow-consumer disconnects.

The browser sees a smaller version of the same pressure through
`socket.bufferedAmount`. The server can react to `EPOLLOUT` when its local send
path has space; browser JavaScript has no equivalent general-purpose writable
or drain event. Browser producers therefore sample `bufferedAmount`, pace work,
and stop enqueueing above an application-defined threshold.

Backpressure is not merely a networking setting. It is a product decision about
which information may be delayed, combined, dropped, or replayed.

> **What to remember:** The connection registry answers which local socket
> should receive an application event. The owning event loop performs the
> mutation and write. If that socket cannot keep up, bounded application policy—not
> TCP alone—decides what happens to newly produced events.

---

# 8. Silence, Failure, and Reconnection

An idle TCP connection sends no application data. Silence can mean the user is
healthy and inactive, or it can mean the browser was suspended, a NAT mapping
expired, the network changed, an intermediary discarded state, or the client
process died.

The server may not receive a prompt TCP `FIN` or `RST`. It needs an explicit
failure-detection policy.

A TCP **FIN** announces an orderly shutdown: the peer will send no more bytes.
A TCP **RST** aborts the connection immediately. Both are useful signals when
they arrive, but a broken network path may deliver neither one.

## Heartbeats

Section 2 described the Ping and Pong frames themselves. The server now needs
a policy for when to send them and how long to wait for a response.

A **heartbeat** is periodic traffic whose missing response lets the application
declare the path unhealthy after a deadline. WebSocket defines ping and pong
control frames: an endpoint can send ping, and a compliant peer responds with
pong. Some applications instead exchange their own heartbeat messages so they
can include session or timing information.

In a browser, JavaScript cannot send protocol Ping frames or receive a `ping`
event. The browser handles protocol Ping/Pong internally. A server may send
Ping and observe Pong; browser code that needs its own timer uses ordinary
application messages and requires the server to answer them.

TCP keepalive probes whether the remote TCP endpoint remains reachable, but
operating-system defaults are often much slower than an application needs.
WebSocket ping/pong or application heartbeats can ask a stronger question: is
this session responding within the service deadline?

```text
TCP keepalive          can the transport peer still be reached?
application heartbeat is this session responsive enough for the product?
```

Every intermediary has its own idle timeout:

```text
client -> NAT/carrier -> edge -> load balancer -> gateway
```

The heartbeat interval must be shorter than the smallest relevant timeout,
with scheduling and network margin. More aggressive heartbeats detect failure
faster but consume bandwidth, event-loop work, and mobile radio power. Large
fleets add jitter so clients do not all ping at the same instant.

For example, if an edge proxy closes connections after 60 seconds of complete
inactivity, a 90-second ping interval cannot keep the path alive. A service
might choose a 25-second interval with per-connection random jitter and declare
failure only after several missed deadlines. The exact values come from the
actual intermediary timeouts and the product's failure-detection requirement.

## Reconnection Rebuilds Application Continuity

When TCP dies, a new socket cannot inherit its kernel state. The client creates
a new connection and repeats discovery, TCP, TLS, WebSocket upgrade, and
authentication. It also restores subscriptions.

A **reconnect** creates a new transport connection. A **resume** reconstructs
the logical application session on that connection. They are separate actions.

The browser's `WebSocket` object does not perform this recovery automatically.
Application code or a client library owns reconnect backoff, authentication,
subscription restoration, and replay state.

If missed events matter, the client includes a **replay cursor**: an
application-defined position recording the last event it processed durably.

```text
client last processed sequence: 1842
server current sequence:         1847
replay required:                 1843..1847
```

The durable store retains those events; the new WebSocket only transports them.
This separates two forms of continuity:

```text
transport connection = disposable byte path
application session  = identity, subscriptions, and replay position
```

## What a Successful Send Proves

Delivery crosses several boundaries:

```text
gateway queues bytes
    -> send() accepts bytes
    -> peer TCP acknowledges them
    -> browser runtime reads them
    -> application callback runs
    -> application records the new sequence
```

`send()` proves only an early local step. If durable storage deletes event
`1843` immediately after `send()` succeeds, a later disconnect can lose the
event from the user's perspective.

Retries create duplicates. If the browser applies `1843` but its acknowledgement
is lost, the server may replay it. A typical client applies events
idempotently:

```text
sequence <= lastApplied       -> duplicate, ignore
sequence == lastApplied + 1   -> apply and advance
sequence > lastApplied + 1    -> gap, request replay
```

An **idempotent** operation can be applied more than once without changing the
final result after the first application. The sequence check makes replayed
events idempotent from this client's perspective.

This produces application-level **at-least-once delivery**: the system prefers
a possible duplicate over silently losing a durable event. WebSocket itself
promises neither replay nor exactly-once effects.

## Reconnect Storms and Graceful Draining

If a gateway closes 100,000 sockets at once, 100,000 clients may reconnect at
once and overload DNS, TLS termination, authentication, and subscription
restoration. **Exponential backoff** increases the delay after each failed
attempt. **Jitter** adds randomness so clients do not retry in lockstep. Servers
also bound handshake concurrency and recover capacity gradually.

A graceful deployment stops accepting new connections, leaves load-balancer
rotation, drains existing sessions for a bounded period, and closes the
remainder in controlled batches. Backoff is still necessary because crashes do
not drain gracefully.

> **What to remember:** Silence is not proof of health or failure. Heartbeats
> turn silence into a bounded decision. After failure, a new WebSocket restores
> only transport; application identity, subscriptions, missed events, and
> duplicate handling must be reconstructed explicitly.

---

# 9. From One Event Loop to a Gateway Fleet

At this point the protocol and one-gateway story are complete. Sections 9 and
10 are the production extension: they explain how the same ownership model
survives additional CPU cores, machines, failures, and operational limits.
They do not change the WebSocket wire protocol.

The single-loop design has one owner and little synchronization, but its
parsing, TLS work, queue management, and system calls still execute on one CPU
core.

A **CPU core** can execute one hardware instruction stream at a time, ignoring
simultaneous-multithreading details. An event-loop thread that remains busy has
therefore reached a computational limit even if the machine has other idle
cores.

## Multiple Cores

A common gateway runs one event loop per core or small CPU set. Each connection
belongs to exactly one loop for its lifetime:

<div>
    <center>{% include figure.html path="assets/img/websockets/multicore_gateway.svg" alt="A multi-core WebSocket gateway with separate event loops, connection shards, command queues, and a worker pool" caption="Connections are sharded by ownership. Cross-thread results are posted back to the owning loop instead of mutating socket state directly." %}</center>
</div>

This is **sharding by ownership**: divide the connection table into independent
subsets, each mutated by one loop. For example:

```text
loop 0 owns connections 1000, 1004, 1008, ...
loop 1 owns connections 1001, 1005, 1009, ...
loop 2 owns connections 1002, 1006, 1010, ...
loop 3 owns connections 1003, 1007, 1011, ...
```

The exact assignment can be round-robin, hash-based, or load-aware. The
important invariant is that one live connection has one current owner.

A **central acceptor** is one thread that calls `accept4()` and hands each new
descriptor to an owner loop. Alternatively, Linux `SO_REUSEPORT` allows several
listening sockets to bind the same local address and port, so each loop can
accept connections assigned to its listener by the kernel.

Neither strategy guarantees equal ongoing work. Equal connection counts do not
mean equal traffic: one loop may own active market-data clients while another
owns idle dashboards.

Most gateways do not migrate live sockets between loops. Migration would need
to transfer the descriptor, TLS and decoder state, queued output, timers,
subscriptions, and pending completions without losing order. It is simpler to
balance at accept or reconnect time.

A **worker pool** is a bounded set of threads that executes work inappropriate
for the event loop, such as blocking database calls or expensive CPU
transformations. Its queue must also be bounded. Moving a slow database call
off the event loop does not solve overload if millions of database tasks
accumulate elsewhere.

Production C++ systems usually use Boost.Asio and Beast, libevent, libuv,
Folly, Seastar, uWebSockets, or another mature runtime. Their APIs may use
callbacks or coroutines, but the underlying ownership remains recognizable:

```text
request asynchronous operation
    -> runtime registers interest
    -> kernel reports progress
    -> runtime resumes the owning connection
```

## Multiple Gateways

A **gateway** is the server process or machine that terminates WebSocket
connections and owns their live state. A **load balancer** accepts or forwards
new connections across a fleet of gateways.

When one machine is insufficient, the load balancer chooses a gateway while a
connection is established. Unlike a sequence of short HTTP requests, the
WebSocket remains attached to that gateway until it closes. Adding a new
gateway changes placement of new connections but does not automatically move
existing ones.

<div>
    <center>{% include figure.html path="assets/img/websockets/distributed_gateways.svg" alt="Clients connected through a load balancer to WebSocket gateways with a connection directory and event broker" caption="Distributed routing locates the gateway; the gateway's local registry locates the owning event loop and connection." %}</center>
</div>

An order event now needs two levels of routing:

```text
distributed: user 42 -> gateway eu-17
local:       user 42 -> loop 2, connection 1041
```

Load-balancer stickiness does not solve this problem. It may influence where a
reconnecting client lands, but it does not tell the order service where the
current live socket resides and cannot preserve a socket through gateway
failure.

## Directory, Broker, and Replay Store

Three components have distinct jobs:

```text
connection directory  where is the user's socket now?
broker                 how does an event reach that gateway?
replay store           what has the client not processed yet?
```

Collapsing them into a vague “messaging layer” hides failure semantics.

A **connection directory** is an index of current routing claims. A **broker**
transports events between services and gateway consumers. A **replay store**
retains durable ordered events long enough for a reconnecting client to request
what it missed.

A directory entry may contain:

```text
userId:        42
gatewayId:     eu-17
connectionId:  1041
sessionId:     tab-8
generation:    73
leaseExpiry:   <timestamp>
```

The directory stores a versioned routing claim, not the socket. The socket
exists only in `eu-17` and its kernel. Leases, generations, and conditional
updates help a reconnect supersede stale placement.

A **lease** is a claim that expires unless its owner renews it. The expiration
prevents a crashed gateway from appearing authoritative forever. A
**generation** is a monotonically newer version used to reject delayed work for
an older session.

<div>
    <center>{% include figure.html path="assets/img/websockets/broker-replay-ha.svg" alt="Connection directory, event broker, replay store, gateway, and client with separate delivery responsibilities" caption="Socket location, event transport, and durable replay are separate responsibilities with separate progress and failure states." %}</center>
</div>

One-to-one delivery can route to the owning gateway. A large room should route
one copy to every gateway with at least one participant, then fan out locally,
rather than producing one broker message per participant or broadcasting to
uninterested gateways.

At scale, the directory is **partitioned** so different servers own different
subsets of users, and **replicated** so each subset has copies across failure
domains. A quorum-backed directory requires enough replicas—commonly a
majority—to accept an authoritative update. If it loses quorum, a gateway must
choose an explicit policy: reject new globally routable sessions, accept
clearly degraded local-only sessions, or use bounded stale routes with known
duplicate and miss risks. Pretending registration succeeded makes a live socket
undiscoverable to the rest of the system.

If the broker is unavailable, established WebSockets can remain open while
external server-push delivery pauses. Durable events wait in replicated
storage; ephemeral updates may be dropped. The gateway must not acknowledge a
durable event merely because it entered an in-memory socket queue.

## Multi-Region Placement

A **region** is a geographically separate deployment containing gateways and
supporting services. Clients normally connect to a nearby healthy region.
Events may originate in a different region and must reach the region that owns
the socket.

During regional failure, clients reconnect elsewhere and create new directory
entries. Whether old durable events are immediately available depends on the
application's replication contract. **Synchronous replication** waits for
remote confirmation before acknowledging a write. **Asynchronous replication**
acknowledges locally and copies later, allowing a bounded amount of recent data
to be unavailable or lost during failover. That possible loss window is the
recovery-point objective. WebSockets provide none of those data guarantees.

Presence is consequently approximate. “Online” usually means at least one
sufficiently fresh session lease is known, not that a globally synchronous
boolean is true. That is useful for UI presence but unsafe as a strong
authorization or financial fact.

> **What to remember:** Scaling preserves the same ownership chain rather than
> replacing it. A user routes to one or more gateways; each gateway routes to
> an owner loop; the loop owns the `Connection`; the kernel owns the socket.
> The directory locates live state, the broker transports events, and the replay
> store restores durable history.

---

# 10. Security, Capacity, and Operations

A long-lived connection reserves resources before it does useful work. A
production gateway must bound every phase and observe the dimensions that can
grow.

## Security and Resource Bounds

**Authentication** determines who the peer is. **Authorization** determines
what that authenticated identity may do. Authentication identifies the session;
authorization still applies to every
subscription and sensitive command. Long-lived credentials also need an expiry
or revocation policy: close and reconnect, refresh the session, periodically
revalidate, or authorize each action independently.

Browsers can attach cookies to a WebSocket handshake. A malicious origin may
try to open a socket with the victim's ambient credentials, so cookie-based
servers validate the `Origin` header against an allowlist.

An internet-facing gateway should bound at least:

- connections and establishment rate per source and account
- concurrent TLS and incomplete upgrade handshakes
- handshake bytes and unauthenticated lifetime
- inbound frame, message, and message-rate limits
- parsing work per event-loop turn
- subscriptions per connection
- queued output per connection, loop, and process
- worker-queue depth and dependency concurrency
- heartbeat, idle, and close deadlines

These controls protect against attacks, buggy clients, and ordinary dependency
slowdowns.

## Capacity Is More Than Connection Count

Process memory is roughly:

```text
connections
* (userspace state + average queued output + TLS state + allocator overhead)
```

Kernel TCP state and allocated socket buffers must be measured separately.
Socket autotuning, kernel version, TLS library, allocator, and workload all
change the result.

File-descriptor planning includes client sockets plus listeners, `epoll`
instances, wake and timer descriptors, broker and database connections, logs,
and headroom. Process, service-manager, and system-wide limits must agree.

Clients and NATs also have finite address/port capacity. Many simultaneous
connections from one source IP to the same destination need distinct source
ports from the configured ephemeral range. A load test concentrated behind a
few source addresses can exhaust client or NAT mappings before the gateway
reaches its own limit.

For event rate `R`, encoded size `S`, and average fanout `F`:

```text
outbound application bandwidth ~= R * S * F
```

A 1 KB event sent 1,000 times per second to 100 recipients is about 100 MB/s
before TLS, TCP, IP overhead, and retransmission. Connection count alone says
nothing about this cost.

Steady state and recovery are different workloads. A fleet that can maintain a
million idle sockets may be unable to recreate them quickly after a regional
disconnect. Capacity tests include DNS, accepts, TLS handshakes,
authentication, directory registration, and subscription restoration.

## Observe Ownership and Progress

Useful metrics include:

- active, opening, and closing connections
- establishment latency and failure reason by phase
- normal, abnormal, heartbeat, and slow-consumer disconnects
- active connections and event-loop lag per loop
- maximum callback duration and timer scheduling lag
- bytes read and written
- queued output bytes per connection, loop, and process
- worker and command-queue depth
- broker-to-gateway, gateway-to-socket, and client-acknowledgement progress
- reconnect and resubscription rate

Aggregate CPU can hide one saturated loop, and “message sent” can hide that
only the gateway queue advanced. Metrics should preserve these separate stages.

Failure paths must be tested rather than inferred: kill a gateway, pause a
broker consumer, suspend a client's receive path, expire a directory lease,
change a client's network, and restart a fleet in stages. Verify that queues
remain bounded and that reconnect, routing, replay, and duplicate handling
behave as designed.

> **What to remember:** Capacity is multi-dimensional: connection state,
> queued bytes, descriptors, bandwidth, establishment rate, and recovery load
> can fail independently. Bounds keep failure finite; metrics reveal which
> ownership or delivery stage has stopped advancing.

---

# 11. When WebSockets Are the Wrong Tool

WebSockets are valuable when both endpoints send frequently or server-to-client
latency matters enough to justify persistent connection infrastructure. They
are not the default for every changing page.

| Mechanism | Good fit | Main trade-off |
|---|---|---|
| Ordinary HTTP | Client-driven, cacheable, independent operations | Server cannot initiate an update |
| Polling | Infrequent changes and loose freshness requirements | Repeated requests and stale intervals |
| Long polling | Server updates through conservative HTTP infrastructure | Repeated request lifecycle and retry complexity |
| Server-Sent Events | Browser-oriented, server-to-client streams | Client commands still use HTTP; one-way stream |
| WebSocket | Low-latency bidirectional sessions | Stateful connection ownership and recovery |

Choose WebSockets when the product benefits from a persistent bidirectional
channel and the team is prepared to operate heartbeats, backpressure, graceful
draining, reconnects, replay, distributed routing, and per-connection limits.

The protocol removes repeated request overhead. It moves complexity into the
lifecycle of the connection.

> **What to remember:** Use the least stateful mechanism that meets the
> product's communication requirement. WebSocket is most valuable when the
> application genuinely needs a low-latency, bidirectional session.

---

# 12. The Complete Mental Model

Return to the order-tracking browser.

1. The browser resolves `orders.example.com` and creates TCP, TLS, and
   WebSocket protocol state.
2. Client and server kernels distinguish the TCP flow using endpoint pairs;
   NAT or proxies may translate or terminate those pairs along the path.
3. The gateway accepts a connected socket as fd `96`, assigns connection
   `1041`, completes the upgrade, authenticates user 42 during the handshake or
   first message, and makes the connection application-ready on loop 2.
4. While the connection is idle, kernel state, a userspace object, and event
   registrations remain. No dedicated thread runs beside it.
5. When the order service publishes sequence `1843`, distributed routing finds
   gateway `eu-17`; the local registry finds loop 2 and connection `1041`.
6. The owner loop queues the bytes and writes them as the socket becomes ready,
   subject to bounded backpressure policy.
7. If the client's network changes, the old TCP connection dies. A new
   connection authenticates and resumes after sequence `1842` from durable
   application state.

The single-process model is:

```text
non-blocking sockets
    + readiness notification
    + one owner per connection
    + bounded protocol and output state
```

The distributed model adds:

```text
user or subscription
    -> owning gateway
    -> owning event loop
    -> generation-safe Connection
    -> kernel socket
```

WebSocket provides the live byte path. The surrounding system provides
identity, pressure policy, failure detection, routing, durability, and
recovery. Keeping those responsibilities separate is what makes the server
understandable—and operable.

---

# Compact Glossary

| Term | Meaning in this article |
|---|---|
| Kernel | Privileged operating-system core that owns TCP, sockets, and their buffers. |
| System call | Controlled operation through which userspace asks the kernel to do work. |
| Endpoint | One IP address and TCP port, such as `203.0.113.40:443`. |
| TCP connection | One reliable ordered byte stream between two endpoints. |
| Socket | Kernel object containing endpoint, TCP, and buffer state. |
| Listening socket | Socket waiting for new TCP connection attempts. |
| Connected socket | Socket representing one established TCP connection. |
| File descriptor | Process-local integer handle used to access a socket or another kernel object. |
| Ephemeral port | Temporary client-side source port selected for an outbound connection. |
| WebSocket message | Complete text or binary unit exposed to the application. |
| WebSocket frame | Wire-level unit carrying part or all of a message, or one control signal. |
| Opcode | Frame-header value identifying continuation, text, binary, Close, Ping, or Pong. |
| Fragmentation | Splitting one text or binary message across several WebSocket frames. |
| Control frame | Close, Ping, or Pong frame; at most 125 payload bytes and never fragmented. |
| Masking | Reversible client-frame transformation required by WebSocket; it is not encryption. |
| Subprotocol | Named application protocol selected from values offered during the handshake. |
| Extension | Negotiated change to WebSocket operation, such as per-message compression. |
| Close code | Numeric category carried in a Close frame to explain why closing began. |
| `bufferedAmount` | Browser count of application bytes queued by `send()` but not yet passed to the network. |
| Non-blocking I/O | Operation returns `EAGAIN` instead of suspending the thread when it cannot progress. |
| Readiness | Kernel indication that an I/O attempt may currently make progress. |
| Interest set | Descriptors and event types registered with an `epoll` instance. |
| Ready set | Registered objects whose current state satisfies the requested interest. |
| Event loop | Thread that waits for events and runs short handlers for their owners. |
| Connection ID | Gateway-assigned logical identity for one socket lifetime, independent of descriptor reuse. |
| Backpressure | Downstream work is accepted more slowly than upstream work is produced. |
| Heartbeat | Periodic exchange used to detect an unresponsive connection within a deadline. |
| Replay cursor | Application position recording the last event processed by a client. |
| Gateway | Process or machine that terminates and owns live WebSocket connections. |
| Connection directory | Distributed index that maps application identities to current gateway ownership. |
| Broker | Infrastructure that transports events from producers to gateway consumers. |
| Replay store | Durable ordered history used to restore events missed during disconnection. |

---

# References

1. IETF, [RFC 6455: The WebSocket Protocol](https://www.rfc-editor.org/rfc/rfc6455)
2. WHATWG, [WebSockets Standard](https://websockets.spec.whatwg.org/)
3. IETF, [RFC 9293: Transmission Control Protocol](https://www.rfc-editor.org/rfc/rfc9293)
4. IETF, [RFC 5382: NAT Behavioral Requirements for TCP](https://www.rfc-editor.org/rfc/rfc5382)
5. IETF, [RFC 8441: Bootstrapping WebSockets with HTTP/2](https://www.rfc-editor.org/rfc/rfc8441)
6. Linux man-pages, [`socket(7)`](https://man7.org/linux/man-pages/man7/socket.7.html)
7. Linux man-pages, [`tcp(7)`](https://man7.org/linux/man-pages/man7/tcp.7.html)
8. Linux man-pages, [`accept(2)` and `accept4(2)`](https://man7.org/linux/man-pages/man2/accept.2.html)
9. Linux man-pages, [`connect(2)`](https://man7.org/linux/man-pages/man2/connect.2.html)
10. Linux man-pages, [`epoll(7)`](https://man7.org/linux/man-pages/man7/epoll.7.html)
11. Linux man-pages, [`eventfd(2)`](https://man7.org/linux/man-pages/man2/eventfd.2.html)
12. Boost.Beast, [WebSocket documentation and asynchronous examples](https://www.boost.org/doc/libs/latest/libs/beast/doc/html/beast/using_websocket.html)
13. Linux man-pages, [`epoll_ctl(2)`](https://man7.org/linux/man-pages/man2/epoll_ctl.2.html)
14. Linux man-pages, [`epoll_wait(2)`](https://man7.org/linux/man-pages/man2/epoll_wait.2.html)
15. Linux man-pages, [`recv(2)`](https://man7.org/linux/man-pages/man2/recv.2.html) and [`send(2)`](https://man7.org/linux/man-pages/man2/send.2.html)
