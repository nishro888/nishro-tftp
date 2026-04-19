# Troubleshooting

## "TFTP engine not running" banner on the dashboard

The server came up in degraded mode -- web UI is running but the packet
engine didn't open a NIC. The banner text tells you which case:

- *No network adapter is configured.* -> Admin Config -> pick one.
- *No virtual IP is configured.* -> Admin Config -> set `virtual_ip` to
  an unused address on the adapter's subnet.
- *Could not resolve a MAC for adapter X.* -> Adapter is gone or
  disabled in Windows. Pick another.
- *Configured Virtual MAC ... does not belong to the selected adapter.*
  -> Clear the **Virtual MAC** field. You most likely swapped NICs and
  left a stale override in place; every switch on the path will drop
  replies stamped with a MAC that doesn't live on its port.

## Client says "TFTP timeout"

In order of likelihood:

1. **Stale Virtual MAC** (see above). Fix in Admin Config.
2. **Virtual IP already assigned to the NIC in Windows.** Nishro
   replies on the raw L2 path; Windows will also ARP-respond and steal
   the flow. Remove the IP from the adapter in Windows.
3. **Client doesn't know the MAC.** Nishro answers ARP for the virtual
   IP; make sure ARP service is allowed in the ACL.
4. **Promiscuous mode disabled on a port that needs it.** If the
   virtual IP is on a VLAN that this NIC doesn't hold natively, you
   need promiscuous mode AND the VLAN must be present on the switch
   port.
5. **Npcap not installed in WinPcap API mode.** Reinstall with that
   option checked.

## The C engine shows a capped ~700 KiB/s

Out of date build. Rebuild:

```bat
cd c_core
mingw32-make clean && mingw32-make
cd ..
build_exe.bat
```

The fast path requires `pcap_sendqueue_transmit`, 16 MiB pcap ring,
and zero UDP checksum -- all present in the current `c_core/`.

## Adapter dropdown shows 40+ entries

You're probably running an older build with the psutil-first
enumerator. The current code iterates Npcap's own list; you should see
only adapters that pcap can actually open (typically 2-6 on a laptop).

## "pcap open '...' failed" in the log

Only root causes worth checking:

- Npcap is not installed -- install from npcap.com.
- The saved NPF path points at an adapter that has since been removed
  (USB NIC unplugged, VM re-provisioned). Pick another in Admin Config.
- The process is not elevated. The exe is UAC-marked; if you launched
  it from a non-elevated shell Windows should have re-prompted -- if
  it didn't, re-launch via right-click -> Run as administrator.

## Upload button does nothing

Uploads land under the RRQ root panel's current subdirectory. If that
path isn't writeable by the elevated process (NTFS ACL), the request
returns 400. Check the folder's permissions or pick a subfolder you
control.
