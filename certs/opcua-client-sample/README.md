# OPC UA Client Sample Certificate

This directory contains a development-only OPC UA client certificate bundle used to test encrypted OPC UA sessions with this project.

Files:

- `client-cert.pem`: sample client certificate (PEM)
- `client-key.pem`: matching private key (PEM, unencrypted)

OPC UA compatibility profile used by this sample certificate:

- Subject Alternative Name (SAN) includes application URI: `urn:example.org:FreeOpcUa:opcua-asyncio`
- SAN includes DNS names: `localhost` and the local host name
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
	--dns opc-client-host --dns localhost
```

Important:

- This bundle is not intended for production use.
- Many OPC UA servers require trusting/importing the client certificate before secure connection will succeed.
- For production, generate unique per-environment certificates and protect private keys with your standard secret management process.
- When creating custom certificates, keep the compatibility profile above and ensure the security policy/mode in your client matches a policy/mode exposed by the target OPC UA endpoint.
