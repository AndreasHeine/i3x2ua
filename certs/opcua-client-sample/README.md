# OPC UA Client Sample Certificate

This directory contains a development-only OPC UA client certificate bundle used to test encrypted OPC UA sessions with this project.

Files:

- `client-cert.pem`: sample client certificate (PEM)
- `client-key.pem`: matching private key (PEM, unencrypted)

OPC UA compatibility profile used by this sample certificate:

- Subject Alternative Name (SAN) includes application URI: `urn:i3x2ua:opcua_client` (the application URI the gateway declares in `i3x_server/infrastructure/opcua/client.py`)
- SAN includes DNS names: `localhost`. The committed sample carries no host name; the local
  host name is added only when the certificate is generated with `--include-hostname`.
- SAN IP addresses are written as `iPAddress` when `--ip` is used.
- Key Usage includes: `digitalSignature`, `nonRepudiation/contentCommitment`, `keyEncipherment`, `dataEncipherment`
- Extended Key Usage (EKU) includes: `clientAuth` and `serverAuth`

These fields are commonly required by OPC UA servers. Missing fields can cause errors such as `BadCertificateUseNotAllowed` or endpoint rejection during `OpenSecureChannel`.

Use with environment variables:

- `I3X_OPCUA_CLIENT_CERT_PATH=./certs/opcua-client-sample/client-cert.pem`
- `I3X_OPCUA_CLIENT_KEY_PATH=./certs/opcua-client-sample/client-key.pem`

Generate/regenerate this bundle with:

```bash
uv run python scripts/generate_opcua_client_cert.py
```

Optional custom values:

```bash
uv run python scripts/generate_opcua_client_cert.py \
	--app-uri urn:my-company:my-opcua-client \
	--common-name my-opcua-client \
	--organization "My Company" \
	--dns opc-client-host --dns localhost \
	--ip 127.0.0.1 \
	--days-valid 365 \
	--include-hostname
```

`--days-valid` must be positive. `--include-hostname` is off by default so the certificate
does not carry your machine name; pass it only when the target server validates the host name.

Important:

- The files in this directory are committed to a public repository, so `client-key.pem` is
  public sample material with no confidentiality or identity value.
- This bundle is not intended for production use.
- Many OPC UA servers require trusting/importing the client certificate before secure connection will succeed.
- For production, generate unique per-environment certificates and protect private keys with your standard secret management process.
- When creating custom certificates, keep the compatibility profile above and ensure the security policy/mode in your client matches a policy/mode exposed by the target OPC UA endpoint.
