import subprocess

import pytest

from mien.verify import (
    Status,
    probe_aws,
    probe_github,
    probe_google,
)


def _fake_run(mocker, *, stdout=b"", returncode=0, stderr=b"", raises=None):
    """Stub the one subprocess both probes shell out through."""
    run = mocker.patch("mien.verify.subprocess.run")
    if raises is not None:
        run.side_effect = raises
    else:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr
        )
    return run


class TestGithub:
    """`gh api user -q .login` reports who the token actually authenticates as,
    which is compared to the profile's configured username."""


    def test_match(self, mocker):
        _fake_run(mocker, stdout=b"octocat\n")
        r = probe_github("octocat", {"GH_TOKEN": "x"})
        assert r.status is Status.MATCH
        assert r.live == "octocat"
        assert r.configured == "octocat"

    def test_mismatch_is_flagged(self, mocker):
        _fake_run(mocker, stdout=b"someone-else\n")
        r = probe_github("octocat", {"GH_TOKEN": "x"})
        assert r.status is Status.MISMATCH
        assert r.live == "someone-else"
        assert r.configured == "octocat"

    def test_revoked_token_is_unauthorized_not_mismatch(self, mocker):
        # gh exits non-zero and says so on stderr when the token is bad.
        _fake_run(mocker, returncode=1,
                 stderr=b"HTTP 401: Bad credentials (https://api.github.com/user)")
        r = probe_github("octocat", {"GH_TOKEN": "x"})
        assert r.status is Status.UNAUTHORIZED
        assert r.live is None

    def test_network_failure_is_unreachable_not_unauthorized(self, mocker):
        # gh also exits non-zero when it cannot reach the network. That is a
        # could-not-check, NOT a dead credential — misreporting it as
        # UNAUTHORIZED would fail the gate offline and name the wrong remedy.
        _fake_run(mocker, returncode=1,
                 stderr=b"error connecting to api.github.com: no such host")
        r = probe_github("octocat", {"GH_TOKEN": "x"})
        assert r.status is Status.UNREACHABLE
        assert r.live is None

    def test_gh_not_installed_is_unavailable(self, mocker):
        _fake_run(mocker, raises=FileNotFoundError())
        r = probe_github("octocat", {"GH_TOKEN": "x"})
        assert r.status is Status.UNAVAILABLE

    def test_non_executable_gh_does_not_crash(self, mocker):
        # A present-but-non-executable gh raises PermissionError (an OSError).
        # The probe must not crash — one broken tool must not hide the others.
        _fake_run(mocker, raises=PermissionError(13, "Permission denied"))
        r = probe_github("octocat", {"GH_TOKEN": "x"})
        assert r.status in (Status.UNAVAILABLE, Status.UNREACHABLE)
        assert r.live is None

    def test_probe_runs_under_the_supplied_env(self, mocker):
        run = _fake_run(mocker, stdout=b"octocat\n")
        probe_github("octocat", {"GH_TOKEN": "profile-token"})
        assert run.call_args.kwargs["env"]["GH_TOKEN"] == "profile-token"


class TestAws:
    """AWS has no configured subject to compare against — a profile name is not
    an ARN — so a successful probe reports the live caller as UNCOMPARABLE, not
    MATCH, while auth and reachability failures are still distinguished."""


    def test_success_is_uncomparable_with_the_live_arn(self, mocker):
        _fake_run(mocker, stdout=b"arn:aws:iam::123456789012:user/dev\n")
        r = probe_aws("work", {"AWS_PROFILE": "work"})
        assert r.status is Status.UNCOMPARABLE
        assert r.live == "arn:aws:iam::123456789012:user/dev"
        assert r.configured == "work"

    def test_expired_credentials_are_unauthorized(self, mocker):
        _fake_run(mocker, returncode=254,
                  stderr=b"An error occurred (ExpiredToken) when calling ...")
        r = probe_aws("work", {"AWS_PROFILE": "work"})
        assert r.status is Status.UNAUTHORIZED

    def test_network_failure_is_unreachable(self, mocker):
        _fake_run(mocker, returncode=255,
                  stderr=b"Could not connect to the endpoint URL: ...")
        r = probe_aws("work", {"AWS_PROFILE": "work"})
        assert r.status is Status.UNREACHABLE

    def test_aws_not_installed_is_unavailable(self, mocker):
        _fake_run(mocker, raises=FileNotFoundError())
        r = probe_aws("work", {})
        assert r.status is Status.UNAVAILABLE


