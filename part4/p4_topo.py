#!/usr/bin/env python3
# topo.py — Mininet topology + host/router IP setup

import json
from itertools import count
from pathlib import Path
from mininet.net import Mininet
from mininet.node import Host
from mininet.link import TCLink
from mininet.log import setLogLevel, info

H1_IP = '10.0.12.2/24'
H2_IP = '10.0.67.2/24'

class LinuxRouter(Host):
    def config(self, **params):
        super().config(**params)
        self.cmd("sysctl -w net.ipv4.ip_forward=1")
 
    def terminate(self):
        self.cmd('killall zebra ospfd')
        super(Host, self).terminate()

def flush_set(node, intf, cidr):
    node.cmd(f"ip addr flush dev {intf}")
    node.cmd(f"ip addr add {cidr} dev {intf}")
    node.cmd(f"ip link set {intf} up")

def set_if(node, ifname, ip_cidr=None, mac=None):
    node.cmd(f'ip addr flush dev {ifname}')
    if mac:
        node.cmd(f'ip link set dev {ifname} address {mac}')
    if ip_cidr:
        node.cmd(f'ip addr add {ip_cidr} dev {ifname}')
    node.cmd(f'ip link set {ifname} up')


def build():
    net = Mininet(
        controller=None, build=False, link=TCLink,
        autoSetMacs=False, autoStaticArp=False
    )

    n = 6
    routers = [net.addHost(f"s{i+1}", cls=LinuxRouter, inNamespace=True) for i in range(n)]


    info('*** Add hosts\n')
    h1 = net.addHost('h1', ip='10.0.12.2/24', mac='00:00:00:00:01:02', inNamespace=True)
    h2 = net.addHost('h2', ip='10.0.67.2/24', mac='00:00:00:00:06:02', inNamespace=True)

    info('*** Host <-> switch links (fixed names)\n')
    # attach hosts to the routers
    link_h1 = net.addLink(h1, routers[0], intfName1='h1-eth1', intfName2='s1-eth1', bw=100)
    link_h2 = net.addLink(h2, routers[-1], intfName1='h2-eth1', intfName2='s6-eth3', bw=100)
    
    
    info('*** Inter-switch ring links (fixed names)\n')
    net.addLink(routers[0], routers[1], intfName1='s1-eth2', intfName2='s2-eth1')  # 10.0.13.0/24
    net.addLink(routers[1], routers[2], intfName1='s2-eth2', intfName2='s3-eth1', bw=100)  # 10.0.23.0/24
    net.addLink(routers[2], routers[5], intfName1='s3-eth2', intfName2='s6-eth1')  # 10.0.36.0/24
    net.addLink(routers[5], routers[4], intfName1='s6-eth2', intfName2='s5-eth2')  # 10.0.56.0/24
    net.addLink(routers[4], routers[3], intfName1='s5-eth1', intfName2='s4-eth2', bw=10)  # 10.0.45.0/24
    net.addLink(routers[3], routers[0], intfName1='s4-eth1', intfName2='s1-eth3')  # 10.0.14.0/24

    info('*** Build & start\n')
    net.build()
    net.start()

    
    info('*** Assign gateway IPs/MACs on host-facing switch ports\n')
    set_if(routers[0], 's1-eth1', ip_cidr='10.0.12.1/24', mac='00:00:00:00:01:01')  # GW for h1
    set_if(routers[5], 's6-eth3', ip_cidr='10.0.67.1/24', mac='00:00:00:00:06:03')  # GW for h2

    info('*** Assign IPs/MACs on ALL inter-switch links (per config)\n')
    # s1 <-> s2 (10.0.13.0/24)
    set_if(routers[0], 's1-eth2', ip_cidr='10.0.13.1/24', mac='00:00:00:00:01:04')
    set_if(routers[1], 's2-eth1', ip_cidr='10.0.13.2/24', mac='00:00:00:00:02:01')

    # s2 <-> s3 (10.0.23.0/24)
    set_if(routers[1], 's2-eth2', ip_cidr='10.0.23.1/24', mac='00:00:00:00:02:02')
    set_if(routers[2], 's3-eth1', ip_cidr='10.0.23.2/24', mac='00:00:00:00:03:01')

    # s3 <-> s6 (10.0.36.0/24)
    set_if(routers[2], 's3-eth2', ip_cidr='10.0.36.1/24', mac='00:00:00:00:03:02')
    set_if(routers[5], 's6-eth1', ip_cidr='10.0.36.2/24', mac='00:00:00:00:06:01')

    # s6 <-> s5 (10.0.56.0/24)
    set_if(routers[5], 's6-eth2', ip_cidr='10.0.56.2/24', mac='00:00:00:00:06:04')
    set_if(routers[4], 's5-eth2', ip_cidr='10.0.56.1/24', mac='00:00:00:00:05:02')

    # s5 <-> s4 (10.0.45.0/24)
    set_if(routers[4], 's5-eth1', ip_cidr='10.0.45.2/24', mac='00:00:00:00:05:01')
    set_if(routers[3], 's4-eth2', ip_cidr='10.0.45.1/24', mac='00:00:00:00:04:02')

    # s4 <-> s1 (10.0.14.0/24)
    set_if(routers[3], 's4-eth1', ip_cidr='10.0.14.2/24', mac='00:00:00:00:04:01')
    set_if(routers[0], 's1-eth3', ip_cidr='10.0.14.1/24', mac='00:00:00:00:01:03')


    info('*** Configure hosts: IP/MAC + default routes\n')
    r1_if_h = link_h1.intf2 if link_h1.intf1.node == h1 else link_h1.intf1
    h1_if   = link_h1.intf1 if link_h1.intf2 == r1_if_h else link_h1.intf2
    rn_if_h = link_h2.intf2 if link_h2.intf1.node == h2 else link_h2.intf1
    h2_if   = link_h2.intf1 if link_h2.intf2 == rn_if_h else link_h2.intf2
    
    r1_h_ip, h1_ip, rn_h_ip, h2_ip = ('10.0.12.1/24', '10.0.12.2/24', '10.0.67.1/24', '10.0.67.2/24')
    flush_set(routers[0], r1_if_h.name, r1_h_ip)
    flush_set(h1, h1_if.name, h1_ip)
    flush_set(routers[-1], rn_if_h.name, rn_h_ip)
    flush_set(h2,  h2_if.name,  h2_ip)
    
    h1_gw = r1_h_ip.split('/')[0]; h2_gw = rn_h_ip.split('/')[0]
    h1.cmd(f"ip route replace default via {h1_gw}")
    h2.cmd(f"ip route replace default via {h2_gw}")
    
    return net
