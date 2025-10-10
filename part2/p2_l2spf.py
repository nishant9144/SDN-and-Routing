from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet
from ryu.topology import event
from ryu.topology.api import get_all_switch, get_all_link
import heapq
import json
import os
import random
from collections import defaultdict

class Controller(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    IDLE_TIMEOUT = 50
    HARD_TIMEOUT = 0

    def __init__(self, *args, **kwargs):
        super(Controller, self).__init__(*args, **kwargs)
        self.config = self._read_config(kwargs.get('config_file', 'config.json'))

        # Configuration parameters
        self.ecmp_enabled = self.config.get('ecmp', False)
        self.node_list = self.config.get('nodes', [])
        self.weight_matrix = self.config.get('weight_matrix', [])
        self.name_to_index = {name: i for i, name in enumerate(self.node_list)}

        # Network state
        self.adjacency = defaultdict(dict)  # adjacency[u][v] = {'port': port_no, 'weight': w}
        self.datapaths = {}
        self.hosts = {}  # mac -> (dpid, port)
        self.dpid_to_name = {}

        self.logger.info("L2 Shortest Path Controller initialized. ECMP=%s", self.ecmp_enabled)

    def _read_config(self, config_file):
        """Read configuration file containing topology and ECMP settings."""
        if not os.path.exists(config_file):
            self.logger.warning("Config file %s not found.", config_file)
            return {}
        try:
            with open(config_file) as f:
                return json.load(f)
        except Exception as e:
            self.logger.error("Error reading config %s: %s", config_file, e)
            return {}

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Install table-miss flow entry to send unmatched packets to controller."""
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        self.logger.info("Table-miss flow installed on switch dpid=%s", datapath.id)

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0, hard_timeout=0):
        """Install a flow entry on the switch."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout
        )
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        """Track switch connections and disconnections."""
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if datapath.id not in self.datapaths:
                self.datapaths[datapath.id] = datapath
                self.adjacency.setdefault(datapath.id, {})
                self.logger.info("Switch connected: dpid=%s", datapath.id)
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                del self.datapaths[datapath.id]
                self.adjacency.pop(datapath.id, None)
                self.logger.info("Switch disconnected: dpid=%s", datapath.id)

    @set_ev_cls(event.EventSwitchEnter)
    def _switch_enter(self, ev):
        """Handle switch discovery events."""
        for sw in get_all_switch(self):
            dp = sw.dp
            self.datapaths[dp.id] = dp
            self.dpid_to_name[dp.id] = f"s{dp.id}"
            self.adjacency.setdefault(dp.id, {})
        self.logger.info("Switches discovered: %s", list(self.datapaths.keys()))

    @set_ev_cls(event.EventLinkAdd)
    def _link_add(self, ev):
        """Handle link discovery events and build adjacency list."""
        link = ev.link
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        src_port = link.src.port_no
        dst_port = link.dst.port_no

        # Get weight from configuration
        weight = self._get_link_weight(src_dpid, dst_dpid)
        
        self.adjacency[src_dpid][dst_dpid] = {'port': src_port, 'weight': weight}
        self.adjacency[dst_dpid][src_dpid] = {'port': dst_port, 'weight': weight}
        
        self.logger.info("Link added: %s:%s <-> %s:%s (weight=%s)", 
                        src_dpid, src_port, dst_dpid, dst_port, weight)

    @set_ev_cls(event.EventLinkDelete)
    def _link_del(self, ev):
        """Handle link deletion events."""
        link = ev.link
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        
        self.adjacency[src_dpid].pop(dst_dpid, None)
        self.adjacency[dst_dpid].pop(src_dpid, None)
        
        self.logger.info("Link removed: %s <-> %s", src_dpid, dst_dpid)

    def _get_link_weight(self, dpid_a, dpid_b):
        """Get link weight from configuration matrix."""
        if not self.weight_matrix or not self.node_list:
            return 1
        
        name_a = self.dpid_to_name.get(dpid_a, f"s{dpid_a}")
        name_b = self.dpid_to_name.get(dpid_b, f"s{dpid_b}")
        
        idx_a = self.name_to_index.get(name_a)
        idx_b = self.name_to_index.get(name_b)
        
        if idx_a is None or idx_b is None:
            return 1
        
        try:
            weight = self.weight_matrix[idx_a][idx_b]
            return weight if weight > 0 else 1
        except (IndexError, KeyError):
            return 1

    def _dijkstra_all_paths(self, src, dst):
        """
        Run Dijkstra's algorithm to find all shortest paths from src to dst.
        Returns list of paths, where each path is a list of dpids.
        """
        if src == dst:
            return [[src]]
        
        # Debug: check if src and dst exist in adjacency
        if src not in self.adjacency or dst not in self.adjacency:
            self.logger.error("Source %s or destination %s not in adjacency list. Adjacency: %s", 
                            src, dst, dict(self.adjacency))
            return []
        
        # Initialize distance and predecessors
        nodes = set(self.adjacency.keys())
        dist = {node: float('inf') for node in nodes}
        dist[src] = 0
        predecessors = defaultdict(set)
        
        # Priority queue: (distance, node)
        pq = [(0, src)]
        
        while pq:
            current_dist, u = heapq.heappop(pq)
            
            if current_dist > dist[u]:
                continue
            
            # Check all neighbors
            for v, edge_data in self.adjacency.get(u, {}).items():
                weight = edge_data['weight']
                new_dist = current_dist + weight
                
                if new_dist < dist[v]:
                    dist[v] = new_dist
                    predecessors[v] = {u}
                    heapq.heappush(pq, (new_dist, v))
                elif new_dist == dist[v]:
                    predecessors[v].add(u)
        
        # No path exists
        if dist[dst] == float('inf'):
            self.logger.error("No path found. Distance to dst: %s. All distances: %s", 
                            dist[dst], {k: v for k, v in dist.items() if v != float('inf')})
            return []
        
        # Reconstruct all shortest paths
        return self._reconstruct_paths(src, dst, predecessors)

    def _reconstruct_paths(self, src, dst, predecessors, limit=50):
        """Reconstruct all shortest paths from src to dst using predecessor information."""
        all_paths = []
        
        def backtrack(node, path):
            if len(all_paths) >= limit:
                return
            if node == src:
                all_paths.append([src] + list(reversed(path)))
                return
            for pred in predecessors.get(node, []):
                backtrack(pred, path + [node])
        
        backtrack(dst, [])
        return all_paths

    def _install_path(self, path, src_mac, dst_mac):
        """Install flow rules along the entire path for both directions."""
        if not path:
            return
        
        # Forward direction: src -> dst
        for i, dpid in enumerate(path):
            datapath = self.datapaths.get(dpid)
            if not datapath:
                continue
            
            parser = datapath.ofproto_parser
            
            # Determine output port
            if i == len(path) - 1:
                # Last switch: output to destination host
                dst_loc = self.hosts.get(dst_mac)
                if dst_loc and dst_loc[0] == dpid:
                    out_port = dst_loc[1]
                else:
                    continue
            else:
                # Intermediate switch: output to next switch
                next_dpid = path[i + 1]
                out_port = self.adjacency[dpid][next_dpid]['port']
            
            match = parser.OFPMatch(eth_dst=dst_mac)
            actions = [parser.OFPActionOutput(out_port)]
            self.add_flow(datapath, 1, match, actions, 
                         idle_timeout=self.IDLE_TIMEOUT, 
                         hard_timeout=self.HARD_TIMEOUT)
        
        # Reverse direction: dst -> src
        reversed_path = list(reversed(path))
        for i, dpid in enumerate(reversed_path):
            datapath = self.datapaths.get(dpid)
            if not datapath:
                continue
            
            parser = datapath.ofproto_parser
            
            # Determine output port
            if i == len(reversed_path) - 1:
                # Last switch: output to source host
                src_loc = self.hosts.get(src_mac)
                if src_loc and src_loc[0] == dpid:
                    out_port = src_loc[1]
                else:
                    continue
            else:
                # Intermediate switch: output to next switch
                next_dpid = reversed_path[i + 1]
                out_port = self.adjacency[dpid][next_dpid]['port']
            
            match = parser.OFPMatch(eth_dst=src_mac)
            actions = [parser.OFPActionOutput(out_port)]
            self.add_flow(datapath, 1, match, actions,
                         idle_timeout=self.IDLE_TIMEOUT,
                         hard_timeout=self.HARD_TIMEOUT)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        """Handle packets sent to controller."""
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match['in_port']
        
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        
        if not eth or eth.ethertype == 0x88cc:  # Ignore LLDP
            return
        
        src_mac = eth.src
        dst_mac = eth.dst
        
        # Learn source host location
        if src_mac not in self.hosts:
            self.hosts[src_mac] = (dpid, in_port)
            self.logger.info("Learned host %s at switch %s port %s", src_mac, dpid, in_port)
        
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        
        # Handle broadcast
        if dst_mac == "ff:ff:ff:ff:ff:ff":
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            self._send_packet_out(datapath, msg, in_port, actions)
            return
        
        # If destination not learned yet, flood
        if dst_mac not in self.hosts:
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            self._send_packet_out(datapath, msg, in_port, actions)
            return
        
        # Get source and destination switches
        src_switch = self.hosts[src_mac][0]
        dst_switch = self.hosts[dst_mac][0]
        
        # Compute shortest paths
        paths = self._dijkstra_all_paths(src_switch, dst_switch)
        
        if not paths:
            self.logger.warning("No path from switch %s to switch %s", src_switch, dst_switch)
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            self._send_packet_out(datapath, msg, in_port, actions)
            return
        
        # Select path based on ECMP setting
        if self.ecmp_enabled and len(paths) > 1:
            chosen_path = random.choice(paths)
            self.logger.info("ECMP enabled: Selected path %s from %d equal-cost paths", 
                           chosen_path, len(paths))
        else:
            chosen_path = paths[0]
            self.logger.info("Selected path: %s", chosen_path)
        
        # Install flow rules along the chosen path
        self._install_path(chosen_path, src_mac, dst_mac)
        
        # Forward the current packet
        if dpid in chosen_path:
            idx = chosen_path.index(dpid)
            if idx == len(chosen_path) - 1:
                out_port = self.hosts[dst_mac][1]
            else:
                next_switch = chosen_path[idx + 1]
                out_port = self.adjacency[dpid][next_switch]['port']
            
            actions = [parser.OFPActionOutput(out_port)]
            self._send_packet_out(datapath, msg, in_port, actions)

    def _send_packet_out(self, datapath, msg, in_port, actions):
        """Send a packet out from the switch."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data
        )
        datapath.send_msg(out)