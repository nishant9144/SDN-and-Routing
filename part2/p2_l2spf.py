from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet
from ryu.topology import event
from ryu.topology.api import get_all_switch
import heapq
import json
import os
import random
from collections import defaultdict

class L2ShortestPath(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    IDLE_TIMEOUT = 50
    HARD_TIMEOUT = 0

    def __init__(self, *args, **kwargs):
        super(L2ShortestPath, self).__init__(*args, **kwargs)
        # config file passed as --config-file by ryu-manager
        self.config = self._read_config(kwargs.get('config_file'))

        self.ecmp_enabled = False
        self.node_list = []
        self.weight_matrix = []
        if self.config:
            self.ecmp_enabled = bool(self.config.get('ecmp', False))
            self.node_list = self.config.get('nodes', [])
            self.weight_matrix = self.config.get('weight_matrix', [])

        self.name_to_index = {name: i for i, name in enumerate(self.node_list)} if self.node_list else {}
        self.adjacency = defaultdict(dict)
        self.datapaths = {}
        self.mac_to_port = {} 
        self.hosts = {}
        self.dpid_to_name = {}

        self.logger.info("ShortestPath initialized. ECMP=%s", self.ecmp_enabled)

    def _read_config(self, config_file):
        path = config_file or "config.json"
        if not os.path.exists(path):
            self.logger.warning("Config file %s not found. Continuing without it.", path)
            return None
        try:
            with open(path) as f:
                cfg = json.load(f)
            return cfg
        except Exception as e:
            self.logger.error("Error reading config %s: %s", path, e)
            return None

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=0, match=match, instructions=inst)
        datapath.send_msg(mod)
        self.logger.info("[SWITCH_FEATURES] table-miss installed dpid=%s", datapath.id)

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0, hard_timeout=0, dst=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                match=match, instructions=inst,
                                idle_timeout=idle_timeout, hard_timeout=hard_timeout)
        datapath.send_msg(mod)
        if dst:
            self.logger.info("[FLOW_INSTALL] dpid=%s eth_dst=%s -> out=%s idle=%s",
                             datapath.id, dst, actions[0].port if actions else "-", idle_timeout)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if dp.id not in self.datapaths:
                self.datapaths[dp.id] = dp
                self.logger.info("Registered datapath %s", dp.id)
        elif ev.state == DEAD_DISPATCHER:
            if dp.id in self.datapaths:
                del self.datapaths[dp.id]
                self.logger.info("Unregistered datapath %s", dp.id)

    # populate dpid->name on switch enter (robust mapping)
    @set_ev_cls(event.EventSwitchEnter)
    def _switch_enter(self, ev):
        for sw in get_all_switch(self):
            dp = sw.dp
            self.datapaths[dp.id] = dp
            # sw.name is usually 's1','s2',...
            try:
                self.dpid_to_name[dp.id] = sw.name
            except Exception:
                self.dpid_to_name[dp.id] = f"s{int(dp.id)}"
        self.logger.debug("Switch enter: dpid->name map: %s", self.dpid_to_name)

    @set_ev_cls(event.EventLinkAdd)
    def _link_add(self, ev):
        link = ev.link
        src = link.src
        dst = link.dst
        src_dpid = src.dpid
        dst_dpid = dst.dpid
        src_port = src.port_no
        dst_port = dst.port_no

        # lookup weight using dpid->name -> index -> matrix
        w = self._weight_between(src_dpid, dst_dpid)
        if w is None:
            w = 1  # default weight if not in matrix

        self.adjacency[src_dpid][dst_dpid] = {'port': src_port, 'weight': w}
        self.adjacency[dst_dpid][src_dpid] = {'port': dst_port, 'weight': w}
        self.logger.info("Link added %s:%s <-> %s:%s w=%s", src_dpid, src_port, dst_dpid, dst_port, w)

    @set_ev_cls(event.EventLinkDelete)
    def _link_del(self, ev):
        link = ev.link
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        if dst_dpid in self.adjacency.get(src_dpid, {}):
            del self.adjacency[src_dpid][dst_dpid]
        if src_dpid in self.adjacency.get(dst_dpid, {}):
            del self.adjacency[dst_dpid][src_dpid]
        self.logger.info("Link removed %s <-> %s", src_dpid, dst_dpid)

    def _weight_between(self, dpid_a, dpid_b):
        if not self.weight_matrix or not self.node_list:
            return None
        name_a = self.dpid_to_name.get(dpid_a, f"s{int(dpid_a)}")
        name_b = self.dpid_to_name.get(dpid_b, f"s{int(dpid_b)}")
        ia = self.name_to_index.get(name_a)
        ib = self.name_to_index.get(name_b)
        if ia is None or ib is None:
            return None
        w = self.weight_matrix[ia][ib]
        return None if w == 0 else w

    def _dijkstra_all_predecessors(self, src):
        dist = {n: float('inf') for n in self.adjacency.keys()}
        preds = defaultdict(set)
        if src not in dist:
            # possibly isolated / not yet discovered switch
            dist[src] = 0
        dist[src] = 0
        pq = [(0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, meta in self.adjacency[u].items():
                w = meta.get('weight', 1)
                nd = d + w
                if nd < dist.get(v, float('inf')):
                    dist[v] = nd
                    preds[v] = {u}
                    heapq.heappush(pq, (nd, v))
                elif nd == dist.get(v, float('inf')):
                    preds[v].add(u)
        return dist, preds

    def _reconstruct_all_paths(self, src, dst, preds, limit_paths=50):
        paths = []
        def backtrack(curr, path):
            if len(paths) >= limit_paths:
                return
            if curr == src:
                paths.append([src] + list(reversed(path)))
                return
            for p in preds.get(curr, []):
                backtrack(p, path + [curr])
        backtrack(dst, [])
        return paths

    def _get_shortest_paths(self, src, dst, limit_paths=50):
        if src == dst:
            return [[src]]
        if src not in self.adjacency or dst not in self.adjacency:
            return []
        dist, preds = self._dijkstra_all_predecessors(src)
        if dist.get(dst, float('inf')) == float('inf'):
            return []
        return self._reconstruct_all_paths(src, dst, preds, limit_paths=limit_paths)

    def _install_path(self, path, src_mac, dst_mac):
        if not path:
            return
        # forward direction (match eth_dst -> out_port)
        for i, node in enumerate(path):
            datapath = self.datapaths.get(node)
            if datapath is None:
                continue
            parser = datapath.ofproto_parser
            ofproto = datapath.ofproto
            if i == len(path) - 1:
                # last hop -> destination host port
                dst_loc = self.hosts.get(dst_mac)
                out_port = dst_loc[1] if dst_loc and dst_loc[0] == node else ofproto.OFPP_FLOOD
            else:
                next_hop = path[i + 1]
                out_port = self.adjacency[node][next_hop]['port']
            actions = [parser.OFPActionOutput(out_port)]
            match = parser.OFPMatch(eth_dst=dst_mac)
            # use LearningSwitch timeouts
            self.add_flow(datapath, priority=1, match=match, actions=actions,
                          idle_timeout=self.IDLE_TIMEOUT, hard_timeout=self.HARD_TIMEOUT)

        # reverse direction (dst->src)
        rev = list(reversed(path))
        for i, node in enumerate(rev):
            datapath = self.datapaths.get(node)
            if datapath is None:
                continue
            parser = datapath.ofproto_parser
            ofproto = datapath.ofproto
            if i == len(rev) - 1:
                out_port = self.hosts.get(src_mac, (None, None))[1] or ofproto.OFPP_FLOOD
            else:
                next_hop = rev[i + 1]
                out_port = self.adjacency[node][next_hop]['port']
            actions = [parser.OFPActionOutput(out_port)]
            match = parser.OFPMatch(eth_dst=src_mac)
            self.add_flow(datapath, priority=1, match=match, actions=actions,
                          idle_timeout=self.IDLE_TIMEOUT, hard_timeout=self.HARD_TIMEOUT)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match.get('in_port')

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return
        src = eth.src
        dst = eth.dst

        # ignore LLDP if it shows up
        if eth.ethertype == 0x88cc:
            return

        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        # update host global mapping
        if src not in self.hosts:
            self.hosts[src] = (dpid, in_port)
            self.logger.info("Learned host %s at %s:%s", src, dpid, in_port)

        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        # broadcast -> flood
        if dst == "ff:ff:ff:ff:ff:ff":
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            data = None
            if msg.buffer_id == ofproto.OFP_NO_BUFFER:
                data = msg.data
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions, data=data)
            datapath.send_msg(out)
            return

        # If destination not learned yet -> flood
        dst_loc = self.hosts.get(dst)
        if dst_loc is None:
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            data = None
            if msg.buffer_id == ofproto.OFP_NO_BUFFER:
                data = msg.data
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions, data=data)
            datapath.send_msg(out)
            return

        src_loc = self.hosts.get(src)
        src_sw = src_loc[0]
        dst_sw = dst_loc[0]

        # compute all shortest paths and pick one (ECMP if enabled)
        paths = self._get_shortest_paths(src_sw, dst_sw)
        if not paths:
            #! This is wrong -> giving error
            self.logger.warning("No path between %s and %s", src_sw, dst_sw)
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            data = None
            if msg.buffer_id == ofproto.OFP_NO_BUFFER:
                data = msg.data
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions, data=data)
            datapath.send_msg(out)
            return

        if self.ecmp_enabled and len(paths) > 1:
            chosen = random.choice(paths)
            self.logger.info("ECMP: chosen path %s (from %d)", chosen, len(paths))
        else:
            paths.sort()
            chosen = paths[0]
            self.logger.info("Chosen path: %s", chosen)

        # install flows along chosen path
        self._install_path(chosen, src, dst)

        # forward this packet_out along chosen path from current switch
        if dpid in chosen:
            idx = chosen.index(dpid)
            if idx == len(chosen) - 1:
                out_port = self.hosts.get(dst)[1]
            else:
                next_hop = chosen[idx + 1]
                out_port = self.adjacency[dpid][next_hop]['port']
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)
