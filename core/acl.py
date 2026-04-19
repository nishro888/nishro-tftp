"""VLAN + IP access control lists with independent per-service scope."""
from __future__ import annotations

import ipaddress
import logging
from typing import Iterable

log = logging.getLogger("nishro.acl")

Mode = str  # "disabled" | "whitelist" | "blacklist"


class ACL:
    """Pair of ACLs (VLAN + IP) with a per-service scope mask.

    Each ACL independently allows/denies based on a whitelist or
    blacklist. A ``disabled`` mode short-circuits to ``allow``.
    Services are selected via the ``apply_to`` list in the config -
    e.g. an ACL that only applies to TFTP will still let ARP / ICMP
    traffic through.
    """

    def __init__(self, cfg: dict) -> None:
        self.reload(cfg)

    def reload(self, cfg: dict) -> None:
        sec = cfg.get("security", {}) or {}
        vlan = sec.get("vlan_acl", {}) or {}
        ip = sec.get("ip_acl", {}) or {}

        self.vlan_mode: Mode = str(vlan.get("mode", "disabled")).lower()
        self.vlan_list: set[int] = {int(v) for v in (vlan.get("list") or [])}
        self.vlan_apply: set[str] = {str(s).lower() for s in (vlan.get("apply_to") or [])}

        self.ip_mode: Mode = str(ip.get("mode", "disabled")).lower()
        self.ip_nets: list[ipaddress._BaseNetwork] = []
        for item in ip.get("list") or []:
            try:
                self.ip_nets.append(ipaddress.ip_network(str(item), strict=False))
            except ValueError:
                log.warning("ignoring invalid IP ACL entry: %r", item)
        self.ip_apply: set[str] = {str(s).lower() for s in (ip.get("apply_to") or [])}

    # -- Checks --------------------------------------------------------
    def check(self, service: str, vlan_id: int | None, src_ip: str | None) -> str | None:
        """Return ``None`` if allowed, or a denial reason string
        (``"vlan"`` or ``"ip"``) if denied."""
        service = service.lower()

        if service in self.vlan_apply and self.vlan_mode != "disabled":
            v = vlan_id if vlan_id is not None else 0
            in_list = v in self.vlan_list
            if self.vlan_mode == "whitelist" and not in_list:
                return "vlan"
            if self.vlan_mode == "blacklist" and in_list:
                return "vlan"

        if service in self.ip_apply and self.ip_mode != "disabled" and src_ip:
            try:
                addr = ipaddress.ip_address(src_ip)
            except ValueError:
                return "ip"
            in_list = any(addr in net for net in self.ip_nets)
            if self.ip_mode == "whitelist" and not in_list:
                return "ip"
            if self.ip_mode == "blacklist" and in_list:
                return "ip"

        return None

    def allow(self, service: str, vlan_id: int | None, src_ip: str | None) -> bool:
        """Return ``True`` if the packet is permitted for ``service``."""
        return self.check(service, vlan_id, src_ip) is None


__all__ = ["ACL", "Mode"]
