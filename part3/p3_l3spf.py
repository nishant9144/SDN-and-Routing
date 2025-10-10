from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.controller.ofp_event import EventOFPPacketIn
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, arp, ether_types
from ryu.ofproto.ofproto_v1_3_parser import OFPActionSetField, OFPActionDecNwTtl, OFPActionOutput, OFPMatch, OFPFlowMod, OFPPacketOut, OFPInstructionActions
import json
from collections import defaultdict
import heapq

class L3SPF(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(L3SPF, self).__init__(*args, **kwargs)
        # Parse configuration
        with open('p3_config.json') as f:
            self.config = json.load(f)

        self.host_to_info = {}
        for h in self.config['hosts']:
            ip = h['ip']
            self.host_to_info[ip] = {
                'name': h['name'],
                'switch': h['switch'],
                'mac': h['mac'],
                'connected_subnet': h['connected_subnet']
            }

        # Set attach_port for hosts
        for s in self.config['switches']:
            for intf in s['interfaces']:
                neigh = intf['neighbor']
                for ip, info in self.host_to_info.items():
                    if info['name'] == neigh:
                        self.host_to_info[ip]['attach_port'] = intf['name']
                        break

        # Switch interfaces: dpid -> {intf_name: info}
        self.switch_intfs = {}
        self.name_to_dpid = {}
        self.dpid_to_name = {}
        for s in self.config['switches']:
            name = s['name']
            dpid = s['dpid']
            self.name_to_dpid[name] = dpid
            self.dpid_to_name[dpid] = name
            intf_dict = {intf['name']: intf for intf in s['interfaces']}
            self.switch_intfs[dpid] = intf_dict

        # dpid -> {ip: (intf_name, mac)}
        self.switch_ip_to_mac_port = {}
        for dpid, intfs in self.switch_intfs.items():
            ip_map = {}
            for intf_name, info in intfs.items():
                ip_map[info['ip']] = (intf_name, info['mac'])
            self.switch_ip_to_mac_port[dpid] = ip_map

        # Port map: dpid -> {local_intf: port_no}
        self.port_map = {
            1: {'eth1': 1, 'eth2': 2, 'eth3': 3},
            2: {'eth1': 1, 'eth2': 2},
            3: {'eth1': 1, 'eth2': 2},
            4: {'eth1': 2, 'eth2': 1},
            5: {'eth1': 2, 'eth2': 1},
            6: {'eth1': 2, 'eth2': 3, 'eth3': 1}
        }

        # Build graph: name -> list[(neigh, cost)]
        self.graph = defaultdict(list)
        for link in self.config['links']:
            src = link['src']
            dst = link['dst']
            cost = link['cost']
            self.graph[src].append((dst, cost))
            self.graph[dst].append((src, cost))

        self.datapaths = {}

    def _get_intf_to_neighbor(self, sw_name, neigh):
        dpid = self.name_to_dpid[sw_name]
        intfs = self.switch_intfs[dpid]
        for intf_name, info in intfs.items():
            if info['neighbor'] == neigh:
                return intf_name
        return None

    def _get_port_no(self, dpid, intf_name):
        if intf_name is None:
            return None
        local = intf_name.split('-')[1]
        return self.port_map[dpid][local]

    def shortest_path(self, start, goal):
        if start == goal:
            return [start]
        dist = {node: float('inf') for node in self.graph}
        dist[start] = 0
        prev = {node: None for node in self.graph}
        pq = [(0, start)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, cost in self.graph[u]:
                alt = d + cost
                if alt < dist[v]:
                    dist[v] = alt
                    prev[v] = u
                    heapq.heappush(pq, (alt, v))
        if dist[goal] == float('inf'):
            return None
        path = []
        u = goal
        while u:
            path.append(u)
            u = prev[u]
        path.reverse()
        return path

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match, instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, instructions=inst)
        datapath.send_msg(mod)

    def install_path_rules(self, path, src_ip, dst_ip):
        for i in range(len(path)):
            sw_name = path[i]
            dpid = self.name_to_dpid[sw_name]
            dp = self.datapaths.get(dpid)
            if not dp:
                self.logger.warning(f"Datapath {dpid} not ready")
                return
            parser = dp.ofproto_parser
            ofproto = dp.ofproto

            # In port
            if i == 0:
                src_host = self.host_to_info[src_ip]['name']
                in_intf = self._get_intf_to_neighbor(sw_name, src_host)
                in_port_no = self._get_port_no(dpid, in_intf)
            else:
                prev_sw = path[i - 1]
                in_intf = self._get_intf_to_neighbor(sw_name, prev_sw)
                in_port_no = self._get_port_no(dpid, in_intf)

            # Out port and MACs
            if i == len(path) - 1:
                dst_host = self.host_to_info[dst_ip]['name']
                out_intf = self._get_intf_to_neighbor(sw_name, dst_host)
                out_port_no = self._get_port_no(dpid, out_intf)
                out_mac = self.host_to_info[dst_ip]['mac']
                out_if_mac = self.switch_intfs[dpid][out_intf]['mac']
            else:
                next_sw = path[i + 1]
                out_intf = self._get_intf_to_neighbor(sw_name, next_sw)
                out_port_no = self._get_port_no(dpid, out_intf)
                in_intf_next = self._get_intf_to_neighbor(next_sw, sw_name)
                out_mac = self.switch_intfs[self.name_to_dpid[next_sw]][in_intf_next]['mac']
                out_if_mac = self.switch_intfs[dpid][out_intf]['mac']

            # Match and actions
            match = parser.OFPMatch(in_port=in_port_no,
                                    eth_type=ether_types.ETH_TYPE_IP,
                                    ipv4_src=src_ip,
                                    ipv4_dst=dst_ip)
            actions = [parser.OFPActionSetField(eth_src=out_if_mac),
                       parser.OFPActionSetField(eth_dst=out_mac),
                       parser.OFPActionDecNwTtl(),
                       parser.OFPActionOutput(out_port_no)]
            self.add_flow(dp, 10, match, actions)
            self.logger.info("Installed rule on %s: in_port=%s -> out_port=%s", sw_name, in_port_no, out_port_no)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        self.datapaths[datapath.id] = datapath
        self.logger.info("Switch %s connected and table-miss flow installed", datapath.id)

        # Install table miss
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def _send_arp_reply(self, datapath, in_port, target_ip, target_mac, requester_ip, requester_mac):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        pkt_arp_reply = packet.Packet()
        pkt_arp_reply.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                                     dst=requester_mac,
                                                     src=target_mac))
        pkt_arp = arp.arp(opcode=arp.ARP_REPLY,
                          src_mac=target_mac,
                          src_ip=target_ip,
                          dst_mac=requester_mac,
                          dst_ip=requester_ip)
        pkt_arp_reply.add_protocol(pkt_arp)
        pkt_arp_reply.serialize()

        actions = [parser.OFPActionOutput(in_port)]
        out = parser.OFPPacketOut(datapath=datapath,
                                  buffer_id=ofproto.OFP_NO_BUFFER,
                                  in_port=ofproto.OFPP_CONTROLLER,
                                  actions=actions,
                                  data=pkt_arp_reply.data)
        datapath.send_msg(out)

    @set_ev_cls(EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        if not eth:
            return

        # Handle ARP
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            arp_pkt = pkt.get_protocol(arp.arp)
            if arp_pkt and arp_pkt.opcode == arp.ARP_REQUEST:
                target_ip = arp_pkt.dst_ip
                dpid = datapath.id
                if target_ip in self.switch_ip_to_mac_port[dpid]:
                    target_intf, target_mac = self.switch_ip_to_mac_port[dpid][target_ip]
                    target_port = self._get_port_no(dpid, target_intf)
                    if target_port == in_port:  # Local to this port
                        self._send_arp_reply(datapath, in_port, target_ip, target_mac, arp_pkt.src_ip, eth.src)
                        self.logger.info("ARP reply sent for %s on port %s from %s", target_ip, in_port, arp_pkt.src_ip)
                    return

        # Handle IP
        if eth.ethertype != ether_types.ETH_TYPE_IP:
            return
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if not ip_pkt:
            return

        src_ip = ip_pkt.src
        dst_ip = ip_pkt.dst
        self.logger.info("PacketIn on switch %s: src_ip=%s, dst_ip=%s", datapath.id, src_ip, dst_ip)

        if src_ip not in self.host_to_info or dst_ip not in self.host_to_info:
            return

        src_switch = self.host_to_info[src_ip]['switch']
        dst_switch = self.host_to_info[dst_ip]['switch']

        if src_switch == dst_switch:
            return  # Same subnet, not handled here

        path = self.shortest_path(src_switch, dst_switch)
        if not path:
            return

        self.logger.info("Computed path for %s -> %s: %s", src_ip, dst_ip, path)
        self.install_path_rules(path, src_ip, dst_ip)
        self.logger.info("Rules installed for %s -> %s", src_ip, dst_ip)

        # Resend packet on first hop only if this is the source switch
        sw_name = path[0]
        dpid = self.name_to_dpid[sw_name]
        if dpid != datapath.id:
            return

        src_host = self.host_to_info[src_ip]['name']
        in_intf = self._get_intf_to_neighbor(sw_name, src_host)
        next_sw = path[1]
        out_intf = self._get_intf_to_neighbor(sw_name, next_sw)
        out_port_no = self._get_port_no(dpid, out_intf)
        in_intf_next = self._get_intf_to_neighbor(next_sw, sw_name)
        out_mac = self.switch_intfs[self.name_to_dpid[next_sw]][in_intf_next]['mac']
        out_if_mac = self.switch_intfs[dpid][out_intf]['mac']

        actions = [parser.OFPActionSetField(eth_src=out_if_mac),
                   parser.OFPActionSetField(eth_dst=out_mac),
                   parser.OFPActionDecNwTtl(),
                   parser.OFPActionOutput(out_port_no)]

        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        else:
            data = None
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)