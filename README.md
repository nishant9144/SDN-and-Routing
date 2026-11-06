# SDN and Routing

> Ryu (OpenFlow 1.3) controllers on Mininet, built up from a hub and a learning switch to Dijkstra-based L2 and L3 routing with link-failure recovery, plus a comparison against OSPF running on FRR.

---

## Table of Contents

- [Overview](#overview)
- [Part 1: Hub vs Learning Switch](#part-1-hub-vs-learning-switch)
- [Part 2: L2 Shortest-Path Forwarding with ECMP](#part-2-l2-shortest-path-forwarding-with-ecmp)
- [Part 3: L3 Shortest-Path Routing](#part-3-l3-shortest-path-routing)
- [Part 4: SDN vs OSPF Under Link Failure](#part-4-sdn-vs-ospf-under-link-failure)
- [Tech Stack](#tech-stack)
- [Setup & Run](#setup--run)
- [Project Structure](#project-structure)

---

## Overview

This repo contains the code for Assignment 3, *An Introduction to the World of SDN*, in COL 334/672 (Diwali '25). On the SDN side, every part uses a custom **Ryu** app as a remote controller for an **Open vSwitch** topology in **Mininet**. Each part adds one layer:

| Part | Controller | What it adds |
|------|------------|--------------|
| 1 | `p1_hub.py`, `p1_learning.py` | Forwarding done by the controller vs forwarding by flow rules on the switch |
| 2 | `p2_l2spf.py` | Dijkstra over a weighted switch graph, with optional ECMP |
| 3 | `p3_l3spf.py` | Routing across subnets: ARP replies, MAC rewriting, TTL decrement |
| 4 | `p4_l4spf.py` + FRR/OSPF scripts | Recovery after a link fails, compared with OSPF |

---

## Part 1: Hub vs Learning Switch

Topology (`p1_topo.py`): two switches with two hosts on each (h1, h2 on s1; h3, h4 on s2).

- **Hub (`p1_hub.py`):** the only rule it installs is the table-miss entry, so every packet goes to the controller. The controller learns MAC-to-port mappings in its own table and sends a `PacketOut` to the known port, or floods if it doesn't know the port yet. It never installs forwarding rules on the switches.
- **Learning switch (`p1_learning.py`):** learns the same way but installs an `eth_dst` → output-port flow (priority 1, idle timeout 100 s). Once those rules exist, traffic between known hosts stays on the switch and never reaches the controller.

Both apps log decisions and MAC-table snapshots to files in `part1/` (`controller.log` and `mac_table.log` for the hub, `learning_*.log` for the learning switch).

---

## Part 2: L2 Shortest-Path Forwarding with ECMP

Topology (`p2_topo.py`): six switches in two branches between h1 (on s1) and h2 (on s6). Inter-switch links are 10 Mbit/s `TCLink`s.

- **Graph:** Ryu's link discovery (`--observe-links`) supplies the links. Each link's cost comes from `weight_matrix` in `config.json`, looked up by node name (`s<dpid>`).
- **Dijkstra (implemented by hand with `heapq`)** records every equal-cost predecessor, so the controller can rebuild all shortest paths, not just one. Both s1→s2→s4→s6 and s1→s3→s5→s6 cost 40.
- **ECMP (`"ecmp": true`):** a switch with more than one shortest-path out-port gets an OpenFlow **select group** with one equal-weight bucket per port, and the switch chooses a bucket. With `"ecmp": false` the controller installs only the first path. The reverse direction is installed at the same time in both modes. Rules match `eth_dst` (priority 10, idle timeout 50 s).

---

## Part 3: L3 Shortest-Path Routing

Topology (`p3_topo.py`): a six-switch ring with h1 (10.0.12.0/24) on s1 and h2 (10.0.67.0/24) on s6. Each link is its own /24. The script fixes DPIDs and port numbers so they match the config.

- **Config-driven:** `p3_config.json` lists the hosts, each switch's interfaces (IP, MAC, subnet, neighbor) and the link costs. A switch's port number is its interface's position in that list.
- **ARP responder:** the controller answers ARP requests itself, using the MACs of the gateways and hosts from the config.
- **Router behaviour:** for each IPv4 flow the controller runs Dijkstra between the source and destination switches. It then installs `ipv4_dst` rules on every hop that **rewrite `eth_src`/`eth_dst` to that hop's interface MACs, decrement the TTL** and forward the packet. Packets that reach the controller with TTL ≤ 1 are dropped.
- **Link events:** the controller handles `PortStatus`. When an inter-switch port goes down it removes that edge from the graph, and when the port comes back up it restores the edge's cost. Either way it deletes all flows, and they are installed again when the next packets arrive.

---

## Part 4: SDN vs OSPF Under Link Failure

The experiment runs a 30 s iperf from h1 to h2. Two seconds in, the s1–s2 link goes down for 5 s. The two setups use the same ring, which now has unequal bandwidth: s2–s3 at 100 Mbit/s and s5–s4 at 10 Mbit/s. With the costs in `p4_config.json`, the primary path s1→s2→s3→s6 costs 50 and the detour s1→s4→s5→s6 costs 60.

**SDN side**
- `p4_l4spf.py` is the Part 3 controller with link-failure handling added. It keeps a list of active flows. When a port goes down or comes up it clears all switches, runs Dijkstra again for each active flow, installs the new path straight away, and logs `*** CONVERGENCE TIME: ... ***`.
- `p4_topo_2.py` is the OVS topology with the bandwidth limits listed above.

**OSPF side** (built on the course's starter code)
- `p4_topo.py`: the six "switches" are Linux-router Mininet hosts with IP forwarding turned on.
- `p4_ospf.py`: writes a `zebra.conf` and `ospfd.conf` for each router and starts the FRR daemons. OSPF interface costs come from the `links` section of the config. Interfaces are point-to-point with a 2 s hello and 6 s dead interval, `maximum-paths 1` turns off ECMP, and the host-facing interfaces are passive.
- `p4_runner.py`: builds the network and starts FRR. It waits until OSPF routes to both host subnets appear, then runs the link-flap + iperf experiment (`h1_iperf.log` and `h2_iperf.log`; uses iperf3 if it's installed) and opens the Mininet CLI.
- `plotter.py`: plots per-second throughput from an iperf log that is pasted into the script, and shades the 2–7 s link-down window. The trace in the file is labelled *h1 → h2 via L3 Controller*. It shows about 96–100 Mbit/s before the failure, almost nothing from t = 2 s to 5 s, about 10–14 Mbit/s from t = 6 s to 13 s, and 93–104 Mbit/s again from t = 14 s.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| SDN controller | Ryu, OpenFlow 1.3 (Python 3) |
| Emulation | Mininet + Open vSwitch, `TCLink` bandwidth limits |
| Traditional routing | FRRouting (zebra + ospfd) |
| Traffic / measurement | iperf / iperf3, `ovs-ofctl dump-flows`, `vtysh` |
| Plotting | Matplotlib |

---

## Setup & Run

**Prerequisites:** Mininet with Open vSwitch, [Ryu](https://ryu.readthedocs.io/en/latest/getting_started.html) and iperf. Part 4 also needs FRR and IPv4 forwarding:

```bash
sudo apt-get install -y frr frr-pythontools
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-ipforward.conf
sudo sysctl --system
```

The OVS topology scripts (Parts 1–3 and `p4_topo_2.py`) connect to a remote controller at `127.0.0.1:6633`. The controllers load their config from a **relative path**, so start each one from inside its part's folder. Run the controller in one terminal and the topology in another. Run `sudo mn -c` between runs to clean up.

```bash
# Part 1: hub or learning switch
cd part1 && ryu-manager p1_hub.py          # or: ryu-manager p1_learning.py
cd part1 && sudo python3 p1_topo.py
# mininet> pingall
# mininet> iperf h1 h3
# mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s2

# Part 2: L2 shortest path (set "ecmp" in config.json to true or false)
cd part2 && ryu-manager --observe-links p2_l2spf.py
cd part2 && sudo python3 p2_topo.py
# mininet> h2 iperf -s &
# mininet> h1 iperf -c h2 -t 10 -P 2

# Part 3: L3 shortest path
cd part3 && ryu-manager p3_l3spf.py
cd part3 && sudo python3 p3_topo.py
# mininet> h1 ping -c 5 h2

# Part 4 (SDN): L3 controller with link-failure handling
cd part4 && ryu-manager p4_l4spf.py
cd part4 && sudo python3 p4_topo_2.py

# Part 4 (OSPF): build, converge, flap s1-s2, run iperf (one command)
cd part4 && sudo mn -c && sudo python3 p4_runner.py --input-file=p4_config.json
#   optional: --converge-timeout 60   --no-cli
# mininet> s1 vtysh -c "show ip ospf neighbor"
# mininet> s1 vtysh -c "show ip route"

# Plot an iperf trace (paste the log into plotter.py first)
python3 part4/plotter.py
```

There is no script for flapping a link in the SDN run. You do it by hand from the Mininet CLI, for example `link s1 s2 down` and later `link s1 s2 up`.

---

## Project Structure

```
.
├── COL334_A3_2501 .pdf     # Assignment specification
├── material.txt            # Working notes: Ryu/Mininet commands, FRR install steps
├── part1/
│   ├── p1_hub.py           # Hub controller (MAC table in the controller, PacketOut only)
│   ├── p1_learning.py      # Learning switch (installs eth_dst flow rules)
│   └── p1_topo.py          # 2 switches, 4 hosts
├── part2/
│   ├── p2_l2spf.py         # L2 Dijkstra + ECMP via OpenFlow select groups
│   ├── config.json         # Switch list, weight matrix, ECMP flag
│   └── p2_topo.py          # 6-switch two-branch topology
├── part3/
│   ├── p3_l3spf.py         # L3 Dijkstra, ARP responder, MAC rewrite + TTL decrement
│   ├── p3_config.json      # Hosts, interfaces/subnets, link costs
│   └── p3_topo.py          # Six-switch ring with fixed DPIDs/ports
└── part4/
    ├── p4_l4spf.py         # L3 controller + link-failure recovery and convergence log
    ├── p4_topo_2.py        # OVS topology for the SDN experiment
    ├── p4_topo.py          # Linux-router topology for OSPF
    ├── p4_ospf.py          # FRR zebra/ospfd config + convergence check
    ├── p4_runner.py        # OSPF link-flap + iperf experiment
    ├── p4_config.json      # Part 4 hosts, interfaces and link costs
    ├── plotter.py          # Per-second throughput plot
    └── p4_readme.md        # OSPF setup notes
```

---

Coursework for COL 334/672, Assignment 3.
