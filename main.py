"""Nishro TFTP - entry point.

Wires together the sniffer, ARP / ICMP / TFTP responders, file source,
cache and web UI. Everything runs on a single asyncio event loop;
scapy's pcap capture runs on its own thread and hands parsed frames
into the loop via :class:`network.sniffer.L2Bridge`.

Must be launched as Administrator with Npcap installed (raw L2 socket
access). Example::

    python main.py --config config.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import os
import sys

import shutil
import uvicorn


def _runtime_dir() -> str:
    """Directory where user-editable runtime files live.

    Under PyInstaller (``sys.frozen``) this is the folder containing the
    .exe, NOT the temporary extraction dir (``sys._MEIPASS``). Everything
    the user touches -- config.yaml, auth.json, users.json, logs/,
    tftp_root/, tftp_uploads/ -- lives here so it survives across runs
    and is editable without unpacking the exe.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _bundle_dir() -> str:
    """Directory containing bundled resources (web/static, seed config).

    Under PyInstaller this is ``sys._MEIPASS``; in dev it's the source
    tree next to main.py.
    """
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


# Make package-style imports work when run as a script (dev mode).
# Under PyInstaller the analyzer already resolved imports at build time.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# When frozen, chdir to the runtime dir so relative paths in the user's
# config (e.g. tftp_root: ./tftp_root) resolve next to the exe.
if getattr(sys, "frozen", False):
    os.chdir(_runtime_dir())

from core.acl import ACL
from core.auth import AuthStore
from core.config import Config
from core import carryover, daily_stats
from core.engine import CoreEngine
from core.stats import STATS
from core.users import UserStore
from core.constants import (
    CACHE_PER_FILE_BYTES_DEFAULT,
    CACHE_TOTAL_BYTES_DEFAULT,
    RESTART_GRACE_SECONDS,
    WEB_HOST_DEFAULT,
    WEB_PORT_DEFAULT,
)
from core.file_lock import FileLockManager
from core.logger import setup_logging
from files.cache import FileCache
from files.source import build_source
from network.arp_responder import ArpResponder
from network.icmp_responder import IcmpResponder
from network.sniffer import L2Bridge
from tftp.server import TftpServer
from web.app import AppState, create_app

log = logging.getLogger("nishro.main")


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _nic_real_mac(iface: str) -> str:
    """Look up the hardware MAC attached to an NPF device path. Empty
    string if not found."""
    if not iface:
        return ""
    try:
        from scapy.arch.windows import get_windows_if_list
        for nic in get_windows_if_list():
            guid = nic.get("guid", "") or ""
            name = nic.get("name", "") or ""
            if (guid and guid in iface) or (name and name == iface):
                return str(nic.get("mac", "")).lower()
    except Exception:  # noqa: BLE001
        log.exception("failed to enumerate NICs")
    return ""


def _resolve_mac(iface: str, override: str | None) -> str:
    """Return the MAC our server should send frames with.

    Honours an explicit ``network.virtual_mac`` override, but warns
    loudly when that override doesn't belong to the currently selected
    adapter -- a very common cause of "the server runs but the client
    never sees a reply" after the admin swaps NICs without clearing
    the old virtual_mac. Returns empty string when nothing resolves;
    the caller surfaces that as a degraded-start error.
    """
    real = _nic_real_mac(iface)
    if override:
        ov = override.lower()
        if real and ov != real:
            log.warning(
                "configured virtual_mac %s does NOT match NIC %s (real MAC %s). "
                "Frames will be dropped by most switches. "
                "Clear network.virtual_mac in the config to auto-use the NIC's MAC.",
                ov, iface, real,
            )
        return ov
    if real:
        log.info("resolved NIC %s -> MAC %s", iface, real)
    return real


def _list_nics() -> list[dict]:
    """Return the current list of Windows NICs as reported by scapy."""
    from scapy.arch.windows import get_windows_if_list
    return list(get_windows_if_list())


