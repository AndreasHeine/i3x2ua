from __future__ import annotations

import argparse
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
        default="urn:example.org:FreeOpcUa:opcua-asyncio",
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
        help="Validity period in days.",
    )
    parser.add_argument(
        "--dns",
        action="append",
        dest="dns_names",
        default=[],
        help="Additional SAN DNS names. Can be provided multiple times.",
    )
    return parser


def generate_certificate(
    out_dir: Path,
    app_uri: str,
    common_name: str,
    organization: str,
    country: str,
    days_valid: int,
    dns_names: list[str],
) -> tuple[Path, Path, str]:
    out_dir.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, country),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )

    hostname = socket.gethostname()
    san_dns = ["localhost", hostname]
    for name in dns_names:
        if name not in san_dns:
            san_dns.append(name)

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
                [x509.UniformResourceIdentifier(app_uri), *[x509.DNSName(name) for name in san_dns]]
            ),
            critical=False,
        )
        .sign(private_key=key, algorithm=hashes.SHA256())
    )

    cert_path = out_dir / "client-cert.pem"
    key_path = out_dir / "client-key.pem"

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

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
    )

    print(f"Written certificate: {cert_path}")
    print(f"Written private key: {key_path}")
    print(f"SHA1 thumbprint: {thumbprint}")
    print("Remember to trust/import the client certificate on your OPC UA server.")


if __name__ == "__main__":
    main()