class TestGoogle:
    """Google is checked by minting an access token from the stored refresh
    token and asking the userinfo endpoint for the email, compared to config."""

    def test_match(self, mocker):
        mocker.patch("mien.verify.exchange_refresh_token", return_value="ya29.tok")
        mocker.patch("mien.verify._userinfo_email", return_value="me@example.com")
        r = probe_google("me@example.com", "cid", "csec", "refresh")
        assert r.status is Status.MATCH
        assert r.live == "me@example.com"

    def test_mismatch(self, mocker):
        mocker.patch("mien.verify.exchange_refresh_token", return_value="ya29.tok")
        mocker.patch("mien.verify._userinfo_email", return_value="other@example.com")
        r = probe_google("me@example.com", "cid", "csec", "refresh")
        assert r.status is Status.MISMATCH

    def test_revoked_refresh_token_is_unauthorized(self, mocker):
        # A revoked refresh token fails at the token exchange with 400
        # invalid_grant. exchange_refresh_token uses httpx, so the exception is
        # httpx.HTTPStatusError — not urllib. This is the shape the real code
        # raises; a smoke test against a live revoked token proved the earlier
        # urllib-based handling never fired.
        import httpx
        resp = httpx.Response(400, request=httpx.Request("POST", "https://x"))
        mocker.patch(
            "mien.verify.exchange_refresh_token",
            side_effect=httpx.HTTPStatusError(
                "invalid_grant", request=resp.request, response=resp
            ),
        )
        r = probe_google("me@example.com", "cid", "csec", "refresh")
        assert r.status is Status.UNAUTHORIZED
        assert r.live is None

    def test_server_error_is_unreachable_not_unauthorized(self, mocker):
        import httpx
        resp = httpx.Response(503, request=httpx.Request("POST", "https://x"))
        mocker.patch(
            "mien.verify.exchange_refresh_token",
            side_effect=httpx.HTTPStatusError(
                "unavailable", request=resp.request, response=resp
            ),
        )
        r = probe_google("me@example.com", "cid", "csec", "refresh")
        assert r.status is Status.UNREACHABLE

    def test_network_failure_is_unreachable(self, mocker):
        import httpx
        mocker.patch(
            "mien.verify.exchange_refresh_token",
            side_effect=httpx.ConnectError("no route to host"),
        )
        r = probe_google("me@example.com", "cid", "csec", "refresh")
        assert r.status is Status.UNREACHABLE

    def test_non_json_200_does_not_crash(self, mocker):
        # A 200 with a non-JSON body (a proxy login page, say) makes resp.json()
        # raise. The probe must not crash — never-raises is unconditional.
        import httpx
        mocker.patch("mien.verify.exchange_refresh_token", return_value="ya29.tok")
        resp = httpx.Response(200, text="<html>not json</html>",
                              request=httpx.Request("GET", "https://x"))
        mocker.patch("mien.verify.httpx.get", return_value=resp)
        r = probe_google("me@example.com", "cid", "csec", "refresh")
        assert r.status is Status.UNREACHABLE
        assert r.live is None

    def test_userinfo_without_email_does_not_crash(self, mocker):
        # A token minted without the email scope yields a 200 with no "email".
        # The probe must not raise — that breaks the "never raises" contract and
        # takes the whole --live run down.
        import httpx
        mocker.patch("mien.verify.exchange_refresh_token", return_value="ya29.tok")
        resp = httpx.Response(200, json={"sub": "12345"},
                              request=httpx.Request("GET", "https://x"))
        mocker.patch("mien.verify.httpx.get", return_value=resp)
        r = probe_google("me@example.com", "cid", "csec", "refresh")
        assert r.status in (Status.UNREACHABLE, Status.UNAUTHORIZED)
        assert r.live is None

    def test_non_object_json_200_does_not_crash(self, mocker):
        # A 200 body of `null` (a proxy or a misbehaving endpoint) makes
        # resp.json() return None; None.get(...) raises AttributeError, which is
        # neither ValueError nor KeyError. The probe must still not crash.
        import httpx
        mocker.patch("mien.verify.exchange_refresh_token", return_value="ya29.tok")
        resp = httpx.Response(200, json=None,
                              request=httpx.Request("GET", "https://x"))
        mocker.patch("mien.verify.httpx.get", return_value=resp)
        r = probe_google("me@example.com", "cid", "csec", "refresh")
        assert r.status is Status.UNREACHABLE
        assert r.live is None


def test_run_probe_safely_never_propagates():
    """The backstop: any exception a probe fails to classify becomes an
    UNREACHABLE result, so one broken probe cannot crash --live."""
    from mien.verify import Status, run_probe_safely

    def boom():
        raise RuntimeError("something a probe forgot to catch")

    r = run_probe_safely("google", boom)
    assert r.status is Status.UNREACHABLE
    assert r.service == "google"
    assert "something a probe forgot" in r.detail
