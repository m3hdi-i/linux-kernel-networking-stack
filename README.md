
# linux-kernel-networking-stack

In this project, We're going to evaluate the performance of the Linux kernel's network functions in two parts: send flow and receive flow, using eBPF.

**Table of contents:**

- What is tracing
- Introduction to eBPF and bpftrace  
- The Linux kernel's networking stack  
- Measuring latency of sending network packets  
- Measuring latency of receiving network packets  
- References


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

For tracing the send flow, we have selected five important kernel functions for each network layer:

Layer 5 :  `sock_sendmsg()`

Layer 4 : `tcp_sendmsg()`

Layer 3 : `ip_output()`

Layer 2 : `dev_hard_start_xmit()`


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

In this script, we used kprobes to run some code when a specific kernel function was called, storing the timestamp of each function (if the packet has recognized in previous layers), and the final latency value is the difference between the timestamp of last function and first function.
**Important**: Each network packet is identified by its corresponding `struct sock *` pointer.

So, we run test-case and then run bpftrace script:

    python3 test-case.py
,

    sudo bpftrace lat-send.bt
    

And stop both after a while. Result:

![packet send latency](https://github.com/m3hdi-i/linux-kernel-networking-stack/blob/main/src/result-send.png)

In our case, the most of sending packets have a latency in 16-23 Micro Seconds range.

## Measuring latency of receiving network packets
In this section, most of our work is similar to the previous section and uses the same test case, but the direction of movement of packages is opposite (from layer 2 to layer 5) and the functions are also different:

Layer 2 : `__netif_receive_skb()`

Layer 3 : `ip_rcv()`

Layer 4 : `tcp_v4_rcv()`

Layer 5 : `tcp_recvmsg`



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


Run test-case and bpftrace script:

    python3 test-case.py
,

    sudo bpftrace lat-rcv.bt
    

And stop both after a while. Result:

![packet receive latency](https://github.com/m3hdi-i/linux-kernel-networking-stack/blob/main/src/result-receive.png)

In our case, the most of receiving packets have a latency in 64-128 Micro Seconds range.

## References

https://wiki.linuxfoundation.org/networking/kernel_flow

https://www.sobyte.net/post/2022-10/linux-net-snd-rcv/

https://amrelhusseiny.github.io/blog/004_linux_0001_understanding_linux_networking/004_linux_0001_understanding_linux_networking_part_1/

https://www.youtube.com/watch?v=6Fl1rsxk4JQ

https://www.cs.dartmouth.edu/~sergey/netreads/path-of-packet/Network_stack.pdf



