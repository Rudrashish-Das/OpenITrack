"""Self-signed HTTPS certificate for phone mode (phone browsers only allow
camera access on secure pages)."""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import logging
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

log = logging.getLogger(__name__)


def local_ipv4_addresses() -> list[str]:
    ips: list[str] = []
    try:
        # Connecting a UDP socket sends nothing; it just picks the outbound interface.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith(("127.", "169.254."))]


def ensure_certificate(directory: Path, ips: list[str], hostnames: list[str] = ()) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path, meta_path = directory / "cert.pem", directory / "key.pem", directory / "cert.json"
    wanted = sorted(set(ips) | {"127.0.0.1"})
    names = sorted(set(hostnames) | {"localhost"})
    try:
        meta = json.loads(meta_path.read_text())
        expires = dt.datetime.fromisoformat(meta["expires"])
        if (
            cert_path.exists() and key_path.exists()
            and set(wanted) <= set(meta["ips"])
            and set(names) <= set(meta.get("names", []))
            and expires - dt.timedelta(days=30) > dt.datetime.now(dt.timezone.utc)
        ):
            return cert_path, key_path
    except (OSError, ValueError, KeyError):
        pass

    log.info("Creating self-signed certificate for %s", ", ".join(names + wanted))
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0] if hostnames else "OpenITrack")])
    now = dt.datetime.now(dt.timezone.utc)
    expires = now + dt.timedelta(days=825)
    san = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in wanted]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(expires)
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    meta_path.write_text(json.dumps({"ips": wanted, "names": names, "expires": expires.isoformat()}))
    return cert_path, key_path
