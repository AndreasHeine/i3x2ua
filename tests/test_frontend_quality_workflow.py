"""The frontend must be gated the way the Python side is.

``frontend/package.json`` only offered ``dev``/``build``/``preview``, so CI ran ``npm ci`` and
``npm run build`` and nothing else: a component that does not compile, a lint error, or an advisory
in the installed tree reached the published image unnoticed while the Python side has ruff, mypy,
pytest, and pip-audit. The gates are the npm scripts, so CI and a developer run the same commands.

The audit gate is ``--audit-level=high``: the current lock carries low-severity advisories in the
Svelte toolchain that upstream has not released a fix for, so a gate on any advisory would block
every run without reporting something actionable.

SvelteKit 3 writes the configuration the project must extend to ``node_modules/$app/tsconfig.json``
and no longer writes ``.svelte-kit/tsconfig.json``. Both directories are gitignored, so a project
config that extends the generated path is unreadable in a clean checkout - CI reaches it as
``Cannot read file .../.svelte-kit/tsconfig.json`` while a developer's stale local copy hides the
failure. ``$app/tsconfig`` is the package export SvelteKit resolves from the installed version.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUALITY = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
PACKAGE_JSON = json.loads((PROJECT_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
SCRIPTS: dict[str, str] = PACKAGE_JSON["scripts"]
DEPENDENCIES: dict[str, str] = PACKAGE_JSON.get("devDependencies", {})
LOCKFILE = (PROJECT_ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")
JSCONFIG = json.loads((PROJECT_ROOT / "frontend" / "jsconfig.json").read_text(encoding="utf-8"))
VITE_CONFIG = (PROJECT_ROOT / "frontend" / "vite.config.js").read_text(encoding="utf-8")
ROUTES = PROJECT_ROOT / "frontend" / "src" / "routes"
FRONTEND_JOB = re.search(r"^  frontend-build:$", QUALITY, re.MULTILINE)


def _job_block(job: str) -> str:
    """The YAML text of one job: up to the next job key at indent 2, or the end of the file."""
    lines = QUALITY.splitlines()
    start = next((index for index, line in enumerate(lines) if line == f"  {job}:"), None)
    assert start is not None, f"quality.yml must define a {job!r} job"
    end = next(
        (index for index in range(start + 1, len(lines)) if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[index])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _run_steps(block: str) -> list[str]:
    """The commands the job runs, in order."""
    return re.findall(r"^\s+run:\s*(.+)$", block, re.MULTILINE)


def test_the_frontend_job_gates_lint_type_check_and_tests() -> None:
    """Without these steps the frontend is only ever checked by whatever compiles."""
    assert FRONTEND_JOB is not None, "quality.yml must keep the frontend-build job"

    block = _job_block("frontend-build")
    for script in ("lint", "check", "test"):
        assert f"npm run {script}" in block, f"the frontend job must run npm run {script}"


def test_the_frontend_job_audits_the_installed_tree() -> None:
    """The Python side audits with pip-audit; the JS side must audit the installed tree."""
    block = _job_block("frontend-build")

    assert "npm run audit" in block, "the frontend job must run the audit gate"
    assert "--audit-level=high" in SCRIPTS["audit"], (
        "the audit must gate actionable advisories, not every advisory the toolchain carries"
    )


def test_the_frontend_gates_run_before_the_build() -> None:
    """A gate that runs after the build cannot stop a broken frontend from shipping."""
    steps = _run_steps(_job_block("frontend-build"))
    positions = {}
    for script in ("lint", "check", "test", "audit"):
        positions[script] = next(index for index, step in enumerate(steps) if f"npm run {script}" in step)

    build = next(index for index, step in enumerate(steps) if "npm run build" in step)
    assert all(position < build for position in positions.values()), f"build runs at {build}: {positions}"


def test_the_ci_gates_are_the_scripts_a_developer_runs() -> None:
    """Regression: a workflow step may invoke a script package.json does not define."""
    assert SCRIPTS["lint"], "package.json must define a lint script"
    assert SCRIPTS["check"], "package.json must define a type-check script"
    assert SCRIPTS["test"], "package.json must define a test script"
    assert SCRIPTS["audit"], "package.json must define an audit script"

    block = _job_block("frontend-build")
    for script in ("lint", "check", "test", "audit"):
        assert f"npm run {script}" in block, f"CI must invoke the {script!r} script, not a bespoke command"


def test_the_lint_configuration_is_committed() -> None:
    """Regression: a lint script may name a configuration that was never committed."""
    config = PROJECT_ROOT / "frontend" / "eslint.config.js"
    assert config.is_file(), "the lint script needs a committed eslint configuration"

    text = config.read_text(encoding="utf-8")
    assert "eslint-plugin-svelte" in text, "svelte components need the svelte plugin's rules"


def test_the_helper_module_and_its_tests_are_committed() -> None:
    """The test script must run real tests; a runner with no test files passes vacuously."""
    assert (PROJECT_ROOT / "frontend" / "src" / "lib" / "format.js").is_file()
    assert (PROJECT_ROOT / "frontend" / "src" / "lib" / "format.test.js").is_file()


def test_the_page_imports_the_helpers_it_is_tested_against() -> None:
    """Regression: a copied helper drifts from the tested one, so the page must import the module."""
    page = (PROJECT_ROOT / "frontend" / "src" / "routes" / "model-builder" / "+page.svelte").read_text(encoding="utf-8")

    assert "../../lib/format.js" in page, "the model-builder page must import src/lib/format.js"
    for helper in ("sortedEntries", "qualityPct", "formatUtcTimestamp"):
        assert re.search(rf"const {helper} = ", page) is None, f"{helper} must live in src/lib/format.js"


def test_the_gate_dependencies_are_declared_and_locked() -> None:
    """``npm ci`` installs from the lockfile only, so every gate tool must be locked too."""
    for package in ("@eslint/js", "eslint", "eslint-plugin-svelte", "svelte-check", "typescript"):
        assert package in DEPENDENCIES, f"{package} must be a devDependency of the frontend"
        assert f'"node_modules/{package}"' in LOCKFILE, f"{package} must appear in package-lock.json"


def _page_routes() -> set[str]:
    """Every page route, as the URL its ``index.html`` is served at."""
    routes = set()
    for page in ROUTES.glob("**/+page.svelte"):
        relative = page.parent.relative_to(ROUTES).as_posix()
        routes.add(f"/{relative}" if page.parent != ROUTES else "/")
    return routes


def _prerender_entries() -> set[str]:
    """The URLs the kit config names in ``prerender.entries``."""
    match = re.search(r"entries:\s*\[([^\]]*)\]", VITE_CONFIG)
    assert match is not None, "the kit config must declare prerender entries"
    return set(re.findall(r"'([^']*)'", match.group(1)))


def test_the_type_check_extends_the_config_the_installed_kit_ships() -> None:
    """A clean checkout has no ``.svelte-kit/``, so extending that path cannot be resolved.

    Kit 3 writes the config a project extends to ``node_modules/$app/tsconfig.json`` and no longer
    writes ``.svelte-kit/tsconfig.json``. Both directories are gitignored, so CI - a fresh checkout
    after ``npm ci`` and ``svelte-kit sync`` - fails with ``Cannot read file ...`` while a developer
    still has the stale file a kit 2 run left behind. Extending the package export resolves against
    the installed kit instead of a generated directory.
    """
    extends = JSCONFIG.get("extends")
    assert extends == "$app/tsconfig", f"jsconfig.json must extend the kit export, not a path: {extends!r}"


def test_the_type_check_lists_the_sources_it_checks() -> None:
    """Kit 3 requires the project config to declare ``include``; without it nothing is checked.

    ``exclude`` must keep the service worker out, because it is compiled against its own config.
    """
    include = JSCONFIG.get("include")
    assert isinstance(include, list), "jsconfig.json must declare include"
    assert "src" in include, "the components and helpers must be type-checked"
    assert "src/service-worker" in JSCONFIG.get("exclude", []), "the service worker has its own config"


def test_node_builtin_types_are_declared_and_locked() -> None:
    """``src/lib/format.test.js`` imports ``node:test`` and ``node:assert/strict``.

    The kit config pins ``types`` to ``["$app/types"]``, so the builtins resolve only when ``node``
    is added to it and the type package is installed - and ``npm ci`` installs from the lockfile
    alone, so a declaration without a lock entry is invisible to CI.
    """
    types = JSCONFIG.get("compilerOptions", {}).get("types")
    assert isinstance(types, list), "jsconfig.json must declare types"
    assert "$app/types" in types, "kit refuses a types list that drops $app/types"
    assert "node" in types, "node builtins stay unresolved without the node types"
    assert "@types/node" in DEPENDENCIES, "@types/node must be a devDependency of the frontend"
    assert '"node_modules/@types/node"' in LOCKFILE, "@types/node must appear in package-lock.json"


def test_every_prerendered_page_is_named_as_a_prerender_entry() -> None:
    """``crawl: false`` means only ``prerender.entries`` reaches the prerenderer.

    The layout opts every page into prerendering, so a page absent from the list ships no
    ``index.html``: the gateway redirects its bare URL to the canonical trailing-slash form and the
    page then 404s.
    """
    assert "prerender = true" in (ROUTES / "+layout.js").read_text(encoding="utf-8")

    pages = _page_routes()
    entries = _prerender_entries()
    assert pages <= entries, f"prerender.entries must name every page route: missing {sorted(pages - entries)}"


def test_page_level_entries_are_only_valid_for_routes_with_params() -> None:
    """Kit 3 emits an ``EntryGenerator`` type only when a route has params.

    ``export const entries`` on a param-less page therefore fails ``npm run check``; the URLs belong
    in ``prerender.entries`` in the kit config instead.
    """
    for module in sorted(ROUTES.glob("**/+page.js")):
        if re.search(r"\[.+\]", module.parent.name):
            continue
        assert "export const entries" not in module.read_text(encoding="utf-8"), (
            f"{module} declares no params, so kit emits no EntryGenerator type for its entries export"
        )
