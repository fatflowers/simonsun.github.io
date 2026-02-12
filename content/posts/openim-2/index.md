---
title: "OpenIM Self-Hosting Pitfalls (2): Socket Leak"
date: 2026-02-12
categories: ["Tech"]
tags: ["OpenIM", "Go", "Backend", "Socket"]
draft: false
---

> We deployed [OpenIM](https://github.com/openimsdk) version 3.5.1 in our Kubernetes environment for our company's IM scenarios. During development and operation, we encountered some issues. Here, I'll document the details of the problems and the resolution process.

## Problem Trigger Scenario
300 users online simultaneously, sending one-on-one messages irregularly.

## Problem Phenomenon
From the monitoring metrics, we can see:
- A large number of goroutines in `openimserver-openim-push` and `openimserver-openim-msggateway`.
- A large number of socket connections in `openimserver-openim-push` and `openimserver-openim-msggateway` pods.

![Excessive Socket Count](/posts/openim-2/0401socketCount.png)

## Cause

This looks like a socket leak: socket connections are opened but not actively closed, leading to a coroutine leak.

![Message Downstream Data Link](/posts/openim-2/msg_downstream_en.png)


## Solution
```go
// open-im-server/internal/push/push_to_client.go
	for host, userIds := range usersHost {
		tconn, err := p.discov.GetConn(ctx, host)
		if err != nil {
			log.ZError(ctx, "p.discov.GetConn failed", err)
		}

        // Add the following code to close tconn after use
		if tconn != nil {
			defer tconn.Close()
		}
	}
```


## Summary and Reflection
**Pay attention to important runtime metrics**: We saw the socket count anomaly from the monitoring dashboard in time, and found this problem after troubleshooting. Later, the `openim-msggateway` pod socket usage peaked at over 40,000. If not handled, in extreme cases, the pod sockets would be exhausted, causing program exceptions.

**Understand the program implementation**: When self-hosting open-source projects, you should be as familiar as possible with their implementation to discover problems in time.

**The solution is not optimal**: The correct implementation should be to maintain a TCP connection pool between `openim-push` and `openim-msggateway`, so that multiple different requests can reuse the same batch of TCP connections, speeding up the calling process and avoiding socket resource waste.