def _interactive_nic_select(preselected: str | None = None) -> str:
    """Prompt the user to pick a NIC and return the ``\\Device\\NPF_{GUID}``
    string suitable for scapy / config.yaml.

    ``preselected`` is the current NIC from the config; when provided
    it's shown as the default and an empty ENTER accepts it.
    """
    nics = _list_nics()
    if not nics:
        raise RuntimeError("no network adapters found - is Npcap installed?")

    # Sort by index for a stable ordering across runs
    def _key(n: dict) -> int:
        try:
            return int(n.get("win_index", 0) or 0)
        except (TypeError, ValueError):
            return 0
    nics.sort(key=_key)

    print()
    print("Available network adapters:")
    print("-" * 72)
    default_idx = -1
    for i, nic in enumerate(nics):
        name = nic.get("name", "?") or "?"
        desc = nic.get("description", "") or ""
        mac = nic.get("mac", "") or ""
        guid = nic.get("guid", "") or ""
        ips = nic.get("ips", []) or []
        ip_str = ", ".join(ips[:3]) if ips else "-"
        npf = f"\\Device\\NPF_{guid}" if guid else ""
        marker = " "
        if preselected and guid and guid in preselected:
            marker = "*"
            default_idx = i
        print(f" {marker} [{i:2}] {desc or name}")
        print(f"        name={name}  mac={mac}  ips={ip_str}")
        print(f"        npf={npf}")
    print("-" * 72)

    while True:
        hint = f" [default {default_idx}]" if default_idx >= 0 else ""
        raw = input(f"Select adapter number{hint}: ").strip()
        if not raw and default_idx >= 0:
            choice = default_idx
        else:
            try:
                choice = int(raw)
            except ValueError:
                print("  please enter a number")
                continue
        if 0 <= choice < len(nics):
            guid = nics[choice].get("guid", "") or ""
            if not guid:
                print("  selected adapter has no GUID; pick another")
                continue
            return f"\\Device\\NPF_{guid}"
        print("  out of range")


def _snapshot_critical(cfg: Config) -> dict:
    """Capture the subset of config values that can't be hot-reloaded
    and require a full teardown + rebuild (NIC, virtual IP/MAC, promisc
    mode, web host/port)."""
    net = cfg.get("network", default={}) or {}
    web = cfg.get("web", default={}) or {}
    return {
        "nic": net.get("nic", "") or "",
        "virtual_ip": net.get("virtual_ip", "") or "",
        "virtual_mac": (net.get("virtual_mac") or "") or "",
        "promiscuous": bool(net.get("promiscuous", True)),
        "web_host": web.get("host", WEB_HOST_DEFAULT) or WEB_HOST_DEFAULT,
        "web_port": int(web.get("port", WEB_PORT_DEFAULT) or WEB_PORT_DEFAULT),
        "engine": (cfg.get("tftp", "engine", default="python") or "python").lower(),
    }


