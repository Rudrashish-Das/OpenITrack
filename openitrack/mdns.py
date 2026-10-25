"""Advertise the phone page as https://openitrack.local via mDNS (Bonjour), so
users type a name instead of an IP address that may change."""

from __future__ import annotations

import logging
import re
import socket

log = logging.getLogger(__name__)

SERVICE_TYPE = "_https._tcp.local."


def _clean(label: str) -> str:
    label = re.sub(r"[^a-z0-9-]", "-", label.lower()).strip("-")
    return label[:40] or "pc"


class MdnsAdvertiser:
    def __init__(self, name: str, port: int, ips: list[str]):
        self.base_name = _clean(name)
        self.port = port
        self.ips = ips
        self.hostname = ""          # e.g. "openitrack.local", once started
        self._zc = None
        self._info = None

    def start(self) -> str | None:
        """Register the host name; returns it, or None if mDNS isn't available."""
        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf  # slow to import, and optional
        except ImportError:
            log.warning("mDNS unavailable (zeroconf not installed); use the IP address link")
            return None
        if not self.ips:
            return None
        try:
            zc = Zeroconf(ip_version=IPVersion.V4Only)
        except OSError as exc:
            log.warning("mDNS unavailable (%s); use the IP address link", exc)
            return None

        # If another PC on this network already answers as openitrack.local,
        # fall back to openitrack-<this pc>.local rather than fighting over it.
        names = [self.base_name, f"{self.base_name}-{_clean(socket.gethostname())}"]
        for host in names:
            existing = zc.get_service_info(SERVICE_TYPE, f"{host}.{SERVICE_TYPE}", timeout=1200)
            addrs = set(existing.parsed_addresses()) if existing else set()
            if existing and not addrs & set(self.ips):
                log.info("%s.local is already used by another device; trying another name", host)
                continue
            info = ServiceInfo(
                SERVICE_TYPE, f"{host}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(ip) for ip in self.ips],
                port=self.port, server=f"{host}.local.",
                properties={"path": "/", "app": "OpenITrack"},
            )
            try:
                zc.register_service(info, allow_name_change=False)
            except Exception as exc:  # NonUniqueNameException and friends
                log.info("mDNS name %s.local unavailable (%s)", host, exc)
                continue
            self._zc, self._info = zc, info
            self.hostname = f"{host}.local"
            return self.hostname
        zc.close()
        return None

    def close(self) -> None:
        if self._zc is not None:
            try:
                if self._info is not None:
                    self._zc.unregister_service(self._info)
            finally:
                self._zc.close()
            self._zc = None
