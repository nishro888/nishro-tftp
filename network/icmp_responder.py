"""ICMP echo responder for the virtual IP."""
from __future__ import annotations

import logging

from core.acl import ACL
from core.stats import STATS

from .packet_utils import ParsedPacket, build_icmp_echo_reply
from .sniffer import L2Bridge

log = logging.getLogger("nishro.icmp")

ICMP_ECHO_REQUEST = 8


class IcmpResponder:
    def __init__(self, bridge: L2Bridge, virtual_ip: str, virtual_mac: str, acl: ACL):
        self.bridge = bridge
        self.virtual_ip = virtual_ip
        self.virtual_mac = virtual_mac
        self.acl = acl

    def update(self, virtual_ip: str, virtual_mac: str) -> None:
        self.virtual_ip = virtual_ip
        self.virtual_mac = virtual_mac

    def handle(self, p: ParsedPacket) -> bool:
        if not (p.is_icmp and p.icmp_type == ICMP_ECHO_REQUEST):
            return False
        if p.ip_dst != self.virtual_ip:
            return False

        STATS.bump("icmp_requests")
        denial = self.acl.check("icmp", p.vlan_id, p.ip_src)
        if denial:
            STATS.bump("acl_denied")
            STATS.bump("vlan_denied" if denial == "vlan" else "ip_denied")
            log.debug("ICMP denied by %s ACL: src=%s vlan=%s", denial, p.ip_src, p.vlan_id)
            return True

        reply = build_icmp_echo_reply(p, self.virtual_mac, self.virtual_ip)
        self.bridge.send(reply)
        STATS.bump("icmp_replies")
        log.debug("ICMP echo reply -> %s (vlan=%s)", p.ip_src, p.vlan_id)
        return True
