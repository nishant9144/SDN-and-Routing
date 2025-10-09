#!/usr/bin/env python3
# main.py — orchestrates: build topo → start FRR/OSPF → wait → flap & iperf → (optional CLI)

import argparse, json
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from p4_topo import build, H1_IP, H2_IP
from p4_ospf import start_frr_ospf, wait_for_convergence, stop_frr, generate_meta_ospf
from pathlib import Path
import random
import time

def if_down_up(net, edge, down=True):
    """Bring both sides of a router-router link down/up."""
    ri, rj = net.get(edge["s_i"]), net.get(edge["s_j"])
    ifs = (edge["i_if"], edge["j_if"])
    action = "down" if down else "up"
    ri.cmd(f"ip link set {ifs[0]} {action}")
    rj.cmd(f"ip link set {ifs[1]} {action}")

def start_iperf(h1, h2, h1_ip, h2_ip, total_seconds, prefer_iperf3=True):
    """Start server on h2, client on h1. Returns (server_log, client_log)."""
    s_log = "h2_iperf.log"
    c_log = "h1_iperf.log"
    have_iperf3 = prefer_iperf3 and ("iperf3" in h1.cmd("which iperf3"))
    if have_iperf3:
        h2.cmd(f"iperf3 -s -1 > {s_log} 2>&1 &")
        time.sleep(0.5)
        ip = h2_ip.split("/")[0]
        h1.cmd(f"iperf3 -c {ip} -t {int(total_seconds)} -i 1 > {c_log} 2>&1 &")
    else:
        h2.cmd(f"iperf -s > {s_log} 2>&1 &")
        time.sleep(0.5)
        ip = h2_ip.split("/")[0]
        h1.cmd(f"iperf -c {ip} -t {int(total_seconds)} -i 1 > {c_log} 2>&1 &")
    return s_log, c_log

def link_flap_exp(net, e, h1_ip, h2_ip, iperf_time = 15, link_down_duration = 5, link_down_time = 2):
    """Choose distinct edges and flap them in sequence."""
    h1, h2 = net.get("h1"), net.get("h2")
    s_log, c_log = start_iperf(h1, h2, h1_ip, h2_ip, iperf_time)

    print(f"*** iperf running: client log {c_log}, server log {s_log}")

    time.sleep(link_down_time)

    key = (e["s_i"], e["s_j"], e["i_if"], e["j_if"])
    print(f"DOWN {e['s_i']}:{e['i_if']} <-> {e['s_j']}:{e['j_if']} for {link_down_duration}s")
    if_down_up(net, e, down=True) ## code to toggle the link
    time.sleep(link_down_duration)
    print(f"UP   {e['s_i']}:{e['i_if']} <-> {e['s_j']}:{e['j_if']}")
    if_down_up(net, e, down=False)

    print("*** Flaps done; waiting a few seconds for iperf to finish…")
    time.sleep(iperf_time-link_down_duration-link_down_time)

    c_out = h1.cmd(f"tail -n +1 {c_log} || true")
    s_out = h2.cmd(f"tail -n +1 {s_log} || true")
    return c_log, s_log, c_out, s_out



def main():
    ap = argparse.ArgumentParser(description="Mininet + FRR OSPF with link flaps and iperf.")
    ap.add_argument("--input-file", required=True, help="config json for OSPF")
    ap.add_argument("--subnet-start", default="10.10", help="pool start as 'A.B' (default 10.10)")
    ap.add_argument("--converge-timeout", type=int, default=120, help="Seconds to wait for initial convergence")
    ap.add_argument("--flap-iters", type=int, default=1, help="How many flap cycles")
    ap.add_argument("--stabilize", type=int, default=40, help="Seconds to wait after bringing link UP")
    ap.add_argument("--no-cli", action="store_true", help="Exit after test (no Mininet CLI)")
    ap.add_argument("--router-bw", type=int, default=10, help="bw (Mbps) for router-router links")
    ap.add_argument("--h1-bw", type=int, default=100, help="bw (Mbps) for h1↔s1 link")
    ap.add_argument("--h2-bw", type=int, default=50, help="bw (Mbps) for h2↔sN link")
    args = ap.parse_args()

    with open(args.input_file) as f:
        config = json.load(f)

    a_str, b_str = args.subnet_start.split(".")
    start_a, start_b = int(a_str), int(b_str)

    # 1) Topology
    net = build()
    meta_ospf = generate_meta_ospf(config)
    
    try:
        # 2) FRR
        start_frr_ospf(net, meta_ospf)

        # 3) Convergence
        print(f"*** Waiting for OSPF convergence (<= {args.converge_timeout}s)…")
        ok = wait_for_convergence(net, meta_ospf, timeout=args.converge_timeout)
        if ok:
            print("✅ OSPF converged (routes present)")
        else:
            print("⚠️  OSPF did not converge within timeout; continuing anyway.")

        # 4) Link flap experiment
        e = None
        for x in meta_ospf["edges"]:
            if x["s_i"] == "s2" and x["s_j"] == "s3":
                e = x
        c_log, s_log, c_out, s_out = link_flap_exp(
            net, e, h1_ip=H1_IP, h2_ip=H2_IP)

        print("\n==== iperf CLIENT (h1) ====\n" + c_out)
        print("\n==== iperf SERVER (h2) ====\n" + s_out)

        # 5) Optional CLI for inspection
        if not args.no_cli:
            print("\n*** Examples:")
            print("  s1 ip route")
            print("  s2 vtysh -c 'show ip ospf neighbor'")
            print("  h1 ping -c 3 h2")
            CLI(net)
    finally:
        # Cleanup
        stop_frr(net, meta_ospf)
        net.stop()

if __name__ == "__main__":
    setLogLevel('info')
    main()
