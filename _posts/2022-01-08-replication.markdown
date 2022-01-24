---
layout: post
title: Replication
date: 2022-01-08 00:00:00-0000
categories: ['Distributed Systems Components']
---


Fault-Tolerence of a system can be acheived by **replication**. 
There are two types of replication

1. State Replication: 
    * The contents of RAM, disk, registers etc. are transferred from primary to secondary.
    * Primary sends [new] state to backup
    * It is simple but consumes a lot of network bandwith as the complete state is transferred
    
2. Replicated State Machine:
    * 

<!-- ### Requirements

**Functional**
1. Send Notification
2. Plugable 


* user, media, friendship : Postgres
* user feeds, activities: Cassandra -->