from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types

# --- added imports for logging to files ---
import os, logging, json
# --- end additions ---

class LearningSwitch(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    
    IDLE_TIMEOUT = 100
    HARD_TIMEOUT = 0

    def __init__(self, *args, **kwargs):
        super(LearningSwitch, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self._init_file_logging()  # added

    # --- added: file logging setup (controller.log & mac_table.log) ---
    def _init_file_logging(self):
        base = os.path.dirname(__file__)
        ctrl_path = os.path.join(base, "learning_controller.log")
        mac_path  = os.path.join(base, "learning_mac_table.log")

        if not any(isinstance(h, logging.FileHandler) and getattr(h, "_ls_ctrl", False)
                   for h in self.logger.handlers):
            fh = logging.FileHandler(ctrl_path, mode='a')
            fh._ls_ctrl = True
            fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            self.logger.addHandler(fh)
            self.logger.propagate = False  # keep output clean

        self.mac_logger = logging.getLogger("learning_mac_table")
        if not any(isinstance(h, logging.FileHandler) and getattr(h, "_ls_mac", False)
                   for h in self.mac_logger.handlers):
            mh = logging.FileHandler(mac_path, mode='a')
            mh._ls_mac = True
            mh.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
            self.mac_logger.addHandler(mh)
            self.mac_logger.setLevel(logging.INFO)
    # --- end additions ---

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
        mod = parser.OFPFlowMod(datapath=datapath,
                                priority=priority,
                                match=match,
                                instructions=inst,
                                idle_timeout=idle_timeout,
                                hard_timeout=hard_timeout)
        datapath.send_msg(mod)
        if dst:
            self.logger.info("[FLOW_INSTALL] dpid=%s eth_dst=%s -> out=%s idle=%s",
                             datapath.id, dst, actions[0].port if actions else "-", idle_timeout)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src = eth.src
        dst = eth.dst

        table = self.mac_to_port.setdefault(dpid, {})
        learned = False
        if table.get(src) != in_port:
            table[src] = in_port
            learned = True
            # dump full MAC table snapshot for this dpid
            self.mac_logger.info("dpid=%s %s", dpid, json.dumps(table, sort_keys=True))
            self.logger.info("[LEARN] dpid=%s %s->port%s size=%d",
                             dpid, src, in_port, len(table))

        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        if dst in table:
            out_port = table[dst]
            actions = [parser.OFPActionOutput(out_port)]
            match = parser.OFPMatch(eth_dst=dst)
            self.add_flow(datapath, priority=1, match=match, actions=actions,
                          idle_timeout=self.IDLE_TIMEOUT, hard_timeout=self.HARD_TIMEOUT, dst=dst)
            decision = f"UNICAST:{out_port}"
        else:
            out_port = ofproto.OFPP_FLOOD
            actions = [parser.OFPActionOutput(out_port)]
            decision = "FLOOD"

        self.logger.info("[PACKET_IN] dpid=%s in_port=%s src=%s dst=%s learned=%s known_dst=%s action=%s",
                         dpid, in_port, src, dst, learned, dst in table, decision)

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        out = parser.OFPPacketOut(datapath=datapath,
                                  buffer_id=msg.buffer_id,
                                  in_port=in_port,
                                  actions=actions,
                                  data=data)
        datapath.send_msg(out)
        self.logger.info("[PACKET_OUT] dpid=%s action=%s buffer_id=%s",
                         dpid, decision,
                         msg.buffer_id if msg.buffer_id != ofproto.OFP_NO_BUFFER else "NO_BUFFER")