---
title: "OpenIM Self-Hosting Pitfalls (3): Scaling Errors"
date: 2026-02-12
categories: ["Tech"]
tags: ["OpenIM", "Go", "Backend", "K8s", "Consistent Hashing"]
draft: false
---

> We deployed [OpenIM](https://github.com/openimsdk) version 3.5.1 in our Kubernetes environment for our company's IM scenarios. During development and operation, we encountered some issues. Here, I'll document the details of the problems and the resolution process.

## Problem Description
Simply put: **Users are clearly online but cannot receive real-time messages. They can only receive push notifications, and messages only appear in the chat interface after a delay.**

This problem only occurs after scaling out. Everything works fine with a single node. The root cause is an inconsistency in OpenIM's internal load balancing mechanism, causing messages to be sent to the wrong service node.

## Trigger Scenario
Scaling `openim-msggateway` (from 1 node to multiple nodes).

## Encountered Problem
After scaling, online users cannot receive websocket messages pushed by the backend. They can only receive offline notification bar push messages. After a period of time, the pushed messages can be seen in the client's chat interface.

## Problem Principle (Simplified)
Before diving into the code, let's use a metaphor to understand this problem:

Imagine a delivery system:
- 📱 **Client**: Recipient
- 📍 **openim-msggateway-proxy**: Customer Service Center (Assigns delivery station to customers via Navigation System B)
- 🏠 **openim-msggateway**: Delivery Station (Multiple)
- 🚚 **openim-push**: Courier (Has their own Navigation System A)

Actual Process:
- **Establish Connection**: Recipient first goes to the Customer Service Center. The Customer Service Center assigns Delivery Station A to the recipient via Navigation System B.
- **Deliver Package**: The Courier uses their own Navigation System A to independently calculate which delivery station the recipient should be at.

Normal Scenario:
1. Customer Service Center's Navigation System B: Recipient -> Station A
2. Courier's Navigation System A: Recipient -> Station A
3. Recipient successfully receives the package at Station A

But the current problem is:
1. Customer Service Center's Navigation System B: Recipient -> Station A
2. Courier's Navigation System A: Recipient -> Station B
3. Recipient waits for the package at Station A, but the Courier delivers it to Station B.

<img src="/posts/openim-3/metaphor_en.svg" alt="Metaphor Illustration" width="600" style="max-width: 100%; height: auto;">

**Two independent navigation systems gave different results!**

## Technical Cause Analysis

### OpenIM Internal Connection Link
![OpenIM Internal Connection Link](/posts/openim-3/full_path_link_en.png)

As seen in the figure, the problem involves two independent routing processes:
- **Message Sending Path**: `openim-push` directly finds the `openim-msggateway` node connected to the user via consistent hashing.
- **Client Connection Path**: The client establishes a websocket connection with `openim-msggateway` via `openim-msggateway-proxy`.

Both paths need to select the same `openim-msggateway` node via consistent hashing, but they use different node address formats!

### Key Code Difference

The core of the problem lies in the inconsistent way service node addresses are constructed in two places:

**First Place: OpenIM-Push Side (When sending messages)**
```go
// open-im-server/pkg/common/discoveryregister/kubernetes/kubernetes.go
// Constructed address format (Notice the :88 port at the end)
host := fmt.Sprintf("%s-openim-msggateway-%d.%s-openim-msggateway-headless.%s.svc.cluster.local:88",
    instance, i, instance, ns, port)
```

**Second Place: OpenIM-MsgGateway-Proxy Side (When establishing connection)**
```go
// openim-msggateway-proxy/common/consistentHash.go
// Constructed address format (Notice no port number)
host := fmt.Sprintf("%s-openim-msggateway-%d.%s-openim-msggateway-headless.%s.svc.cluster.local",
    instance, i, instance, ns)
```

### Problem Example
![Input side and output side find different openim-msggateway](/posts/openim-3/expansion_en.png)

Assume scaling to 5 nodes:
1. **User Connection**: Assigned to the 4th node via consistent hashing.
2. **Send Message**: Due to different address formats, consistent hashing might route the message to the 2nd node.
3. **Result**: User connection not found on the 2nd node, message sending fails, degrading to offline push.

## Solution
The solution is very simple: make both places use the same address format.

```go
// openim-msggateway-proxy/common/consistentHash.go
func getMsgGatewayHost(ctx context.Context) []string {
    // Original code
    // host := fmt.Sprintf("%s-openim-msggateway-%d.%s-openim-msggateway-headless.%s.svc.cluster.local", instance, i, instance, ns)

    // Modified: Append :88 port to match openim-push side
    host := fmt.Sprintf("%s-openim-msggateway-%d.%s-openim-msggateway-headless.%s.svc.cluster.local:88", instance, i, instance, ns)

    // ... Other code remains unchanged
}
```

## Reflection and Summary
- **Small problem, big impact**: A difference of just 3 characters can cause the entire real-time messaging function to fail.
- **Scale with caution**: Problems not exposed with a single node may surface after scaling.
- **Understanding internal mechanisms is important**: Without understanding OpenIM's internal load balancing mechanism, it's hard to quickly locate the problem.
- **Consistency is key**: In distributed systems, the way components identify the same resource must remain consistent.

When self-hosting open-source projects, besides focusing on functional implementation, you also need to deeply understand their internal mechanisms to quickly locate and solve problems when they arise.
