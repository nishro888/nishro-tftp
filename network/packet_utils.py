"""Parse and build raw Ethernet / VLAN / IP / UDP / ARP / ICMP frames.

Every function here accepts or returns a :class:`ParsedPacket` so
higher-level responders never touch scapy directly. That keeps the
crafted reply symmetrical with the incoming frame - crucially, if the
request was 802.1Q-tagged the response re-emits the same VLAN ID,
CFI/DEI and PCP so the switch port keeps the trunk mapping intact.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from scapy.layers.inet import ICMP, IP, UDP
from scapy.layers.l2 import ARP, Dot1Q, Ether
from scapy.packet import Packet, Raw

from core.constants import IP_DEFAULT_TTL


@dataclass
class ParsedPacket:
    raw: Packet
    eth_src: str
    eth_dst: str
    vlan_id: Optional[int]
    vlan_pcp: int
    vlan_dei: int
    ether_type: int
    # Upper-layer shortcuts - only set when relevant
    is_arp: bool = False
    arp_op: int = 0
    arp_spa: str = ""
    arp_tpa: str = ""
    arp_sha: str = ""
    arp_tha: str = ""
    is_ip: bool = False
    ip_src: str = ""
    ip_dst: str = ""
    ip_proto: int = 0
    is_icmp: bool = False
    icmp_type: int = 0
    icmp_code: int = 0
    icmp_id: int = 0
    icmp_seq: int = 0
    icmp_payload: bytes = b""
    is_udp: bool = False
    udp_sport: int = 0
    udp_dport: int = 0
    udp_payload: bytes = b""


def parse(pkt: Packet) -> Optional[ParsedPacket]:
    """Best-effort parse of a scapy Ethernet frame.

    Returns ``None`` if the frame isn't something we care about -
    callers just drop the packet in that case.
    """
    if Ether not in pkt:
        return None
    eth = pkt[Ether]
    vlan_id = vlan_pcp = vlan_dei = 0
    vid: Optional[int] = None

    layer = eth.payload
    if isinstance(layer, Dot1Q):
        vid = int(layer.vlan)
        vlan_pcp = int(layer.prio)
        vlan_dei = int(layer.id)
        layer = layer.payload

    p = ParsedPacket(
        raw=pkt,
        eth_src=str(eth.src),
        eth_dst=str(eth.dst),
        vlan_id=vid,
        vlan_pcp=vlan_pcp,
        vlan_dei=vlan_dei,
        ether_type=int(eth.type),
    )

    if isinstance(layer, ARP):
        p.is_arp = True
        p.arp_op = int(layer.op)
        p.arp_spa = str(layer.psrc)
        p.arp_tpa = str(layer.pdst)
        p.arp_sha = str(layer.hwsrc)
        p.arp_tha = str(layer.hwdst)
        return p

    if isinstance(layer, IP):
        p.is_ip = True
        p.ip_src = str(layer.src)
        p.ip_dst = str(layer.dst)
        p.ip_proto = int(layer.proto)
        inner = layer.payload
        if isinstance(inner, ICMP):
            p.is_icmp = True
            p.icmp_type = int(inner.type)
            p.icmp_code = int(inner.code)
            p.icmp_id = int(getattr(inner, "id", 0) or 0)
            p.icmp_seq = int(getattr(inner, "seq", 0) or 0)
            payload = inner.payload
            p.icmp_payload = bytes(payload) if payload else b""
        elif isinstance(inner, UDP):
            p.is_udp = True
            p.udp_sport = int(inner.sport)
            p.udp_dport = int(inner.dport)
            # The UDP len field is (header + data). Truncate by it to
            # strip Ethernet padding that Scapy otherwise appends as a
            # Padding sub-layer under Raw - if that padding leaks into
            # udp_payload, WRQ writes trailing zeros onto disk and the
            # uploaded file comes out larger than the original.
            payload = inner.payload
            raw_bytes = bytes(payload) if payload else b""
            udp_data_len = max(0, int(inner.len) - 8) if inner.len else len(raw_bytes)
            p.udp_payload = raw_bytes[:udp_data_len]
        return p

    return p


def _vlan_wrap(base: Packet, src: ParsedPacket) -> Packet:
    """Re-tag a reply with the VLAN headers of the original request."""
    if src.vlan_id is None:
        return base
    return base / Dot1Q(vlan=src.vlan_id, prio=src.vlan_pcp, id=src.vlan_dei)


def build_arp_reply(req: ParsedPacket, our_mac: str, our_ip: str) -> Packet:
    eth = Ether(src=our_mac, dst=req.eth_src)
    # When VLAN tagged, the 802.1Q layer sits between Ether and ARP.
    frame = _vlan_wrap(eth, req) / ARP(
        op=2,  # reply
        hwsrc=our_mac,
        psrc=our_ip,
        hwdst=req.arp_sha,
        pdst=req.arp_spa,
    )
    return frame


def build_icmp_echo_reply(req: ParsedPacket, our_mac: str, our_ip: str) -> Packet:
    eth = Ether(src=our_mac, dst=req.eth_src)
    ip = IP(src=our_ip, dst=req.ip_src, ttl=IP_DEFAULT_TTL)
    icmp = ICMP(type=0, code=0, id=req.icmp_id, seq=req.icmp_seq)
    frame = _vlan_wrap(eth, req) / ip / icmp / Raw(load=req.icmp_payload)
    return frame


def build_udp_reply(
    req: ParsedPacket,
    our_mac: str,
    our_ip: str,
    sport: int,
    dport: int,
    payload: bytes,
) -> Packet:
    """Generic UDP reply builder used by the TFTP engine."""
    eth = Ether(src=our_mac, dst=req.eth_src)
    ip = IP(src=our_ip, dst=req.ip_src, ttl=IP_DEFAULT_TTL)
    udp = UDP(sport=sport, dport=dport)
    frame = _vlan_wrap(eth, req) / ip / udp / Raw(load=payload)
    return frame


def build_udp_frame(
    dst_mac: str,
    src_mac: str,
    vlan_id: Optional[int],
    vlan_pcp: int,
    vlan_dei: int,
    src_ip: str,
    dst_ip: str,
    sport: int,
    dport: int,
    payload: bytes,
) -> Packet:
    """Build a UDP frame from raw fields (used for retransmits where we
    no longer have the original ParsedPacket)."""
    eth = Ether(src=src_mac, dst=dst_mac)
    if vlan_id is not None:
        eth = eth / Dot1Q(vlan=vlan_id, prio=vlan_pcp, id=vlan_dei)
    return eth / IP(src=src_ip, dst=dst_ip, ttl=IP_DEFAULT_TTL) / UDP(sport=sport, dport=dport) / Raw(load=payload)
