"""Shared test isolation.

One rule here, and it exists because breaking it produced a green suite on a
developer machine and a red one in CI: **no test may read the host's real
credentials.**

The Claude credential check consults the macOS Keychain, so on a logged-in
laptop it answers "yes" for any test that touches capacity. Two tests were
passing for that reason alone. One asserted that an empty `{}` credentials file
counts as a login, which is the exact opposite of what the code does and what
another test in the same file asserts; the other never intended to exercise
capacity at all and only got past the harness-headroom gate because the
Keychain vouched for it.

Neither would have failed on this machine no matter how wrong the code was.
"""

from __future__ import annotations

import pytest

#: Every environment variable that can make a harness look configured. Cleared
#: for every test so an exported token in the developer's shell cannot decide
#: the outcome of an assertion.
CREDENTIAL_ENV = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "CTSWARM_EXECUTION_MODE",
)


@pytest.fixture(autouse=True)
def no_host_credentials(monkeypatch, tmp_path_factory):
    """Give every test a host with no credentials of any kind.

    Three sources are cut off, because credentials are checked in three places
    and a test only has to reach one of them to be answered by the machine it
    happens to run on:

    - the macOS Keychain, consulted for the Claude login
    - ``Path.home()``, where the Claude and Codex credential files live
    - the ambient environment

    Autouse rather than opt-in. A test that forgets an opt-in fixture does not
    fail; it silently starts trusting the developer's own login, and the
    failure surfaces later on a machine nobody is watching. A test that wants a
    login present should create the artifact under ``Path.home()``, which now
    resolves somewhere disposable and behaves identically on Linux and macOS.
    """
    monkeypatch.setattr(
        "ctswarm.capacity._KEYCHAIN_CACHE", {"claude": (float("inf"), False)}
    )
    monkeypatch.setattr("ctswarm.capacity._KEYCHAIN_TTL_S", float("inf"))

    empty_home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr("pathlib.Path.home", lambda: empty_home)
    monkeypatch.setenv("HOME", str(empty_home))

    for name in CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
