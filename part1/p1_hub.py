from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types

class HubController(app_manager.RyuApp):
    """ To use OpenFlow 1.3, the OpenFlow 1.3 version is specified for OFP_VERSIONS. """
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(HubController, self).__init__(*args, **kwargs)
        #* MAC address table
        # mac_to_port: dpid -> { mac -> port_no }
        self.mac_to_port = {}

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        # Install a table-miss flow that sends unmatched packets to controller.
        #* In ev.msg, the instance of the OpenFlow message class corresponding to the event is stored.
        datapath = ev.msg.datapath #* In msg.datapath, the instance of the ryu.controller.controller.Datapath class corresponding to the OpenFlow switch that issued this message is stored.
        ofproto = datapath.ofproto #* ryu.ofproto.ofproto_v1_3
        parser = datapath.ofproto_parser #* ryu.ofproto.ofproto_v1_3_parser

        # match any, send to controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        mod = parser.OFPFlowMod(datapath=datapath,
                                priority=0,
                                match=match,
                                instructions=inst)
        datapath.send_msg(mod)
        self.logger.info("Table-miss installed on %s", datapath.id) 

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        # ignore lldp, arp may be useful but we handle normally
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src = eth.src
        dst = eth.dst

        self.mac_to_port.setdefault(dpid, {})
        # learn source MAC at controller
        self.mac_to_port[dpid][src] = in_port
        self.logger.debug("DPID %s learned %s -> %s", dpid, src, in_port)

        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        # If we know destination MAC, send PacketOut to only that port
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
            actions = [parser.OFPActionOutput(out_port)]
            self.logger.debug("Forwarding %s -> out_port %s (controller based)", dst, out_port)

            data = None
            # If the packet is buffered at the switch, no need to send data payload
            # if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            #     data = msg.data

            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions, data=data)
            datapath.send_msg(out)
        else:
            # Flood if unknown
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            self.logger.debug("Flooding packet for dst %s", dst)
            data = None
            # if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            #     data = msg.data
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions, data=data)
            datapath.send_msg(out)
