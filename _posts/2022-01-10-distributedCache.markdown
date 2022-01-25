---
layout: post
title: Distributed Cache
date: 2022-01-10 00:00:00-0000
description: Caching in a distributed environment
categories: ['Distributed Systems Components']
---

Placing a cache between the web server and database decreases the latency. In systems like Facebook, Twitter the requests per second (RPS) may be as high as 500 million/sec. This type of environment requires high availability, high performance, low latency cache.

---

## Architecture

We can have two types of architecture:
1. **Co-located cache:** Each web server has an in-built cache that interacts with DB.
    * Cache scaling is dependent on the number of webservers
    * Each web server has a limited view of the database restricted to its cache
2. **Dedicated Cache Cluster:** Webservers interact with a Cache cluster that in turn interacts with DB.
    * This scales well as the caches can be added or removed from the cluster, independent of web servers.

> *` Dedicated Cache Cluster`* is a scalable solution for a distributed cache


<div>
    <img src="{{ site.baseurl }}/assets/img/distCache/CacheCluster.png">
</div>

---

## Data Distribution

Sharding the data in the cache cluster is an effective way to ensure that a single cache server is not overwhelmed by requests. The sharding ensures that the data is distributed across the cache servers. The data can be sharded based on request location, IP, etc. **`Consistent Hashing`** can be used to shard the data. 

For every incoming request, the key is hashed to find out the cache server it needs to be forwarded to. The web server also needs to keep track of the reconfiguration of shards. To manage this responsibility and isolate the webserver from cache changes, a **`cache client`** runs on every webserver. The cache client takes the requests, hashes them, and finds the cache server it should forward to.

**Cache Client**

1. Cache client keeps track of all cache server
2. It communicates with Cache Server through UDP/TCP protocol
3. It performs the hashing for every incoming request

To keep track of all the cache servers, configuration services like **` Zookeeper`** can be used. Zookeeper tracks every active cache client by heartbeat messages. 


<div>
    <img src="{{ site.baseurl }}/assets/img/distCache/CacheServer.png">
</div>

---
## Availability


To achieve availability, we can replicate every cache server. Those replicas can share the `get` requests load, while the `put` request is only forwarded to the leader. The leader selection and discovery of replica server can be performed by a configuration service like: `zookeeper`.

With replication we can have two types of setup:
1. **Eventual Consistency**: 
2. **Write to Leader, read from replicas**: 

<div>
    <img src="{{ site.baseurl }}/assets/img/distCache/Availability.png">
</div>
