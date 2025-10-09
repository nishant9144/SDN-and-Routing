from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types

# Added for file logging
import os, logging, json

class HubController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(HubController, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self._init_file_logging()

    def _init_file_logging(self):
        log_dir = os.path.dirname(__file__)
        controller_log = os.path.join(log_dir, "controller.log")
        mac_table_log = os.path.join(log_dir, "mac_table.log")

        # Controller log file (info lines you already had)
        if not any(isinstance(h, logging.FileHandler) and getattr(h, "_ctl", False)
                   for h in self.logger.handlers):
            fh = logging.FileHandler(controller_log, mode='a')
            fh._ctl = True
            fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            self.logger.addHandler(fh)
            self.logger.propagate = False  # keep output clean

        # Separate MAC table logger
        self.mac_logger = logging.getLogger("mac_table")
        if not any(isinstance(h, logging.FileHandler) and getattr(h, "_mac", False)
                   for h in self.mac_logger.handlers):
            mh = logging.FileHandler(mac_table_log, mode='a')
            mh._mac = True
            mh.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
            self.mac_logger.addHandler(mh)
            self.mac_logger.setLevel(logging.INFO)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath,
                                priority=0,
                                match=match,
                                instructions=inst)
        datapath.send_msg(mod)
        self.logger.info("[SWITCH_FEATURES] dpid=%s table-miss flow installed", datapath.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match.get('in_port')
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src = eth.src
        dst = eth.dst

        table = self.mac_to_port.setdefault(dpid, {})
        if table.get(src) != in_port:
            table[src] = in_port
            self.logger.info("[LEARN] dpid=%s %s -> port %s", dpid, src, in_port)
            # Dump full table snapshot for this dpid
            self.mac_logger.info("dpid=%s %s", dpid, json.dumps(table, sort_keys=True))

        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        if dst in table:
            out_port = table[dst]
            actions = [parser.OFPActionOutput(out_port)]
            decision = f"FORWARD:{out_port}"
        else:
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            decision = "FLOOD"

        self.logger.info("[PACKET_IN] dpid=%s in_port=%s src=%s dst=%s known_dst=%s action=%s",
                         dpid, in_port, src, dst, dst in table, decision)

        out = parser.OFPPacketOut(datapath=datapath,
                                  buffer_id=ofproto.OFP_NO_BUFFER,
                                  in_port=in_port,
                                  actions=actions,
                                  data=msg.data)
        datapath.send_msg(out)
        self.logger.info("[PACKET_OUT] dpid=%s action=%s", dpid, decision)