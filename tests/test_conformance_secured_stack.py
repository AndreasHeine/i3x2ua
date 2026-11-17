"""CI must exercise the HTTPS/auth deployment stack, not only the plain-HTTP fixture check.

The gateway authenticates nothing itself and serves plain HTTP; nginx provides TLS, Basic Auth on
everything but the public ``/v1/info``, and the HTTP->HTTPS redirect (``nginx/entrypoint.sh``). A
conformance run against the gateway's own port proves the i3X contract but never crosses those
layers, so an auth or TLS regression in the proxy config would ship unnoticed. The secured job
brings the real proxy image up and runs the same suite through it.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUALITY = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")

_JOB_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):$")
_STEP_START = re.compile(r"^      - name: (.+)$")


def _job_block(job: str) -> str:
    """The YAML text of one job, i.e. up to the next job key at indent 2 (or the end of file)."""
    lines = QUALITY.splitlines()
    start = next((index for index, line in enumerate(lines) if line == f"  {job}:"), None)
    assert start is not None, f"quality.yml must define a {job!r} job"
    end = next(
        (index for index in range(start + 1, len(lines)) if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[index])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _job_names() -> list[str]:
    return [match.group(1) for match in (_JOB_KEY.match(line) for line in QUALITY.splitlines()) if match]


def _steps(job: str) -> list[tuple[str, str]]:
    """The ``(name, body)`` pairs of a job's steps."""
    lines = _job_block(job).splitlines()
    starts = [index for index, line in enumerate(lines) if _STEP_START.match(line)]
    steps: list[tuple[str, str]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        header = _STEP_START.match(lines[start])
        assert header is not None
        steps.append((header.group(1), "\n".join(lines[start + 1 : end])))
    return steps


def _step(job: str, name_fragment: str) -> str:
    match = next((body for name, body in _steps(job) if name_fragment.lower() in name.lower()), None)
    assert match is not None, f"{job} must have a step matching {name_fragment!r}"
    return match


def _secured_job() -> str:
    """The job that runs conformance through the secured proxy stack."""
    match = next((job for job in _job_names() if "secured" in job), None)
    assert match is not None, "quality.yml must run conformance against the secured stack"
    return match


def test_a_secured_conformance_job_exists_alongside_the_plain_one() -> None:
    """The roadmap asks for the secured stack *in addition to* the fixture-server check."""
    secured = _secured_job()
    assert "conformance" in secured
    plain = next((job for job in _job_names() if job == "conformance"), None)
    assert plain is not None, "the plain fixture-server conformance check must stay in place"


def test_the_secured_job_builds_the_proxy_image_it_ships() -> None:
    """Running nginx some other way would test a configuration this repository does not deploy."""
    block = _job_block(_secured_job())

    assert "nginx/Dockerfile" in block, "the job must build the repository's proxy image"
    assert "nginx.conf" not in block, "the proxy config comes from the image, not from CI overrides"


def test_the_secured_job_generates_a_development_certificate_instead_of_committing_one() -> None:
    """TLS needs material; committed key material is what test_no_committed_key_material forbids."""
    secured = _secured_job()
    script = _step(secured, "certificate")

    assert "generate_https_dev_cert.py" in script, "the dev certificate must be generated at run time"
    assert "privkey.pem" in _job_block(secured), "the proxy must be pointed at the generated key"


def test_the_secured_job_enables_tls_and_basic_auth() -> None:
    """Both layers are the point of the job; a stack without them is the plain check again."""
    script = _step(_secured_job(), "proxy")

    assert "NGINX_HTTPS_ENABLED=1" in script or "NGINX_HTTPS_ENABLED: 1" in script
    assert "NGINX_BASIC_AUTH_ENABLED=1" in script or "NGINX_BASIC_AUTH_ENABLED: 1" in script
    assert "NGINX_BASIC_AUTH_USER" in script, "auth must be configured with a user, not left empty"
    assert "NGINX_BASIC_AUTH_PASSWORD" in script, "auth must be configured with a password, not left empty"


def test_the_secured_job_reaches_the_loopback_gateway_through_the_host_network() -> None:
    """Regression: `host-gateway` is the docker bridge IP on Linux, not the host's loopback.

    The gateway binds 127.0.0.1, so a proxied request aimed at the bridge gateway never reaches
    it and the readiness probe times out (the CI run of 5cbd4a4 failed exactly there). Docker
    Desktop forwards host-gateway to localhost, which is why the local check passed. The proxy
    therefore joins the host network namespace and reaches the gateway on loopback directly.
    """
    script = _step(_secured_job(), "proxy")

    assert "--network host" in script, "the proxy must share the runner's network namespace"
    assert "NGINX_UPSTREAM_HOST=127.0.0.1" in script, "the upstream is the loopback gateway"
    assert "host.docker.internal" not in script, "the bridge gateway cannot reach a loopback server"
    assert "-p 8080:80" not in script, "host networking publishes ports directly; mapping is meaningless"
    assert "NGINX_HTTP_PORT=8080" in script, "the proxy listens on the host port the asserts curl"
    assert "NGINX_HTTPS_PORT=8443" in script, "the proxy listens on the host port the suite reaches"


def test_the_secured_job_asserts_the_edge_behaviour_directly() -> None:
    """Auth and redirect must be proven at the proxy, not inferred from a suite that passes."""
    block = _job_block(_secured_job())

    assert re.search(r"\b301\b", block), "the HTTP->HTTPS redirect must be asserted"
    assert re.search(r"\b401\b", block), "an unauthenticated request must be asserted to be refused"


def test_redirect_probe_waits_for_proxy_startup_and_reports_failures() -> None:
    """A detached container can take time to open its listening sockets."""
    script = _step(_secured_job(), "HTTP redirects")

    assert "--retry-connrefused" in script
    assert re.search(r"--retry\s+[1-9]\d*", script)
    assert re.search(r"--retry-max-time\s+[1-9]\d*", script)
    assert re.search(r"--max-time\s+[1-9]\d*", script)
    assert "docker logs i3x2ua-proxy-ci" in script
    assert "exit 1" in script


def test_the_secured_job_runs_the_suite_through_the_proxy() -> None:
    """The suite has to carry credentials and accept the self-signed certificate."""
    script = _step(_secured_job(), "conformance")

    assert "--scheme https" in script, "the suite must be pointed at the TLS endpoint"
    assert "--public-port" in script, "the suite must be pointed at the proxy port, not the gateway port"
    assert "--basic" in script, "the suite must send credentials, or every request answers 401"
    assert "--insecure" in script, "the self-signed development certificate must be accepted explicitly"
    assert "--json" in script, "the verdict gate needs a report to check"


def test_the_proxy_is_cleaned_up_even_when_the_suite_fails() -> None:
    """A failed run must not leave a container holding the published port for later jobs."""
    block = _job_block(_secured_job())

    assert "--rm" in block or "docker rm -f" in block, "the proxy container must be removed"
    assert "if: always()" in block, "the cleanup must run after a failing suite"


def test_the_secured_report_is_uploaded_as_an_artifact() -> None:
    """The evidence a reviewer needs is the verdict of the secured run, not only its exit code."""
    block = _job_block(_secured_job())

    assert "i3x-conformance-report-secured" in block, "the secured report must be uploaded"
