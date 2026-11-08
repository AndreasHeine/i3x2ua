from __future__ import annotations

import argparse
import ipaddress
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an OPC UA-compatible client certificate and private key.",
    )
    parser.add_argument(
        "--out-dir",
        default="certs/opcua-client-sample",
        help="Output directory for client-cert.pem and client-key.pem.",
    )
    parser.add_argument(
        "--app-uri",
        default="urn:i3x2ua:opcua_client",
        help="Application URI to write into SAN URI.",
    )
    parser.add_argument(
        "--common-name",
        default="i3x2ua-opcua-client",
        help="Certificate subject common name (CN).",
    )
    parser.add_argument(
        "--organization",
        default="i3x2ua",
        help="Certificate subject organization (O).",
    )
    parser.add_argument(
        "--country",
        default="DE",
        help="Certificate subject country (C).",
    )
    parser.add_argument(
        "--days-valid",
        type=int,
        default=3650,
        help="Validity period in days. Must be a positive number.",
    )
    parser.add_argument(
        "--dns",
        action="append",
        dest="dns_names",
        default=[],
        help="Additional SAN DNS names. Can be provided multiple times.",
    )
    parser.add_argument(
        "--ip",
        action="append",
        dest="ip_addresses",
        default=[],
        help="Additional SAN IP addresses, written as iPAddress. Can be provided multiple times.",
    )
    parser.add_argument(
        "--include-hostname",
        action="store_true",
        help="Add the local host name to the SAN DNS names. Off by default so the generated "
        "certificate does not carry a developer machine name into committed material.",
    )
    return parser


def _san_dns_names(dns_names: list[str], include_hostname: bool) -> list[str]:
    names = ["localhost"]
    if include_hostname:
        names.append(socket.gethostname())
    for name in dns_names:
        if name not in names:
            names.append(name)
    return names


def _san_ip_addresses(ip_addresses: list[str]) -> list[x509.IPAddress]:
    try:
        return [x509.IPAddress(ipaddress.ip_address(value)) for value in ip_addresses]
    except ValueError as exc:
        raise SystemExit(f"Invalid --ip value: {exc}") from exc


def _write_private_key(key_path: Path, private_key: rsa.RSAPrivateKey) -> None:
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    # Keep the key owner-only on POSIX. Windows has no POSIX permission bits to set.
    if os.name != "nt":
        key_path.chmod(0o600)


def generate_certificate(
    out_dir: Path,
    app_uri: str,
    common_name: str,
    organization: str,
    country: str,
    days_valid: int,
    dns_names: list[str],
    ip_addresses: list[str],
    include_hostname: bool,
) -> tuple[Path, Path, str]:
    if days_valid <= 0:
        raise SystemExit("--days-valid must be a positive number of days")

    out_dir.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, country),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )

    san_dns = _san_dns_names(dns_names, include_hostname=include_hostname)

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days_valid))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=True,
                data_encipherment=True,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(app_uri),
                    *[x509.DNSName(name) for name in san_dns],
                    *_san_ip_addresses(ip_addresses),
                ]
            ),
            critical=False,
        )
        .sign(private_key=key, algorithm=hashes.SHA256())
    )

    cert_path = out_dir / "client-cert.pem"
    key_path = out_dir / "client-key.pem"

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _write_private_key(key_path, key)

    thumbprint = cert.fingerprint(hashes.SHA1()).hex()
    return cert_path, key_path, thumbprint


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    cert_path, key_path, thumbprint = generate_certificate(
        out_dir=Path(args.out_dir),
        app_uri=args.app_uri,
        common_name=args.common_name,
        organization=args.organization,
        country=args.country,
        days_valid=args.days_valid,
        dns_names=args.dns_names,
        ip_addresses=args.ip_addresses,
        include_hostname=args.include_hostname,
    )

    print(f"Written certificate: {cert_path}")
    print(f"Written private key: {key_path}")
    print(f"SHA1 thumbprint: {thumbprint}")
    print("Remember to trust/import the client certificate on your OPC UA server.")


if __name__ == "__main__":
    main()
