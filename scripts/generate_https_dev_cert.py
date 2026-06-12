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
        description="Generate a development HTTPS certificate for the nginx reverse proxy.",
    )
    parser.add_argument(
        "--out-dir",
        default="certs/https-sample",
        help="Output directory for fullchain.pem and privkey.pem.",
    )
    parser.add_argument(
        "--common-name",
        default="localhost",
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
        default=825,
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
    common_name: str,
    organization: str,
    country: str,
    days_valid: int,
    dns_names: list[str],
) -> tuple[Path, Path, str]:
    out_dir.mkdir(parents=True, exist_ok=True)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, country),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )

    host_name = socket.gethostname()
    san_dns = ["localhost", host_name]
    for dns_name in dns_names:
        if dns_name not in san_dns:
            san_dns.append(dns_name)

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days_valid))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in san_dns]),
            critical=False,
        )
        .sign(private_key=private_key, algorithm=hashes.SHA256())
    )

    cert_path = out_dir / "fullchain.pem"
    key_path = out_dir / "privkey.pem"

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    thumbprint = cert.fingerprint(hashes.SHA256()).hex()
    return cert_path, key_path, thumbprint


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    cert_path, key_path, thumbprint = generate_certificate(
        out_dir=Path(args.out_dir),
        common_name=args.common_name,
        organization=args.organization,
        country=args.country,
        days_valid=args.days_valid,
        dns_names=args.dns_names,
    )

    print(f"Written certificate: {cert_path}")
    print(f"Written private key: {key_path}")
    print(f"SHA256 fingerprint: {thumbprint}")
    print("This certificate is for development only.")


if __name__ == "__main__":
    main()
