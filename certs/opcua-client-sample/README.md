# OPC UA Client Sample Certificate

This directory holds a development-only OPC UA client certificate bundle used to test encrypted
OPC UA sessions with this project. The bundle is generated at setup and is not tracked by git
(`certs/**/*.pem` is ignored), so a fresh clone contains only this README.

Generate the files with:

```bash
uv run python scripts/generate_opcua_client_cert.py
```

The script writes `client-cert.pem` (client certificate) and `client-key.pem` (matching private
key, unencrypted) into `certs/opcua-client-sample/`.

OPC UA compatibility profile used by the generated certificate:

- Subject Alternative Name (SAN) includes application URI: `urn:i3x2ua:opcua_client` (the application URI the gateway declares in `i3x_server/infrastructure/opcua/client.py`)
- SAN includes DNS names: `localhost`. The generated sample carries no host name; the local
  host name is added only when the certificate is generated with `--include-hostname`.
- SAN IP addresses are written as `iPAddress` when `--ip` is used.
- Key Usage includes: `digitalSignature`, `nonRepudiation/contentCommitment`, `keyEncipherment`, `dataEncipherment`
- Extended Key Usage (EKU) includes: `clientAuth` and `serverAuth`

These fields are commonly required by OPC UA servers. Missing fields can cause errors such as `BadCertificateUseNotAllowed` or endpoint rejection during `OpenSecureChannel`.

Use with environment variables:

- `I3X_OPCUA_CLIENT_CERT_PATH=./certs/opcua-client-sample/client-cert.pem`
- `I3X_OPCUA_CLIENT_KEY_PATH=./certs/opcua-client-sample/client-key.pem`

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

- The bundle is generated at setup and never committed: the private key stays out of the
  repository, and git ignores `certs/**/*.pem`.
- The sample bundle that used to be committed here is **withdrawn**. Its private key was
  published in a public repository, so an OPC UA server that trusted that client certificate must
  untrust it, and the certificate must not be trusted anywhere.
- This bundle is not intended for production use.
- Many OPC UA servers require trusting/importing the client certificate before secure connection will succeed.
- For production, generate unique per-environment certificates and protect private keys with your standard secret management process.
- When creating custom certificates, keep the compatibility profile above and ensure the security policy/mode in your client matches a policy/mode exposed by the target OPC UA endpoint.
