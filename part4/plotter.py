import matplotlib.pyplot as plt
import re

# Paste your iperf log as a multiline string
iperf_log = """
[  1] 0.0000-1.0000 sec  11.9 MBytes  99.6 Mbits/sec
[  1] 1.0000-2.0000 sec  11.5 MBytes  96.5 Mbits/sec
[  1] 2.0000-3.0000 sec  76.2 KBytes   624 Kbits/sec
[  1] 3.0000-4.0000 sec  63.6 KBytes   521 Kbits/sec
[  1] 4.0000-5.0000 sec  0.000 Bytes  0.000 bits/sec
[  1] 5.0000-6.0000 sec   640 KBytes  5.24 Mbits/sec
[  1] 6.0000-7.0000 sec  1.25 MBytes  10.5 Mbits/sec
[  1] 7.0000-8.0000 sec  1.25 MBytes  10.5 Mbits/sec
[  1] 8.0000-9.0000 sec  1.62 MBytes  13.6 Mbits/sec
[  1] 9.0000-10.0000 sec  1.25 MBytes  10.5 Mbits/sec
[  1] 10.0000-11.0000 sec  1.46 MBytes  12.3 Mbits/sec
[  1] 11.0000-12.0000 sec  1.49 MBytes  12.5 Mbits/sec
[  1] 12.0000-13.0000 sec  1.49 MBytes  12.5 Mbits/sec
[  1] 13.0000-14.0000 sec  3.87 MBytes  32.4 Mbits/sec
[  1] 14.0000-15.0000 sec  11.1 MBytes  93.3 Mbits/sec
[  1] 15.0000-16.0000 sec  11.2 MBytes  94.4 Mbits/sec
[  1] 16.0000-17.0000 sec  11.1 MBytes  93.3 Mbits/sec
[  1] 17.0000-18.0000 sec  11.2 MBytes  94.4 Mbits/sec
[  1] 18.0000-19.0000 sec  11.1 MBytes  93.3 Mbits/sec
[  1] 19.0000-20.0000 sec  12.4 MBytes   104 Mbits/sec
[  1] 20.0000-21.0000 sec  11.1 MBytes  93.3 Mbits/sec
[  1] 21.0000-22.0000 sec  11.2 MBytes  94.4 Mbits/sec
[  1] 22.0000-23.0000 sec  11.2 MBytes  94.4 Mbits/sec
[  1] 23.0000-24.0000 sec  11.4 MBytes  95.4 Mbits/sec
[  1] 24.0000-25.0000 sec  11.1 MBytes  93.3 Mbits/sec
[  1] 25.0000-26.0000 sec  12.4 MBytes   104 Mbits/sec
[  1] 26.0000-27.0000 sec  11.1 MBytes  93.3 Mbits/sec
[  1] 27.0000-28.0000 sec  11.2 MBytes  94.4 Mbits/sec
[  1] 28.0000-29.0000 sec  11.1 MBytes  93.3 Mbits/sec
[  1] 29.0000-30.0000 sec  11.1 MBytes  93.3 Mbits/sec
"""

# Extract time and throughput using regex
pattern = r"\[\s*\d+\]\s*([\d\.]+)-([\d\.]+)\s*sec.*?([\d\.]+)\s*Mbits/sec"
matches = re.findall(pattern, iperf_log)

times = []
throughput = []

for start, end, bw in matches:
    times.append(float(end))          # end time (x-axis)
    throughput.append(float(bw))      # throughput in Mbits/sec (y-axis)

# Plot
plt.figure(figsize=(10,5))
plt.plot(times, throughput, marker='o', linewidth=2)
plt.axvspan(2, 7, color='red', alpha=0.2, label='Link Down (2s–7s)')
plt.title('Per-Second Throughput (h1 → h2 via L3 Controller)')
plt.xlabel('Time (seconds)')
plt.ylabel('Throughput (Mbits/sec)')
plt.grid(True, linestyle='--', alpha=0.6)
plt.legend()
plt.tight_layout()
plt.show()
