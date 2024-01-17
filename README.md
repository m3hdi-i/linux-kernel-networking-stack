
# linux-kernel-networking-stack

In this project, We're going to evaluate the performance of the Linux kernel's network functions in two parts: send flow and receive flow, using eBPF.
<br/>

**Table of contents:**

- What is tracing
- Introduction to eBPF and bpftrace  
- The Linux kernel's networking stack  
- Measuring latency of sending network packets  
- Measuring latency of receiving network packets  
- References

## What is Tracing
Tracing in the context of the Linux kernel refers to the process of recording and analyzing the behavior of the kernel and its components. Tracing is a powerful tool that can help developers understand how the kernel works, identify performance bottlenecks, and debug issues. There are several tracing technologies available in the Linux kernel. These technologies allow developers to trace different aspects of the kernel’s behavior, such as function calls, system calls, resource allocation, and more.

## Introduction to eBPF and bpftrace  

[**eBPF**](https://ebpf.io/) (extended Berkeley Packet Filter) is a technology that allows developers to write programs that run inside the Linux kernel (available in recent Linux kernels 4.x). These programs can be used to trace and extend the capabilities of the kernel and its components, as well as to filter and manipulate network packets. eBPF programs are executed in a sandboxed and safe manner, and they can be loaded and unloaded dynamically without requiring a kernel restart.
**bpftrace** is a high-level tracing language for Linux that is based on eBPF technology. It allows developers to write small programs that execute whenever an event occurs, such as a kernel function call.
bpftrace uses LLVM as a backend to compile scripts to BPF-bytecode and makes use of [BCC](https://github.com/iovisor/bcc) for interacting with the Linux BPF system, as well as existing Linux tracing capabilities: kernel dynamic tracing (kprobes), user-level dynamic tracing (uprobes), and tracepoints. The bpftrace language is inspired by awk and C, and predecessor tracers such as DTrace and SystemTap.

To install bpftrace, you can follow the instructions provided on the [official GitHub page](https://github.com/iovisor/bpftrace). 

## The Linux kernel's networking stack  
In the Linux kernel, many functions are used to process network packets. Here we review some of the most important ones in a scenario of TCP over IPv4 over Ethernet connections:
<br/>


## Transmission path

**Layer 5: Session layer (sockets and files)**

There are three system calls that can send data over the network:

- write (memory data to a file descriptor)
- sendto (memory data to a socket)
- sendmsg (a composite message to a socket)

All of these eventually end up in \_\_sock\_sendmsg(), which does security\_sock\_sendmsg() to check permissions and then forwards the message to the next layer using the socket's sendmsg virtual method.

**Layer 4: Transport layer (TCP)**

tcp\_sendmsg: for each segment in the message

1. find an sk\_buff with space available (use the one at the end if space left, otherwise allocate and append a new one)
2. copy data from user space to sk\_buff data space (kernel space, probably DMA-able space) using skb\_add\_data().
  - The buffer space is pre-allocated for each socket. If the buffer runs out of space, communication stalls: the data remains in user space until buffer space becomes available again (or the call returns with an error immediately if it was non-blocking).
  - The size of allocated sk\_buff space is equal to the MSS (Maximum Segment Size) + headroom (MSS may change during connection, and is modified by user options).
  - Segmentation (or coalescing of individual writes) happens at this level. Whatever ends up in the same sk\_buff will become a single TCP segment. Still, the segments can be fragmented further at IP level.
3. The TCP queue is activated; packets are sent with tcp\_transmit\_skb() (called multiple times if there are more active buffers).
4. tcp\_transmit\_skb() builds the TCP header (the allocation of the sk\_buff has left space for it). It clones the skb in order to pass control to the network layer. The network layer is called through the queue\_xmit virtual function of the socket's address family (inet\_connection\_sock→icsk\_af\_ops).

**Layer 3: Network layer (IPv4)**

1. ip\_queue\_xmit() does routing (if necessary), creates the IPv4 header
2. nf\_hook() is called in several places to perform network filtering (firewall, NAT, …). This hook may modify the datagram or discard it.
3. The routing decision results in a destination (dst\_entry) object. This destination models the receiving IP address of the datagram. The dst\_entry's output virtual method is called to perform actual output.
4. The sk\_buff is passed on to ip\_output() (or another output mechansim, e.g. in case of tunneling).
5. ip\_output() does post-routing filtering, re-outputs it on a new destination if necessary due to netfiltering, fragments the datagram into packets if necessary, and finally sends it to the output device.
  - Fragmentation tries to reuse existing fragment buffers, if possible. This happens when forwarding an already fragmented incoming IP packet. The fragment buffers are special sk\_buff objects, pointing in the same data space (no copy required).
  - If no fragment buffers are available, new sk\_buff objects with new data space are allocated, and the data is copied.
  - Note that TCP already makes sure the packets are smaller than MTU, so normally fragmentation is not required.
6. Device-specific output is again through a virtual method call, to output of the dst\_entry's neighbour data structure. This usually is dev\_queue\_xmit. There is some optimisation for packets with a known destination (hh\_cache).

**Layer 2: Link layer (e.g. Ethernet)**

The main function of the kernel at the link layer is scheduling the packets to be sent out. For this purpose, Linux uses the queueing discipline (struct Qdisc) abstraction.

dev\_queue\_xmit puts the sk\_buff on the device queue using the qdisc→enqueue virtual method.

- If necessary (when the device doesn't support scattered data) the data is linearised into the sk\_buff. This requires copying.
- Devices which don't have a Qdisc (e.g. loopback) go directly to dev\_hard\_start\_xmit().
- Several Qdisc scheduling policies exist. The basic and most used one is pfifo\_fast, which has three priorities.

The device output queue is immediately triggered with qdisc\_run(). It calls qdisc\_restart(), which takes an skb from the queue using the qdisc→dequeue virtual method. Specific queueing disciplines may delay sending by not returning any skb, and setting up a qdisc\_watchdog\_timer() instead. When the timer expires, netif\_schedule() is called to start transmission.

Eventually, the sk\_buff is sent with dev\_hard\_start\_xmit() and removed from the Qdisc. If sending fails, the skb is re-queued.netif\_schedule() is called to schedule a retry.

netif\_schedule() raises a software interrupt, which causes net\_tx\_action() to be called when the NET\_TX\_SOFTIRQ is ran by ksoftirqd. net\_tx\_action() calls qdisc\_run() for each device with an active queue.

dev\_hard\_start\_xmit() calls the hard\_start\_xmit virtual method for the net\_device. But first, it calls dev\_queue\_xmit\_nit(), which checks if a packet handler has been registered for the ETH\_P\_ALL protocol. This is used for tcpdump.

The device driver's hard\_start\_xmit function will generate one or more commands to the network device for scheduling transfer of the buffer. After a while, the network device replies that it's done. This triggers freeing of the sk\_buff. If the sk\_buff is freed from interrupt context, dev\_kfree\_skb\_irq() is used. This delays the actual freeing until the next NET\_TX\_SOFTIRQ run, by putting the skb on the softnet\_data completion\_queue. This avoids doing frees from interrupt context.

## Receive flow

**Layer 2: Link layer (e.g. Ethernet)**

The network device pre-allocates a number of sk\_buffs for reception. How many, is configured per device. Usually, the addresses to the data space in these sk\_buffs are configured directly as DMA area for the device. The device interrupt handler takes the sk\_buff and performs reception handling on it. Before NAPI, this was done using netif\_rx(). In NAPI, it is done in two phases.

1. From the interrupt handler, the device driver just calls netif\_rx\_schedule() and returns from interrupt. netif\_rx\_schedule() adds the device to softnet\_data's poll\_list and raises the NET\_RX\_SOFTIRQ software interrupt.
2. ksoftirqd runs net\_rx\_action(), which calls the device's poll virtual method. The poll method does device-specific buffer management, calls netif\_receive\_skb() for each sk\_buff, allocates new sk\_buffs as required, and terminates with netif\_rx\_complete().

netif\_receive\_skb() finds out how to pass the sk\_buff to upper layers.

1. netpoll\_rx() is called, to support the Netpoll API
2. Call packet handlers for ETH\_P\_ALL protocol (for tcpdump)
3. Call handle\_ing() for ingress queueing
4. Call handle\_bridge() for bridging
5. Call handle\_macvlan() for virtual LAN
6. Call the packet handler registered for the L3 protocol specified by the packet.

The packet handlers are called with the deliver\_skb() function, which calls the protocol's func virtual method to handle the packet.

## Layer 3: Network layer (IPv4, ARP)

**ARP** <br/>
ARP packets are handled with arp\_rcv(). It processes the ARP information, stores it in the neighbour cache, and sends a reply if required. In the latter case, a new sk\_buff (with new data space) is allocated for the reply.

**IPv4** <br/>
IPv4 packets are handled with ip\_rcv(). It parses headers, checks for validity, sends an ICMP reply or error message if required. It also looks up the destination address using ip\_route\_input(). The destination's input virtual method is called with the sk\_buff.

- ip\_mr\_input() is called for multicast addresses. The packet may be forwarded using ip\_mr\_forward(), and it may be delivered locally using ip\_local\_delivery().
- ip\_forward() is called for packets with a different destination for which we have a route. It directly calls the neighbour's output virtual method.
- ip\_local\_deliver() is called if this machine is the destination of the packet. Datagram fragments are collected here.

ip\_local\_deliver() delivers to any raw sockets for this connection first, using raw\_local\_deliver(). Then, it calls the L4 protocol handler for the protocol specified in the datagram. The L4 protocol is called even if a raw socket exists.

Throughout, xfrm4\_policy\_check calls are included to support IPSec.

**Layer 4: Transport layer (TCP)**

The net\_protocol handler for TCP is tcp\_v4\_rcv(). Most of the code here deals with the protocol processing in TCP, for setting up connections, performing flow control, etc.

A received TCP packet may include an acknowledgement of a previously sent packet, which may trigger further sending of packets (tcp\_data\_snd\_check()) or of acknowledgements (tcp\_ack\_snd\_check()).

Passing the incoming packet to an upper layer is done in tcp\_rcv\_established() and tcp\_data\_queue(). These functions maintain the tcp connection's out\_of\_order\_queue, and the socket's sk\_receive\_queue and sk\_async\_wait\_queue. If a user process is already waiting for data to arrive, the data is immediately copied to user space using skb\_copy\_datagram\_iovec(). Otherwise, the sk\_buff is appended to one of the socket's queues and will be copied later.

Finally, the receive functions call the socket's sk\_data\_ready virtual method to signal that data is available. This wakes up waiting processes.

**Layer 5: Session layer (sockets and files)**

There are three system calls that can receive data from the network:

- read (memory data from a file descriptor)
- recvfrom (memory data from a socket)
- recvmsg (a composite message from a socket)

All of these eventually end up in \_\_sock\_recvmsg(), which does security\_sock\_recvmsg() to check permissions and then requests the message to the next layer using the socket's recvmsg virtual method. This is often sock\_common\_recvmsg(), which calls the recvmsg virtual method of the socket's protocol.

tcp\_recvmsg() either copies data from the socket's queue using skb\_copy\_datagram\_iovec(), or waits for data to arrive using sk\_wait\_data(). The latter blocks and is woken up by the layer 4 processing.

<br/>
<br/>

## Measuring latency of sending network packets

Here, our goal is to detect the traverse of a network packet (when sending) in all TCP/IP layers and find out how long it takes for a network packet to be processed in the kernel. We will do it by storing the elapsed time for each packet and finally showing a histogram of time values.


First, we need a test case program to make some dummy network traffic and trace it using eBPF, so I wrote a simple Python script to repeatedly do some HTTP get requests:

    import requests
    
    c = 1
    for i in range(100):
        try:
            response = requests.get("https://google.com")
            print("Request "+str(c)+" sent successfully!")
        except requests.exceptions.RequestException as e:
            print("An error occurred:", e)
        c+=1
        
> the test case file located [here](https://github.com/m3hdi-i/linux-kernel-networking-stack/blob/main/src/test-case.py)

<br/>
For tracing the send flow, we have selected five important kernel functions for each network layer:

Layer 5 :  `sock_sendmsg()`

Layer 4 : `tcp_sendmsg()`

Layer 3 : `ip_output()`

Layer 2 : `dev_hard_start_xmit()`

<br/>

The bpftrace script:



    #!/usr/bin/bpftrace
    
    kprobe:sock_sendmsg
    {
    	$a = (struct socket *) arg0;
    	$s = (struct sock *) $a -> sk;
    
    	@time0[$s] = nsecs;
    }
    
    kprobe:tcp_sendmsg
    {
    
    	$s = (struct sock *) arg0;
    	if (@time0[$s] > 0)
    	{
    		@time1[$s] = nsecs;
      	}
    }
    
    kprobe:ip_output
    {
    	$s = (struct sock *) arg1;
    
    	if (@time1[$s] > 0)
    	{
    		@time2[$s] = nsecs;
      	}
    }
    
    
    kprobe:dev_hard_start_xmit
    {
    	$b = (struct sk_buff *) arg0;
    	$s = (struct sock *) $b -> sk;
    
    	if (@time2[$s] > 0)
    	{
    		@time3[$s] = nsecs;
    
    		$latency = (@time3[$s] - @time0[$s]) / 1e3;
    
    		printf("--- Send Latency : %ld µs\n", $latency);
    
    		@latency_histogram = hist($latency);
    
    		delete(@time0[$s]);
    		delete(@time1[$s]);
    		delete(@time2[$s]);
    		delete(@time3[$s]);
      	}
    }
    
    END {
    	clear(@time0);
    	clear(@time1);
    	clear(@time2);
    	clear(@time3);
    }





> the source code located [here](https://github.com/m3hdi-i/linux-kernel-networking-stack/blob/main/src/lat-send.bt)

<br/>
In this script, we used kprobes to run some code when a specific kernel function was called, storing the timestamp of each function (if the packet has recognized in previous layers), and the final latency value is the difference between the timestamp of last function and first function.
**Important**: Each network packet is identified by its corresponding `struct sock *` pointer.

So, we run test-case and then run bpftrace script:

    python3 test-case.py

<br/>

    sudo bpftrace lat-send.bt
    
    

And stop both after a while.
Result:

![packet send latency](https://github.com/m3hdi-i/linux-kernel-networking-stack/blob/main/src/result-send.png)

In our case, the most of sending packets have a latency in 16-23 Micro Seconds range.

## Measuring latency of receiving network packets
In this section, most of our work is similar to the previous section and uses the same test case, but the direction of movement of packages is opposite (from layer 2 to layer 5) and the functions are also different:

Layer 2 : `__netif_receive_skb()`

Layer 3 : `ip_rcv()`

Layer 4 : `tcp_v4_rcv()`

Layer 5 : `tcp_recvmsg`

<br/>

The bpftrace script:




    #!/usr/bin/bpftrace
    
    kprobe:__netif_receive_skb
    {
    	$a = (struct sk_buff *) arg0;
    	$s = (struct sock *) $a -> sk;
    
    	@time0[$s] = nsecs;
    }
    
    kprobe:ip_rcv
    {
    	$b = (struct sk_buff *) arg0;
    	$s = (struct sock *) $b -> sk;
    
    	if (@time0[$s] > 0)
    	{
    		@time1[$s] = nsecs;
      	}
    }
    
    kprobe:tcp_v4_rcv
    {
    	$c = (struct sk_buff *) arg0;
    	$s = (struct sock *) $c -> sk;
    
    	if (@time1[$s] > 0)
    	{
    		@time2[$s] = nsecs;
      	}
    }
    
    kprobe:tcp_recvmsg
    {
    
    	$d = (struct socket *) arg0;
    	$s = (struct sock *) $d -> sk;
    
    	if (@time2[$s] > 0)
    	{
    		@time3[$s] = nsecs;
    
    		$latency = (@time3[$s] - @time1[$s]) / 1e3;
    
    		printf("--- Receive Latency : %ld µs\n", $latency);
    		@latency_histogram = hist($latency);
    		
    		delete(@time0[$s]);
    		delete(@time1[$s]);
    		delete(@time2[$s]);
    		delete(@time3[$s]);
    	}
    }
    
    END {
    	clear(@time0);
    	clear(@time1);
    	clear(@time2);
    	clear(@time3);
    }


> the source code located [here](https://github.com/m3hdi-i/linux-kernel-networking-stack/blob/main/src/lat-rcv.bt)

<br/>

Run test-case and bpftrace script:

    python3 test-case.py
<br/>

    sudo bpftrace lat-rcv.bt
    

And stop both after a while. Result:

![packet receive latency](https://github.com/m3hdi-i/linux-kernel-networking-stack/blob/main/src/result-receive.png)

In our case, the most of receiving packets have a latency in 64-128 Micro Seconds range.

<br/>

## References

https://wiki.linuxfoundation.org/networking/kernel_flow

https://www.sobyte.net/post/2022-10/linux-net-snd-rcv/

https://amrelhusseiny.github.io/blog/004_linux_0001_understanding_linux_networking/004_linux_0001_understanding_linux_networking_part_1/

https://www.youtube.com/watch?v=6Fl1rsxk4JQ

https://www.cs.dartmouth.edu/~sergey/netreads/path-of-packet/Network_stack.pdf




