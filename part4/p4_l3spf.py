import heapq
import json
import os
from collections import defaultdict

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib.packet import ethernet, ipv4, packet, arp
from ryu.lib.packet import ether_types
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event
from ryu.topology.api import get_all_switch


class L3ShortestPathDynamic(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    IDLE_TIMEOUT = 50
    HARD_TIMEOUT = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        cfg_path = kwargs.get('config_file') or os.environ.get(
            'P4_CONFIG', 'part4/p4_config.json')
        self.config = self._read_config(cfg_path)

        self.datapaths = {}
        self.port_name_map = defaultdict(dict)         # dpid -> {if_name: port_no}
        self.interface_port_map = {}                   # (switch_name, if_name) -> port_no
        self.dpid_to_name = {}
        self.name_to_dpid = {}
        self.hosts_by_ip = {}
        self.hosts_by_mac = {}
        self.host_attachment = {}                      # host_name -> {'dpid','port_no','iface'}

        self.interface_lookup = {}                     # (switch, neighbor) -> iface dict
        self.interface_by_name = {}                    # (switch, if_name) -> iface dict
        self.interface_by_ip = {}                      # ip -> (switch, iface dict)
        self.links_cfg = []                            # raw config links
        self.link_info = {}                            # (src_dpid, dst_dpid) -> dict(meta)
        self.adjacency = defaultdict(dict)             # runtime adjacency

        if self.config:
            self._ingest_config(self.config)
        else:
            self.logger.error("Missing topology config; controller will idle.")

    # ---------------- config helpers ----------------
    def _read_config(self, path):
        if not os.path.exists(path):
            self.logger.error("Config file %s not found.", path)
            return None
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception as exc:
            self.logger.error("Failed to parse %s: %s", path, exc)
            return None

    def _ingest_config(self, cfg):
        for sw in cfg.get('switches', []):
            name = sw['name']
            dpid = sw['dpid']
            self.dpid_to_name[dpid] = name
            self.name_to_dpid[name] = dpid
            for iface in sw.get('interfaces', []):
                if_name = iface['name']
                neighbor = iface.get('neighbor')
                self.interface_by_name[(name, if_name)] = iface
                if neighbor:
                    self.interface_lookup[(name, neighbor)] = iface
                if iface.get('ip'):
                    self.interface_by_ip[iface['ip']] = (name, iface)

        for host in cfg.get('hosts', []):
            self.hosts_by_ip[host['ip']] = host
            self.hosts_by_mac[host['mac']] = host

        self.links_cfg = cfg.get('links', [])

        for link in self.links_cfg:
            src, dst = link['src'], link['dst']
            cost = link.get('cost', 1)
            src_iface = self.interface_lookup.get((src, dst))
            dst_iface = self.interface_lookup.get((dst, src))
            if not src_iface or not dst_iface:
                continue
            src_dpid = self.name_to_dpid.get(src)
            dst_dpid = self.name_to_dpid.get(dst)
            if src_dpid is None or dst_dpid is None:
                continue
            self.link_info[(src_dpid, dst_dpid)] = {
                'src_name': src,
                'dst_name': dst,
                'src_if': src_iface,
                'dst_if': dst_iface,
                'weight': cost,
                'src_port': None,
                'dst_port': None,
            }
            self.link_info[(dst_dpid, src_dpid)] = {
                'src_name': dst,
                'dst_name': src,
                'src_if': dst_iface,
                'dst_if': src_iface,
                'weight': cost,
                'src_port': None,
                'dst_port': None,
            }

    def _normalize_port_name(self, port):
        if isinstance(port, bytes):
            port = port.decode('utf-8', errors='ignore')
        return port.rstrip('\x00')

    def _port_no_for_interface(self, sw_name, if_name):
        dpid = self.name_to_dpid.get(sw_name)
        if dpid is None:
            return None
        return self.port_name_map.get(dpid, {}).get(if_name)

    def _refresh_switch_interfaces(self, dpid):
        sw_name = self.dpid_to_name.get(dpid)
        if not sw_name:
            return
        for iface in self.interface_by_name.values():
            if iface['name'] and sw_name == iface.get('switch', sw_name):
                port_no = self._port_no_for_interface(sw_name, iface['name'])
                if port_no is not None:
                    self.interface_port_map[(sw_name, iface['name'])] = port_no

        for (src_dpid, dst_dpid), meta in list(self.link_info.items()):
            if src_dpid != dpid:
                continue
            src_port = self._port_no_for_interface(meta['src_name'], meta['src_if']['name'])
            dst_port = self._port_no_for_interface(meta['dst_name'], meta['dst_if']['name'])
            if src_port is not None:
                meta['src_port'] = src_port
                self.link_info[(dst_dpid, src_dpid)]['dst_port'] = src_port
            if dst_port is not None:
                meta['dst_port'] = dst_port
                self.link_info[(dst_dpid, src_dpid)]['src_port'] = dst_port

    def _populate_host_attachments(self):
        for host in self.config.get('hosts', []):
            sw_name = host.get('switch')
            host_name = host['name']
            iface = self.interface_lookup.get((sw_name, host_name))
            if not iface:
                continue
            port_no = self.interface_port_map.get((sw_name, iface['name']))
            if port_no is None:
                continue
            dpid = self.name_to_dpid.get(sw_name)
            if dpid is None:
                continue
            self.host_attachment[host_name] = {
                'dpid': dpid,
                'port_no': port_no,
                'iface': iface,
                'host': host,
            }

    def _rebuild_adjacency(self):
        adj = defaultdict(dict)
        for (src_dpid, dst_dpid), meta in self.link_info.items():
            if meta.get('src_port') is None or meta.get('dst_port') is None:
                continue
            adj[src_dpid][dst_dpid] = {
                'port': meta['src_port'],
                'weight': meta['weight'],
            }
        self.adjacency = adj
        self.logger.debug("Adjacency refreshed: %s", dict(adj))

    # ---------------- event handlers ----------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        parser = dp.ofproto_parser
        ofproto = dp.ofproto

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=0,
                                      match=match, instructions=inst))

        for port in ev.msg.ports:
            pname = self._normalize_port_name(port.name)
            self.port_name_map[dp.id][pname] = port.port_no

        self.datapaths[dp.id] = dp
        self._refresh_switch_interfaces(dp.id)
        self._populate_host_attachments()
        self._rebuild_adjacency()
        self.logger.info("Switch %s ready (table-miss installed).", dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        for sw in get_all_switch(self):
            dp = sw.dp
            name = next((cfg['name'] for cfg in self.config.get('switches', [])
                         if cfg['dpid'] == dp.id), None)
            if name:
                self.dpid_to_name[dp.id] = name
                self.name_to_dpid[name] = dp.id
        self.logger.debug("Switch mapping: %s", self.dpid_to_name)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        reason = msg.reason
        desc = msg.desc
        ofproto = dp.ofproto

        port_no = desc.port_no
        link_down = desc.state & ofproto.OFPPS_LINK_DOWN
        if reason == ofproto.OFPPR_DELETE or link_down:
            self._handle_port_down(dp.id, port_no)
        elif reason in (ofproto.OFPPR_ADD, ofproto.OFPPR_MODIFY) and not link_down:
            self._handle_port_up(dp.id, port_no)

    def _handle_port_down(self, dpid, port_no):
        affected = []
        for (src_dpid, dst_dpid), meta in self.link_info.items():
            if src_dpid == dpid and meta.get('src_port') == port_no:
                affected.append((src_dpid, dst_dpid))
        if not affected:
            return

        for src_dpid, dst_dpid in affected:
            self.adjacency[src_dpid].pop(dst_dpid, None)
            meta = self.link_info[(src_dpid, dst_dpid)]
            meta['down'] = True
            self.logger.info("Link down: %s -> %s (port %s)",
                             self.dpid_to_name.get(src_dpid, src_dpid),
                             self.dpid_to_name.get(dst_dpid, dst_dpid),
                             port_no)
            self._delete_port_flows(src_dpid, port_no)
            # symmetric
            rev_meta = self.link_info.get((dst_dpid, src_dpid))
            if rev_meta:
                rev_port = rev_meta.get('src_port')
                self.adjacency[dst_dpid].pop(src_dpid, None)
                if rev_port is not None:
                    self._delete_port_flows(dst_dpid, rev_port)

    def _handle_port_up(self, dpid, port_no):
        for (src_dpid, dst_dpid), meta in self.link_info.items():
            if src_dpid == dpid and meta.get('src_port') == port_no:
                meta.pop('down', None)
                self.adjacency[src_dpid][dst_dpid] = {
                    'port': port_no,
                    'weight': meta['weight']
                }
                rev = self.link_info.get((dst_dpid, src_dpid))
                if rev and rev.get('src_port') is not None:
                    self.adjacency[dst_dpid][src_dpid] = {
                        'port': rev['src_port'],
                        'weight': rev['weight']
                    }
                self.logger.info("Link up: %s <-> %s restored.",
                                 self.dpid_to_name.get(src_dpid, src_dpid),
                                 self.dpid_to_name.get(dst_dpid, dst_dpid))

    def _delete_port_flows(self, dpid, port_no):
        dp = self.datapaths.get(dpid)
        if dp is None:
            return
        parser = dp.ofproto_parser
        ofproto = dp.ofproto
        mod = parser.OFPFlowMod(datapath=dp,
                                command=ofproto.OFPFC_DELETE,
                                out_port=port_no,
                                out_group=ofproto.OFPG_ANY,
                                priority=0,
                                match=parser.OFPMatch())
        dp.send_msg(mod)
        self.logger.debug("Removed flows with out_port=%s on dpid=%s", port_no, dpid)

    # ---------------- packet processing ----------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        if msg.reason == dp.ofproto.OFPR_INVALID_TTL:
            return

        in_port = msg.match.get('in_port')
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if not eth or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            self._handle_arp(dp, in_port, eth, arp_pkt)
            return

        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
        if ipv4_pkt:
            self._handle_ipv4(msg, dp, in_port, ipv4_pkt)
            return

    def _handle_arp(self, datapath, in_port, eth, arp_pkt):
        if arp_pkt.opcode != arp.ARP_REQUEST:
            return
        entry = self.interface_by_ip.get(arp_pkt.dst_ip)
        if not entry:
            return
        sw_name, iface = entry
        if self.name_to_dpid.get(sw_name) != datapath.id:
            return

        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        reply = packet.Packet()
        reply.add_protocol(ethernet.ethernet(dst=eth.src,
                                             src=iface['mac'],
                                             ethertype=ether_types.ETH_TYPE_ARP))
        reply.add_protocol(arp.arp(opcode=arp.ARP_REPLY,
                                   src_mac=iface['mac'],
                                   src_ip=iface['ip'],
                                   dst_mac=eth.src,
                                   dst_ip=arp_pkt.src_ip))
        reply.serialize()

        actions = [parser.OFPActionOutput(in_port)]
        out = parser.OFPPacketOut(datapath=datapath,
                                  buffer_id=ofproto.OFP_NO_BUFFER,
                                  in_port=ofproto.OFPP_CONTROLLER,
                                  actions=actions,
                                  data=reply.data)
        datapath.send_msg(out)

    def _handle_ipv4(self, msg, datapath, in_port, ipv4_pkt):
        dst_host = self.hosts_by_ip.get(ipv4_pkt.dst)
        if not dst_host:
            return

        src_dpid = datapath.id
        dst_dpid = self.name_to_dpid.get(dst_host['switch'])
        if dst_dpid is None:
            return

        path = self._shortest_path(src_dpid, dst_dpid)
        if not path:
            self.logger.warning("No path %s -> %s", src_dpid, dst_dpid)
            return

        actions = self._install_path(path, dst_host)
        if not actions:
            return

        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(datapath=datapath,
                                  buffer_id=msg.buffer_id,
                                  in_port=in_port,
                                  actions=actions,
                                  data=data)
        datapath.send_msg(out)

        src_host = self.hosts_by_ip.get(ipv4_pkt.src)
        if src_host:
            reverse_path = self._shortest_path(dst_dpid, src_dpid)
            if reverse_path:
                self._install_path(reverse_path, src_host)

    # ---------------- path / flow helpers ----------------
    def _shortest_path(self, src, dst):
        if src == dst:
            return [src]
        dist = defaultdict(lambda: float('inf'))
        prev = {}
        dist[src] = 0
        heap = [(0, src)]
        visited = set()

        while heap:
            cost, node = heapq.heappop(heap)
            if node in visited:
                continue
            visited.add(node)
            if node == dst:
                break
            for neigh, meta in self.adjacency.get(node, {}).items():
                new_cost = cost + meta['weight']
                if new_cost < dist[neigh]:
                    dist[neigh] = new_cost
                    prev[neigh] = node
                    heapq.heappush(heap, (new_cost, neigh))

        if dst not in prev and dst != src:
            return []
        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        path.reverse()
        return path

    def _install_path(self, path, dst_host):
        actions_first = None
        dst_attach = self.host_attachment.get(dst_host['name'])
        if not dst_attach:
            self.logger.warning("Host attachment missing for %s", dst_host['name'])
            return None

        for idx, node in enumerate(path):
            dp = self.datapaths.get(node)
            if dp is None:
                continue
            parser = dp.ofproto_parser
            ofproto = dp.ofproto

            if idx == len(path) - 1:
                out_port = dst_attach['port_no']
                src_mac = dst_attach['iface']['mac']
                dst_mac = dst_host['mac']
            else:
                next_hop = path[idx + 1]
                link = self.link_info.get((node, next_hop))
                if not link or link.get('src_port') is None or link.get('dst_port') is None:
                    self.logger.warning("Missing link info %s -> %s", node, next_hop)
                    return None
                out_port = link['src_port']
                src_mac = link['src_if']['mac']
                dst_mac = link['dst_if']['mac']

            actions = [
                parser.OFPActionDecNwTtl(),
                parser.OFPActionSetField(eth_src=src_mac),
                parser.OFPActionSetField(eth_dst=dst_mac),
                parser.OFPActionOutput(out_port)
            ]
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                    ipv4_dst=dst_host['ip'])
            self._add_flow(dp, 20, match, actions,
                           idle_timeout=self.IDLE_TIMEOUT,
                           hard_timeout=self.HARD_TIMEOUT)
            if idx == 0:
                actions_first = actions
        self.logger.info("Installed path %s for %s", path, dst_host['ip'])
        return actions_first

    def _add_flow(self, datapath, priority, match, actions,
                  idle_timeout=0, hard_timeout=0):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath,
                                priority=priority,
                                match=match,
                                instructions=inst,
                                idle_timeout=idle_timeout,
                                hard_timeout=hard_timeout)
        datapath.send_msg(mod)