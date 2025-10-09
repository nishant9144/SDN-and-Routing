#!/usr/bin/env python3
# frr_ospf.py — FRR (zebra + ospfd) config/start/stop + convergence wait

import time

FRR_BIN_ZEBRA = "/usr/lib/frr/zebra"
FRR_BIN_OSPFD = "/usr/lib/frr/ospfd"

def _iface_by_ip(n, ip_cidr):
    # ip_cidr like "10.255.1.1/24"
    cmd = f"ip -o -4 addr show | awk '$4==\"{ip_cidr}\" {{print $2}}'"
    return n.cmd(cmd).strip()

def start_frr_ospf(net, meta, host_if_cost=1):
    # Build interface cost map per router
    if_costs = {r: {} for r in meta["routers"]}
    for e in meta["edges"]:
        if_costs[e["s_i"]][e["i_if"]] = int(e["cost"])
        if_costs[e["s_j"]][e["j_if"]] = int(e["cost"])

    r1_if_ip = meta["host_links"]["s1_if_ip"]
    rn_if_ip = meta["host_links"]["sn_if_ip"]

    for rname in meta["routers"]:
        n = net.get(rname)
        n.cmd(f"rm -rf /tmp/{rname} && install -d -m 0777 /tmp/{rname}/run /tmp/{rname}/log")

        r1_if_name = _iface_by_ip(n, r1_if_ip)
        rn_if_name = _iface_by_ip(n, rn_if_ip)

        n.cmd(f"bash -lc \"cat > /tmp/{rname}/zebra.conf <<'EOF'\n"
              f"log file /tmp/{rname}/log/zebra.log\n"
              f"EOF\"")

        # Minimal ospfd.conf with per-interface costs
        n.cmd(f"bash -lc \"cat > /tmp/{rname}/ospfd.conf <<'EOF'\n"
              f"log file /tmp/{rname}/log/ospfd.log\n"
              f"router ospf\n"
              f" router-id {int(rname[1:])}.{int(rname[1:])}.{int(rname[1:])}.{int(rname[1:])}\n"
              f" network 10.0.0.0/8 area 0\n"
              f" network 10.255.0.0/16 area 0\n"
              f"!\n"
              + "\n".join(
                    f"interface {ifn}\n ip ospf cost {cost}\n!"
                    for ifn, cost in if_costs[rname].items()
                ) + "\n"
              + (f"interface {r1_if_name}\n ip ospf cost {host_if_cost}\n ip ospf passive\n!\n"
                 if r1_if_name else "")
              + (f"interface {rn_if_name}\n ip ospf cost {host_if_cost}\n ip ospf passive\n!\n"
                 if rn_if_name else "")
              + "line vty\n exec-timeout 0 0\n login\n!\n"
              + "EOF\"")

        # Start daemons in the netns
        n.cmd(f"{FRR_BIN_ZEBRA} -d -f /tmp/{rname}/zebra.conf "
              f"-i /tmp/{rname}/run/zebra.pid -z /tmp/{rname}/run/zserv.api")
        n.cmd(f"{FRR_BIN_OSPFD} -d -f /tmp/{rname}/ospfd.conf "
              f"-i /tmp/{rname}/run/ospfd.pid -z /tmp/{rname}/run/zserv.api")

    print('*** FRR (zebra+ospfd) started on all routers; waiting a bit…')
    time.sleep(3)

def wait_for_convergence(net, meta, timeout=120, poll=1.0):
    """Convergence proxy: r1 has OSPF route to h2/24 AND rN has route to h1/24."""
    r1_dst = meta["host_links"]["h2_ip"].rsplit('.', 1)[0] + ".0/24"
    rN_dst = meta["host_links"]["h1_ip"].rsplit('.', 1)[0] + ".0/24"
    deadline = time.time() + timeout
    r1 = net.get("s1")
    rN = net.get(meta["routers"][-1])

    while time.time() < deadline:
        r1_ok = r1_dst in r1.cmd("ip route | grep 'proto ospf' || true")
        rN_ok = rN_dst in rN.cmd("ip route | grep 'proto ospf' || true")
        if r1_ok and rN_ok:
            return True
        time.sleep(poll)
    return False

def stop_frr(net, meta):
    """Best-effort cleanup; daemons die anyway when netns is removed via net.stop()."""
    for rname in meta["routers"]:
        n = net.get(rname)
        # Try pid files first; otherwise pkill in that namespace.
        n.cmd("for p in /tmp/{}/run/*.pid; do "
              "[ -f \"$p\" ] && kill $(cat \"$p\") 2>/dev/null || true; done".format(rname))
        n.cmd("pkill -9 zebra || true")
        n.cmd("pkill -9 ospfd || true")

def find_switch_info(switches, s_name, neighbor):
    for x in switches:
        if x["name"] == s_name:
            for intf in x["interfaces"]:
                if intf["neighbor"] == neighbor:
                    return intf
            
            
def generate_meta_ospf(conf):
    switches = conf["switches"]
    links = conf["links"]
    routers = [switch["name"] for switch in switches]
    edges = []
    for link in links:
        s_i = link["src"]
        s_j = link["dst"]
        intf_info_i = find_switch_info(switches, s_i, s_j)
        intf_info_j = find_switch_info(switches, s_j, s_i)
        edges.append({
                        "s_i": s_i,
                        "s_j": s_j,
                        "i_if": intf_info_i["name"],
                        "j_if": intf_info_j["name"],
                        "subnet": intf_info_i["subnet"],
                        "ip_i": intf_info_i["ip"]+"/24",
                        "ip_j": intf_info_j["ip"]+"/24",
                        "cost": link["cost"]
                    })
    hosts = conf["hosts"]
    r1_h_ip = find_switch_info(switches, switches[0]["name"], "h1")["ip"]
    rn_h_ip = find_switch_info(switches, switches[-1]["name"], "h2")["ip"]
    host_links =  {
        "h1_ip": f"{hosts[0]['ip']}/24", "s1_if_ip": f'{r1_h_ip}/24',
        "h2_ip": f"{hosts[-1]['ip']}/24", "sn_if_ip": f'{rn_h_ip}/24'
    }
    meta = {"routers": routers, "edges": edges, "host_links": host_links}
    return meta