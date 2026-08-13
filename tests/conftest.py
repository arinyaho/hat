import pytest

from mien.shell import CAPTURE_MARKER_VARS


@pytest.fixture(autouse=True)
def _no_capture_context(monkeypatch):
    """Neutralize agent-harness markers for every test.

    `mien token` refuses to print a secret when it detects a harness that records
    stdout. The test suite is frequently *run* from inside such a harness, so
    without this the same test would pass locally and fail under an agent — the
    behaviour under test would depend on who invoked pytest. Tests that exercise
    the refusal set a marker explicitly.

    `MIEN_TOKEN` is cleared for the mirror-image reason: it *disarms* the
    refusal, so an ambient `MIEN_TOKEN=capture-ok` would silently turn the
    refusal tests green-by-default.
    """
    for marker in (*CAPTURE_MARKER_VARS, "MIEN_TOKEN"):
        monkeypatch.delenv(marker, raising=False)


def make_config(*, profiles):
    """A Config with the boilerplate every test repeats verbatim.

    Twenty-two call sites built this literal inline, identical but for
    `profiles`, so adding a field to Config meant editing all of them.
    """
    from mien.config import BackendConfig, Config, SecretNaming
    from mien.secret_naming import BUILTIN_DEFAULT, BUILTIN_SLACK_TOKEN
    return Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={},
        secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles=profiles,
    )