class Nishro:
    """Top-level orchestrator - owns every long-lived component."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.loop = asyncio.get_running_loop()
        self._dead = False
        self._restart_requested = asyncio.Event()
        self._prev_critical = _snapshot_critical(cfg)
        self.engine_mode: str = (cfg.get("tftp", "engine", default="python") or "python").lower()
        self.core: CoreEngine | None = None
        self.nic_error: str | None = None   # surfaced to the web UI

        net = cfg.get("network", default={}) or {}
        self.iface: str = str(net.get("nic", "") or "")
        self.virtual_ip: str = str(net.get("virtual_ip", "") or "")
        self.virtual_mac: str = _resolve_mac(self.iface, net.get("virtual_mac"))
        self.promisc: bool = bool(net.get("promiscuous", True))
        if not self.iface:
            self.nic_error = "No network adapter is configured. Open Admin Config and select one."
        elif not self.virtual_ip:
            self.nic_error = "No virtual IP is configured. Open Admin Config and set network.virtual_ip."
        elif not self.virtual_mac:
            self.nic_error = (
                f"Could not resolve a MAC for adapter {self.iface!r}. "
                "Pick a different adapter or set network.virtual_mac manually."
            )
        else:
            # Stale virtual_mac guard -- if the user swapped NICs but left
            # an old virtual_mac override in place, nothing will reach us.
            override = (net.get("virtual_mac") or "").lower()
            real = _nic_real_mac(self.iface)
            if override and real and override != real:
                self.nic_error = (
                    f"Configured Virtual MAC ({override}) does not belong to the "
                    f"selected adapter (real MAC {real}). Clear the Virtual MAC "
                    "field in Admin Config to use the adapter's own MAC, or the "
                    "network switch will drop every reply we send."
                )

        if self.engine_mode == "c":
            # C engine owns the NIC end-to-end. Python only runs the web
            # UI, so we skip the packet-path components (bridge/tftp/arp/
            # icmp) but still build the read-only helpers the UI queries
            # (cache snapshot, source listings, ACL view).
            self.bridge = None  # type: ignore[assignment]
            self.acl = ACL(cfg.data)
            self.locks = FileLockManager()
            cache_cfg = cfg.get("files", "cache", default={}) or {}
            self.cache = FileCache(
                max_bytes=int(cache_cfg.get("max_bytes", CACHE_TOTAL_BYTES_DEFAULT)),
                max_file_bytes=int(cache_cfg.get("max_file_bytes", CACHE_PER_FILE_BYTES_DEFAULT)),
                enabled=False,
            )
            self.source = build_source(cfg.data)
            self.tftp = None  # type: ignore[assignment]
            self.arp = None  # type: ignore[assignment]
            self.icmp = None  # type: ignore[assignment]
            cfg.subscribe(self._on_config_change)
            return

        self.bridge = (
            L2Bridge(self.iface, self.loop, promisc=self.promisc)
            if (self.iface and not self.nic_error)
            else None   # type: ignore[assignment]
        )
        self.acl = ACL(cfg.data)
        self.locks = FileLockManager()

        cache_cfg = cfg.get("files", "cache", default={})
        self.cache = FileCache(
            max_bytes=int(cache_cfg.get("max_bytes", CACHE_TOTAL_BYTES_DEFAULT)),
            max_file_bytes=int(cache_cfg.get("max_file_bytes", CACHE_PER_FILE_BYTES_DEFAULT)),
            enabled=bool(cache_cfg.get("enabled", False)),
        )
        self.source = build_source(cfg.data)

        tftp_cfg = cfg.get("tftp", default={}) or {}
        merged = {
            **tftp_cfg,
            "sessions": cfg.get("sessions", default={}) or {},
            "files": cfg.get("files", default={}) or {},
        }
        if self.bridge is not None:
            self.tftp = TftpServer(
                bridge=self.bridge,
                virtual_ip=self.virtual_ip,
                virtual_mac=self.virtual_mac,
                cfg=merged,
                source=self.source,
                cache=self.cache,
                locks=self.locks,
                acl=self.acl,
            )
            self.arp = ArpResponder(self.bridge, self.virtual_ip, self.virtual_mac, self.acl)
            self.icmp = IcmpResponder(self.bridge, self.virtual_ip, self.virtual_mac, self.acl)
        else:
            self.tftp = None   # type: ignore[assignment]
            self.arp = None    # type: ignore[assignment]
            self.icmp = None   # type: ignore[assignment]

        # Hot-reload wiring
        cfg.subscribe(self._on_config_change)

    def _core_cfg_data(self) -> dict:
        """Return a shallow copy of the YAML config with the resolved
        virtual_mac injected, so the C engine doesn't have to depend on
        the user providing it explicitly."""
        data = dict(self.cfg.data)
        net = dict(data.get("network", {}) or {})
        if not net.get("virtual_mac"):
            net["virtual_mac"] = self.virtual_mac
        data["network"] = net
        return data

    # -- Config reload -------------------------------------------------
    def _on_config_change(self, cfg: Config) -> None:
        if self._dead:
            # Orphaned subscription from a previous Nishro instance -
            # ignore it, the restarted instance has its own subscriber.
            return

        log.info("config reloaded")

        # Critical fields (NIC, virtual IP/MAC, web host/port) can't be
        # hot-swapped - uvicorn can't rebind mid-serve and the L2 socket
        # is pinned to its device. Request a full restart instead; the
        # outer loop in ``_amain`` will tear us down and rebuild.
        new_critical = _snapshot_critical(cfg)
        if new_critical != self._prev_critical:
            log.info(
                "critical config changed (%s) - restarting",
                {k: (self._prev_critical[k], new_critical[k])
                 for k in new_critical if new_critical[k] != self._prev_critical[k]},
            )
            try:
                self.loop.call_soon_threadsafe(self._restart_requested.set)
            except RuntimeError:
                # Loop already closed
                self._restart_requested.set()
            return

        try:
            setup_logging(cfg.get("logging", default={}) or {})
        except Exception:  # noqa: BLE001
            log.exception("logging reconfigure failed")

        if self.engine_mode == "c":
            # Push config to the running C engine; critical fields would
            # already have triggered a restart above.
            if self.core:
                self.loop.create_task(self.core.push_config(self._core_cfg_data()))
            return

        self.acl.reload(cfg.data)

        net = cfg.get("network", default={})
        new_ip = net.get("virtual_ip", self.virtual_ip)
        new_mac = net.get("virtual_mac") or self.virtual_mac
        self.virtual_ip = new_ip
        self.virtual_mac = new_mac.lower() if isinstance(new_mac, str) else new_mac

        self.arp.update(self.virtual_ip, self.virtual_mac)
        self.icmp.update(self.virtual_ip, self.virtual_mac)

        cache_cfg = cfg.get("files", "cache", default={}) or {}
        self.cache.update(
            max_bytes=int(cache_cfg.get("max_bytes", self.cache.max_bytes)),
            max_file_bytes=int(cache_cfg.get("max_file_bytes", self.cache.max_file_bytes)),
            enabled=bool(cache_cfg.get("enabled", self.cache.enabled)),
        )

        # Rebuild source if the source kind / root changed
        try:
            self.source.close()
        except Exception:  # noqa: BLE001
            pass
        self.source = build_source(cfg.data)

        tftp_cfg = cfg.get("tftp", default={}) or {}
        merged = {
            **tftp_cfg,
            "sessions": cfg.get("sessions", default={}) or {},
            "files": cfg.get("files", default={}) or {},
        }
        self.tftp.update(self.virtual_ip, self.virtual_mac, merged)
        self.tftp.source = self.source
        self.tftp.cache = self.cache
        self.tftp.acl = self.acl

    # -- Run -----------------------------------------------------------
    async def _dispatch_loop(self) -> None:
        """Consume packets from the sniffer queue and dispatch them to
        the first responder that claims them."""
        async for pkt in self.bridge.packets():
            try:
                if self.arp.handle(pkt):
                    continue
                if self.icmp.handle(pkt):
                    continue
                if self.tftp.handle(pkt):
                    continue
            except Exception:  # noqa: BLE001
                log.exception("dispatch failed")

    async def run(self) -> bool:
        """Run until the dispatch or web task exits, or a config change
        requests a restart. Returns ``True`` if the caller should rebuild
        a fresh :class:`Nishro` instance.

        Startup is best-effort: if the NIC can't be opened (bad or
        missing adapter in the config), we still bring up the web UI so
        the admin can pick a working one. `nic_error` is surfaced in
        ``/api/stats`` and rendered as a banner in the dashboard.
        """
        if self.engine_mode == "c":
            if self.nic_error:
                log.warning("C engine not started: %s", self.nic_error)
            else:
                self.core = CoreEngine()
                try:
                    await self.core.start(self._core_cfg_data())
                    log.info("Nishro TFTP online (C engine) - iface=%s vip=%s vmac=%s",
                             self.iface, self.virtual_ip, self.virtual_mac)
                except Exception as e:  # noqa: BLE001
                    log.exception("C engine failed to start")
                    self.nic_error = f"C engine failed to start: {e}"
                    self.core = None
        else:
            if self.bridge is None or self.nic_error:
                log.warning("Python engine not started: %s",
                            self.nic_error or "no NIC configured")
            else:
                try:
                    self.bridge.start()
                    log.info(
                        "Nishro TFTP online - iface=%s vip=%s vmac=%s",
                        self.iface, self.virtual_ip, self.virtual_mac,
                    )
                except Exception as e:  # noqa: BLE001
                    log.exception("L2 bridge failed to start")
                    self.nic_error = (
                        f"Could not open adapter {self.iface!r}: {e}. "
                        "Open Admin Config and pick a different adapter."
                    )
                    try:
                        self.bridge.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    self.bridge = None   # type: ignore[assignment]
                    self.tftp = None     # type: ignore[assignment]

        auth_store = AuthStore(str(self.cfg.path.parent))
        user_store = UserStore(
            os.path.join(str(self.cfg.path.parent), "users.json")
        )
        app_state = AppState(
            config=self.cfg,
            tftp_server=self.core if self.engine_mode == "c" else self.tftp,
            cache=self.cache,
            source_provider=lambda: self.source,
            reload_hook=lambda new_cfg: None,  # config.save already fires subscribers
            auth=auth_store,
            users=user_store,
        )
        # Degraded-start context -- the UI shows a banner based on this.
        app_state.nic_error = self.nic_error
        app = create_app(app_state)

        web_cfg = self.cfg.get("web", default={}) or {}
        host = web_cfg.get("host", WEB_HOST_DEFAULT)
        port = int(web_cfg.get("port", WEB_PORT_DEFAULT))

        ucfg = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            lifespan="off",
            access_log=False,
        )
        server = uvicorn.Server(ucfg)
        # Uvicorn's default signal handlers use loop.add_signal_handler,
        # which raises NotImplementedError on the Windows ProactorEventLoop
        # and causes serve() to bail out before it ever binds the port.
        # We're already wrapping the whole process in a KeyboardInterrupt
        # handler in main(), so the uvicorn handlers aren't needed.
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

        log.info("starting web UI on http://%s:%d", host, port)

        if self.engine_mode == "c":
            # No Python dispatch loop in C mode -- substitute a task that
            # just waits on the child process exiting.
            async def _watch_core():
                if self.core and self.core.proc:
                    await self.core.proc.wait()
                else:
                    # Degraded mode -- wait forever so the web task owns
                    # the lifecycle.
                    await asyncio.Event().wait()
            dispatch_task = asyncio.create_task(_watch_core(), name="dispatch")
        elif self.bridge is not None:
            dispatch_task = asyncio.create_task(self._dispatch_loop(), name="dispatch")
        else:
            async def _idle():
                await asyncio.Event().wait()
            dispatch_task = asyncio.create_task(_idle(), name="dispatch")
        web_task = asyncio.create_task(self._run_web(server), name="web")
        restart_task = asyncio.create_task(
            self._restart_requested.wait(), name="restart-wait"
        )

        restart = False
        try:
            pending = (await asyncio.wait(
                {dispatch_task, web_task, restart_task},
                return_when=asyncio.FIRST_COMPLETED,
            ))[1]
            restart = self._restart_requested.is_set()
            if restart:
                log.info("restart requested - draining tasks")
                # Ask uvicorn to shut down cleanly; it will let the
                # in-flight /api/config POST finish responding before
                # the listen socket is closed.
                server.should_exit = True
            for t in pending:
                if t is not web_task:
                    t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            log.info("shutting down")
            self._dead = True
            try:
                self.cfg.unsubscribe(self._on_config_change)
            except Exception:  # noqa: BLE001
                pass
            if self.engine_mode == "c":
                if self.core:
                    # Freeze the C engine's cumulative counters so the
                    # next engine (or a fresh C subprocess) keeps showing
                    # them. Must happen before stop() tears the pipe down.
                    try:
                        carryover.add(self.core.get_stats() or {})
                    except Exception:  # noqa: BLE001
                        log.debug("carryover capture (C engine) failed", exc_info=True)
                    await self.core.stop()
            else:
                if self.tftp is not None:
                    self.tftp.shutdown()
                if self.bridge is not None:
                    self.bridge.stop()
                # Fold the Python engine's cumulative counters into the
                # carryover and zero STATS out, so restarting into any
                # engine doesn't reset the user-visible totals.
                try:
                    carryover.add(STATS.reset_counters())
                except Exception:  # noqa: BLE001
                    log.debug("carryover capture (python engine) failed", exc_info=True)

        return restart

    async def _run_web(self, server: "uvicorn.Server") -> None:
        """Run uvicorn and make sure any startup error is visible in
        the log rather than swallowed by the enclosing ``gather``."""
        try:
            await server.serve()
        except Exception:  # noqa: BLE001
            log.exception("web UI crashed")
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Nishro TFTP - VLAN-aware raw-Ethernet TFTP server")
    parser.add_argument("--config", default="config.yaml", help="path to YAML config")
    parser.add_argument(
        "--select-nic",
        action="store_true",
        help="interactively pick a network adapter and save it to the config",
    )
    parser.add_argument(
        "--list-nics",
        action="store_true",
        help="list network adapters and exit",
    )
    args = parser.parse_args()

    if sys.platform != "win32":
        print("ERROR: Nishro TFTP is Windows-only (requires Npcap).", file=sys.stderr)
        sys.exit(1)

    if args.list_nics:
        try:
            _interactive_nic_select(None)
        except KeyboardInterrupt:
            print()
        return

    if not _is_admin():
        print("ERROR: must run as Administrator for raw L2 socket access.", file=sys.stderr)
        sys.exit(1)

    # First-run seed: under PyInstaller, if the user hasn't placed a
    # config.yaml next to the exe, copy the bundled template out so they
    # have something to edit.
    if getattr(sys, "frozen", False) and not os.path.exists(args.config):
        seed = os.path.join(_bundle_dir(), "config.yaml")
        if os.path.exists(seed):
            try:
                shutil.copy2(seed, args.config)
                print(f"seeded {args.config} from bundled template")
            except Exception as exc:  # noqa: BLE001
                print(f"WARN: could not seed config.yaml: {exc}", file=sys.stderr)

    cfg = Config(args.config)
    setup_logging(cfg.get("logging", default={}) or {})
    log.info("loaded config from %s", args.config)

    # Initialise the daily session counter. File lives next to the
    # user's config so it survives re-installs and is easy to back up.
    daily_stats.init(os.path.join(str(cfg.path.parent), "daily_sessions.json"))

    # NIC selection: either the user explicitly asked with --select-nic,
    # or the config's network.nic field is empty / null. In both cases
    # we prompt interactively and persist the selection.
    current_nic = (cfg.get("network", "nic", default="") or "").strip()
    if args.select_nic or not current_nic:
        try:
            picked = _interactive_nic_select(current_nic or None)
        except KeyboardInterrupt:
            print()
            sys.exit(1)
        new_data = dict(cfg.data)
        net = dict(new_data.get("network", {}) or {})
        net["nic"] = picked
        new_data["network"] = net
        cfg.save(new_data)
        log.info("saved selected NIC to config: %s", picked)

    try:
        asyncio.run(_amain(cfg))
    except KeyboardInterrupt:
        log.info("interrupted")


async def _amain(cfg: Config) -> None:
    while True:
        # Reload from disk each iteration: a /api/config POST has
        # already saved the new YAML, and dropping the old subscribers
        # keeps us from double-firing into the dead instance.
        try:
            cfg.clear_subscribers()
            cfg.reload()
        except Exception:  # noqa: BLE001
            log.exception("config reload on restart failed")

        nishro = Nishro(cfg)
        should_restart = await nishro.run()
        if not should_restart:
            return
        log.info("restarting Nishro with new config")
        # Small pause to let uvicorn finish flushing the /api/config
        # response and release the listen socket before we rebind.
        await asyncio.sleep(RESTART_GRACE_SECONDS)


if __name__ == "__main__":
    main()
