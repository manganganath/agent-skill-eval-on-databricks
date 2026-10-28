"""The repository is public: lockfiles must point only at the public package registries.

A lock written behind a corporate package proxy records the proxy's host for every package. That leaks an
internal hostname and breaks installs on GitHub-hosted runners, so re-lock against the public registries.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPI_HOSTS = {"pypi.org", "files.pythonhosted.org"}
NPM_HOSTS = {"registry.npmjs.org"}


def _hosts(urls: list[str]) -> set[str]:
    return {re.match(r"https://([^/]+)/", u).group(1) for u in urls}


def test_uv_lock_uses_only_public_pypi():
    text = (ROOT / "uv.lock").read_text(encoding="utf-8")
    urls = re.findall(r'(?:registry|url) = "(https://[^"]+)"', text)
    assert urls, "no package URLs found in uv.lock"
    assert _hosts(urls) <= PYPI_HOSTS, sorted(_hosts(urls) - PYPI_HOSTS)


def test_package_lock_uses_only_public_npm():
    lock = json.loads((ROOT / "app" / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    urls = [p["resolved"] for p in lock.get("packages", {}).values() if "resolved" in p]
    assert urls, "no resolved URLs found in package-lock.json"
    assert _hosts(urls) <= NPM_HOSTS, sorted(_hosts(urls) - NPM_HOSTS)
