from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4
import json
import heapq
from collections import defaultdict
import ipaddress

class Controller(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(Controller, self).__init__(*args, **kwargs)
        
        #* weight_matrix
        self.topology = {}         # {switch_name: {neighbor_name: cost}}
        self.original_topology = {}# backup of costs for link-up restore
        self.dpid_to_name = {}     # {dpid: switch_name}
        self.name_to_dpid = {}     # {switch_name: dpid}
        
        self.switch_interfaces = {} # {switch_name: {neighbor: {ip, mac, subnet, ...}}}
        self.switch_subnets = {}    # {switch_name: [subnet_objects]}
        self.host_info = {}         # {host_ip: {mac, switch, subnet, name}}
        self.datapaths = {}         # {dpid: datapath}
        self.arp_table = {}         # {ip: mac}
        self.switch_ports = {}      # {switch_name: {neighbor_name: port_number}}
        self.port_to_neighbor = {}  # {switch_name: {port_no: neighbor_name}}
        self.failed_links = set()   # {(sA, sB)} where edge is currently down

        self.load_config('p3_config.json')
        self.logger.info("L3 Shortest Path Controller initialized")

    def load_config(self, config_file):
        """Load network configuration robustly from the JSON file."""
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
            
            # 1. Load host information and populate ARP table
            if 'hosts' in config:
                for host in config['hosts']:
                    self.host_info[host['ip']] = host
                    self.arp_table[host['ip']] = host['mac']
            
            # 2. Load switch interfaces and build topology at the same time
            if 'switches' in config:
                for switch in config['switches']:
                    sw_name = switch['name']
                    sw_dpid = int(switch['dpid'])
                    
                    self.dpid_to_name[sw_dpid] = sw_name
                    self.name_to_dpid[sw_name] = sw_dpid
                    
                    # Initialize data structures for this switch
                    self.topology.setdefault(sw_name, {})
                    self.switch_interfaces.setdefault(sw_name, {})
                    self.switch_ports.setdefault(sw_name, {})
                    self.port_to_neighbor.setdefault(sw_name, {})
                    
                    for idx, interface in enumerate(switch['interfaces']):
                        neighbor = interface['neighbor']
                        port = idx + 1
                        
                        self.switch_interfaces[sw_name][neighbor] = interface
                        self.switch_ports[sw_name][neighbor] = port
                        self.port_to_neighbor[sw_name][port] = neighbor
                        self.arp_table[interface['ip']] = interface['mac']
                        if neighbor.startswith('s'):
                            self.topology[sw_name][neighbor] = 1 # Default cost

            # 3. Use the 'links' section ONLY to update costs
            if 'links' in config:
                for link in config['links']:
                    src, dst, cost = link['src'], link['dst'], link['cost']
                    if src in self.topology and dst in self.topology[src]:
                        self.topology[src][dst] = cost
                    if dst in self.topology and src in self.topology[dst]:
                        self.topology[dst][src] = cost

            # Keep a backup for restoring on link-up
            self.original_topology = {sw: dict(nei) for sw, nei in self.topology.items()}
            
            self.logger.info("Configuration loaded successfully.")

        except Exception as e:
            self.logger.error(f"Error loading config: {e}")

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        
        self.datapaths[dpid] = datapath

        # Install table-miss flow entry
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        self.logger.info("Table-miss flow installed on switch dpid=%s", dpid)

    # -------- Link up/down detection --------
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        dpid = dp.id
        sw_name = self.dpid_to_name.get(dpid, f"unknown-{dpid}")
        port_no = msg.desc.port_no

        # DOWN/DELETE
        if (msg.desc.state & ofp.OFPPS_LINK_DOWN) or msg.reason == ofp.OFPPR_DELETE:
            self.logger.warning(f"Port {port_no} on {sw_name} DOWN/DELETED")
            self.handle_link_down(dpid, port_no)
        # UP/ADD/MOD (and not LINK_DOWN)
        elif msg.reason in (ofp.OFPPR_ADD, ofp.OFPPR_MODIFY) and not (msg.desc.state & ofp.OFPPS_LINK_DOWN):
            self.logger.info(f"Port {port_no} on {sw_name} UP/READY")
            self.handle_link_up(dpid, port_no)

    def handle_link_down(self, dpid, port_no):
        """Remove failed switch-to-switch edge from topology and flush flows."""
        sw_name = self.dpid_to_name.get(dpid)
        if not sw_name or sw_name not in self.port_to_neighbor:
            return

        neighbor = self.port_to_neighbor[sw_name].get(port_no)
        if not neighbor or not neighbor.startswith('s'):
            return  # host-facing change; ignore here

        self.logger.warning(f"LINK DOWN: {sw_name} <-> {neighbor}")

        # remove both directions in the live topology
        if neighbor in self.topology.get(sw_name, {}):
            del self.topology[sw_name][neighbor]
        if sw_name in self.topology.get(neighbor, {}):
            del self.topology[neighbor][sw_name]

        self.failed_links.add((sw_name, neighbor))
        self.failed_links.add((neighbor, sw_name))

        self._flush_all_datapaths()

    def handle_link_up(self, dpid, port_no):
        """Restore edge cost from original_topology and flush flows."""
        sw_name = self.dpid_to_name.get(dpid)
        if not sw_name or sw_name not in self.port_to_neighbor:
            return

        neighbor = self.port_to_neighbor[sw_name].get(port_no)
        if not neighbor or not neighbor.startswith('s'):
            return

        # only proceed if we know this edge in the original topo or had failed it
        if (sw_name, neighbor) not in self.failed_links and \
           (neighbor, sw_name) not in self.failed_links and \
           not (sw_name in self.original_topology and neighbor in self.original_topology[sw_name]):
            return

        self.logger.info(f"LINK UP: {sw_name} <-> {neighbor}")

        # restore both directions from backup (if present)
        if sw_name in self.original_topology and neighbor in self.original_topology[sw_name]:
            cost = self.original_topology[sw_name][neighbor]
            self.topology.setdefault(sw_name, {})[neighbor] = cost
        if neighbor in self.original_topology and sw_name in self.original_topology[neighbor]:
            cost = self.original_topology[neighbor][sw_name]
            self.topology.setdefault(neighbor, {})[sw_name] = cost

        self.failed_links.discard((sw_name, neighbor))
        self.failed_links.discard((neighbor, sw_name))

        self._flush_all_datapaths()

    def _flush_all_datapaths(self):
        """Delete all flows on all switches and reinstall table-miss."""
        for dpid, dp in self.datapaths.items():
            self.clear_flows(dp)
        for dpid, dp in self.datapaths.items():
            parser = dp.ofproto_parser
            ofp = dp.ofproto
            match = parser.OFPMatch()
            actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
            self.add_flow(dp, 0, match, actions)
        self.logger.info("Flushed flows and restored table-miss on all switches")

    def clear_flows(self, datapath):
        """Delete all flows on a switch."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        mod = parser.OFPFlowMod(datapath=datapath,
                                command=ofproto.OFPFC_DELETE,
                                out_port=ofproto.OFPP_ANY,
                                out_group=ofproto.OFPG_ANY,
                                match=parser.OFPMatch())
        datapath.send_msg(mod)
        self.logger.info(f"Cleared flows on {self.dpid_to_name.get(datapath.id)}")

    # -------- Flow programming & routing --------

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0, hard_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                match=match, instructions=inst,
                                idle_timeout=idle_timeout,
                                hard_timeout=hard_timeout)
        datapath.send_msg(mod)

    def dijkstra(self, src, dst):
        if src not in self.topology or dst not in self.topology:
            self.logger.warning(f"Source {src} or destination {dst} not in topology")
            return None
        
        distances = {node: float('inf') for node in self.topology}
        distances[src] = 0
        previous = {node: None for node in self.topology}
        pq = [(0, src)]
        visited = set()
        
        while pq:
            curr_dist, curr = heapq.heappop(pq)
            if curr in visited:
                continue
            visited.add(curr)
            if curr == dst:
                break
            for neighbor, cost in self.topology[curr].items():
                distance = curr_dist + cost
                if distance < distances[neighbor]:
                    distances[neighbor] = distance
                    previous[neighbor] = curr
                    heapq.heappush(pq, (distance, neighbor))
        
        # Reconstruct path
        if distances[dst] == float('inf'):
            return None
        
        path = []
        curr = dst
        while curr is not None:
            path.append(curr)
            curr = previous[curr]
        path.reverse()
        return path

    def get_switch_for_host(self, ip):
        """Get the switch to which a host is connected"""
        if ip in self.host_info:
            return self.host_info[ip]['switch']
        return None

    def get_interface_for_neighbor(self, switch, neighbor):
        """Get interface information for a neighbor"""
        if switch in self.switch_interfaces and neighbor in self.switch_interfaces[switch]:
            return self.switch_interfaces[switch][neighbor]
        return None

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']
        dpid = datapath.id
        
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        # guard: if ethernet header not parsed, ignore
        if eth is None:
            return
        
        if eth.ethertype == 0x0806:  # ARP
            self.handle_arp(datapath, in_port, pkt, eth)
        elif eth.ethertype == 0x0800:  # IPv4
            self.handle_ipv4(datapath, in_port, pkt, eth, msg)

    def handle_arp(self, datapath, in_port, pkt, eth):
        """Handle ARP packets by acting as a gateway."""
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt is None:
            return

        # Learn the MAC address of the sender
        if arp_pkt.src_ip not in self.arp_table:
            self.arp_table[arp_pkt.src_ip] = arp_pkt.src_mac

        # We only care about ARP requests
        if arp_pkt.opcode != arp.ARP_REQUEST:
            return

        target_ip = arp_pkt.dst_ip
        
        # Check if the ARP request is for a known host or gateway interface
        target_mac = self.arp_table.get(target_ip)

        if target_mac:
            # We know the MAC for the requested IP. Send a reply.
            self.send_arp_reply(datapath, in_port, target_mac, target_ip,
                                arp_pkt.src_mac, arp_pkt.src_ip)

    def send_arp_reply(self, datapath, port, src_mac, src_ip, dst_mac, dst_ip):
        """Send ARP reply"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        e = ethernet.ethernet(dst=dst_mac, src=src_mac, ethertype=0x0806)
        a = arp.arp(opcode=arp.ARP_REPLY, src_mac=src_mac, src_ip=src_ip,
                   dst_mac=dst_mac, dst_ip=dst_ip)
        p = packet.Packet()
        p.add_protocol(e)
        p.add_protocol(a)
        p.serialize()
        
        actions = [parser.OFPActionOutput(port)]
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=ofproto.OFP_NO_BUFFER,
                                  in_port=ofproto.OFPP_CONTROLLER, actions=actions,
                                  data=p.data)
        datapath.send_msg(out)

    def handle_ipv4(self, datapath, in_port, pkt, eth, msg):
        """Handle IPv4 packets"""
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt is None:
            return
        
        dpid = datapath.id
        sw_name = self.dpid_to_name.get(dpid, f"unknown-{dpid}")
        src_ip = ip_pkt.src
        dst_ip = ip_pkt.dst
                
        # === ignore multicast and broadcast IPv4 destinations ===
        try:
            ip_obj = ipaddress.ip_address(dst_ip)
            if ip_obj.is_multicast:
                self.logger.debug("Ignoring multicast dst %s", dst_ip)
                return
            if dst_ip == '255.255.255.255':
                self.logger.debug("Ignoring broadcast dst %s", dst_ip)
                return
        except Exception:
            pass

        self.logger.info(f"IPv4 packet at {sw_name}: {src_ip} -> {dst_ip}, TTL={ip_pkt.ttl}")

        # TTL check
        if ip_pkt.ttl <= 1:
            self.logger.warning(f"TTL expired for packet from {src_ip} to {dst_ip}")
            return
        
        # Find source and destination switches
        src_switch = self.get_switch_for_host(src_ip)
        dst_switch = self.get_switch_for_host(dst_ip)
        
        # If source unknown, assume it's the current switch
        if src_switch is None:
            src_switch = sw_name
        
        if dst_switch is None:
            self.logger.warning(f"Unknown destination: {dst_ip}")
            return
        
        self.logger.info(f"Routing from {src_switch} to {dst_switch}")
        
        # Compute shortest path
        path = self.dijkstra(src_switch, dst_switch)
        if path is None:
            self.logger.warning(f"No path from {src_switch} to {dst_switch}")
            return
        
        self.logger.info(f"Shortest path: {' -> '.join(path)}")
        
        # Install flow rules along the path
        self.install_path(path, src_ip, dst_ip, eth.src, msg)

    def install_path(self, path, src_ip, dst_ip, original_src_mac, msg=None):
        """Install flow rules with correct L3 logic along the computed path."""
        dst_host_name = self.host_info.get(dst_ip, {}).get('name')
        if not dst_host_name:
            self.logger.error(f"Cannot find host name for IP {dst_ip}. Check your config.")
            return

        # Iterate through the switches in the path to install rules
        for i, switch_name in enumerate(path):
            datapath = self.datapaths.get(self.name_to_dpid.get(switch_name))
            if not datapath:
                continue
            
            parser = datapath.ofproto_parser
            ofproto = datapath.ofproto
            
            is_final_hop = (i == len(path) - 1)

            if is_final_hop:
                # Final hop: Switch -> Host
                out_port = self.switch_ports[switch_name].get(dst_host_name)
                dst_mac = self.host_info[dst_ip]['mac']
                # Source MAC is the switch's interface MAC facing the host
                src_mac = self.switch_interfaces[switch_name][dst_host_name]['mac']
            else:
                # Intermediate hop: Switch -> Next Switch
                next_switch_name = path[i + 1]
                out_port = self.switch_ports[switch_name].get(next_switch_name)
                src_mac = self.switch_interfaces[switch_name][next_switch_name]['mac']
                dst_mac = self.switch_interfaces[next_switch_name][switch_name]['mac']

            if out_port is None or src_mac is None or dst_mac is None:
                self.logger.error(f"Incomplete path info at {switch_name}. Aborting install.")
                return

            match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=dst_ip)
            actions = [
                parser.OFPActionSetField(eth_src=src_mac),
                parser.OFPActionSetField(eth_dst=dst_mac),
                parser.OFPActionDecNwTtl(),
                parser.OFPActionOutput(out_port)
            ]
            self.add_flow(datapath, 10, match, actions, idle_timeout=60)
            
            if i == 0 and msg:
                data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
                out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                          in_port=msg.match['in_port'], actions=actions, data=data)
                datapath.send_msg(out)
