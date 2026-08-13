import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mien.backends.base import SecretNotFound
from mien.config import (
    AWSService,
    AtlassianService,
    GitHubService,
    GoogleService,
    NotionService,
    OCIService,
    Profile,
    SlackWorkspace,
)
from mien.env import build_env, plan_env


@pytest.fixture
def fake_backend():
    b = MagicMock()
    b.get.side_effect = lambda ref: {
        "refresh-ref": b"refresh-tok",
        "csec-ref": b"client-secret-val",
        "gh-token-ref": b"ghp_xxx",
        "slack-team-a-ref": b"xoxp-aaa",
        "slack-team-b-ref": b"xoxp-bbb",
        "aws-key-ref": b"AKIAIOSFODNN7EXAMPLE",
        "aws-secret-ref": b"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    }[ref]
    return b


def test_google_synthesizes_adc_from_refresh_token(monkeypatch, tmp_path, fake_backend):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    prof = Profile(
        name="personal",
        google=GoogleService(
            email="me@x.com",
            oauth_client_id="cid-123",
            oauth_client_secret_ref="csec-ref",
            refresh_token_ref="refresh-ref",
            adc_ref=None,
            gcloud_config_name="personal",
            default_project="myproj",
        ),
    )
    bundle = build_env(prof, fake_backend, pid=111)
    env = bundle.env
    assert env["MIEN_PROFILE"] == "personal"
    assert env["CLOUDSDK_ACTIVE_CONFIG_NAME"] == "personal"
    assert env["CLOUDSDK_CORE_PROJECT"] == "myproj"
    adc_path = Path(env["GOOGLE_APPLICATION_CREDENTIALS"])
    assert adc_path.exists()
    payload = json.loads(adc_path.read_text())
    assert payload == {
        "type": "authorized_user",
        "client_id": "cid-123",
        "client_secret": "client-secret-val",
        "refresh_token": "refresh-tok",
    }
    assert (adc_path.stat().st_mode & 0o777) == 0o600
    assert "GH_TOKEN" not in env
    assert "MIEN_SLACK_TOKENS" not in env
    assert fake_backend.get.call_count == 2  # refresh_token_ref + oauth_client_secret_ref


def test_skips_adc_without_an_oauth_client_secret(monkeypatch, tmp_path, fake_backend):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    prof = Profile(
        name="work",
        google=GoogleService(
            email="me@h.com",
            oauth_client_id="cid",
            oauth_client_secret_ref=None,
            refresh_token_ref="refresh-ref",
            adc_ref=None,
            gcloud_config_name="work",
            default_project="hp",
        ),
    )
    bundle = build_env(prof, fake_backend, pid=222)
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in bundle.env
    fake_backend.get.assert_not_called()


def test_github_sets_gh_token(monkeypatch, tmp_path, fake_backend):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    prof = Profile(
        name="work",
        github=GitHubService(username="u", host="github.com", token_ref="gh-token-ref"),
    )
    bundle = build_env(prof, fake_backend, pid=333)
    assert bundle.env["GH_TOKEN"] == "ghp_xxx"


def test_github_ssh_key_path_sets_git_ssh_command(monkeypatch, tmp_path, fake_backend):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    prof = Profile(
        name="work",
        github=GitHubService(
            username="u", host="github.com", token_ref=None,
            ssh_key_path="/home/u/.ssh/id_work",
        ),
    )
    bundle = build_env(prof, fake_backend, pid=701)
    assert "GH_TOKEN" not in bundle.env
    assert bundle.env["GIT_SSH_COMMAND"] == "ssh -i /home/u/.ssh/id_work -o IdentitiesOnly=yes"


def test_github_ssh_key_ref_materializes_ephemeral_file(monkeypatch, tmp_path, fake_backend):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    fake_backend.get.side_effect = lambda ref: {"ssh-ref": b"PRIVATE-KEY-BYTES"}[ref]
    prof = Profile(
        name="work",
        github=GitHubService(
            username="u", host="github.com", token_ref=None,
            ssh_key_ref="ssh-ref",
        ),
    )
    bundle = build_env(prof, fake_backend, pid=702)
    cmd = bundle.env["GIT_SSH_COMMAND"]
    assert cmd.startswith("ssh -i ")
    assert cmd.endswith(" -o IdentitiesOnly=yes")
    key_path = Path(cmd.split(" ")[2])
    assert key_path.read_bytes() == b"PRIVATE-KEY-BYTES"
    assert (key_path.stat().st_mode & 0o777) == 0o600


def test_slack_writes_workspace_map(monkeypatch, tmp_path, fake_backend):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    prof = Profile(
        name="p",
        slack=[
            SlackWorkspace(workspace="team-a", user_token_ref="slack-team-a-ref"),
            SlackWorkspace(workspace="team-b", user_token_ref="slack-team-b-ref"),
        ],
    )
    bundle = build_env(prof, fake_backend, pid=444)
    p = Path(bundle.env["MIEN_SLACK_TOKENS"])
    payload = json.loads(p.read_text())
    assert payload == {"team-a": "xoxp-aaa", "team-b": "xoxp-bbb"}
    assert (p.stat().st_mode & 0o777) == 0o600
    prof2 = Profile(
        name="solo",
        slack=[SlackWorkspace(workspace="team-a", user_token_ref="slack-team-a-ref")],
    )
    b2 = build_env(prof2, fake_backend, pid=555)
    assert b2.env["MIEN_SLACK_DEFAULT_TOKEN"] == "xoxp-aaa"


def test_aws_with_keys_sets_env(monkeypatch, tmp_path, fake_backend):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    prof = Profile(
        name="work",
        aws=AWSService(
            region="us-east-1",
            access_key_id_ref="aws-key-ref",
            secret_access_key_ref="aws-secret-ref",
        ),
    )
    bundle = build_env(prof, fake_backend, pid=600)
    assert bundle.env["AWS_ACCESS_KEY_ID"] == "AKIAIOSFODNN7EXAMPLE"
    assert bundle.env["AWS_SECRET_ACCESS_KEY"] == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    assert bundle.env["AWS_DEFAULT_REGION"] == "us-east-1"
    assert "AWS_PROFILE" not in bundle.env


def test_aws_with_profile_sets_env(monkeypatch, tmp_path, fake_backend):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    prof = Profile(
        name="work",
        aws=AWSService(profile="work-sso", region="ap-northeast-2"),
    )
    bundle = build_env(prof, fake_backend, pid=601)
    assert bundle.env["AWS_PROFILE"] == "work-sso"
    assert bundle.env["AWS_DEFAULT_REGION"] == "ap-northeast-2"
    assert "AWS_ACCESS_KEY_ID" not in bundle.env


def test_oci_sets_profile_env(monkeypatch, tmp_path, fake_backend):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    prof = Profile(
        name="work",
        oci=OCIService(profile="WORK", config_file="/home/u/.oci/work.ini"),
    )
    bundle = build_env(prof, fake_backend, pid=700)
    assert bundle.env["OCI_CLI_PROFILE"] == "WORK"
    assert bundle.env["OCI_CLI_CONFIG_FILE"] == "/home/u/.oci/work.ini"


def test_atlassian_sets_env(monkeypatch, tmp_path, fake_backend):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    fake_backend.get.side_effect = lambda ref: {
        **{
            "refresh-ref": b"refresh-tok",
            "csec-ref": b"client-secret-val",
            "gh-token-ref": b"ghp_xxx",
            "slack-team-a-ref": b"xoxp-aaa",
            "slack-team-b-ref": b"xoxp-bbb",
            "aws-key-ref": b"AKIAIOSFODNN7EXAMPLE",
            "aws-secret-ref": b"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        },
        "atl-token-ref": b"atl-api-token-xyz",
    }[ref]
    prof = Profile(
        name="work",
        atlassian=AtlassianService(
            email="me@company.com",
            base_url="https://company.atlassian.net",
            api_token_ref="atl-token-ref",
        ),
    )
    bundle = build_env(prof, fake_backend, pid=800)
    assert bundle.env["ATLASSIAN_EMAIL"] == "me@company.com"
    assert bundle.env["ATLASSIAN_API_TOKEN"] == "atl-api-token-xyz"
    assert bundle.env["ATLASSIAN_BASE_URL"] == "https://company.atlassian.net"


def test_notion_sets_env(monkeypatch, tmp_path, fake_backend):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    fake_backend.get.side_effect = lambda ref: {
        **{
            "refresh-ref": b"refresh-tok",
            "csec-ref": b"client-secret-val",
            "gh-token-ref": b"ghp_xxx",
            "slack-team-a-ref": b"xoxp-aaa",
            "slack-team-b-ref": b"xoxp-bbb",
            "aws-key-ref": b"AKIAIOSFODNN7EXAMPLE",
            "aws-secret-ref": b"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        },
        "notion-token-ref": b"notion-secret-tok",
    }[ref]
    prof = Profile(
        name="work",
        notion=NotionService(api_token_ref="notion-token-ref"),
    )
    bundle = build_env(prof, fake_backend, pid=810)
    assert bundle.env["NOTION_TOKEN"] == "notion-secret-tok"


def test_custom_variables_arrive_under_the_names_the_profile_chose(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    backend = MagicMock()
    backend.get.side_effect = lambda ref: {
        "anthropic-ref": b"sk-ant-secret\n",   # trailing newline, as a store returns
        "npm-ref": b"npm_tok",
    }[ref]
    prof = Profile(name="work", custom={
        "ANTHROPIC_API_KEY": "anthropic-ref", "NPM_TOKEN": "npm-ref"})
    env = build_env(prof, backend, pid=222).env
    # Stripped like every other secret mien reads, so a store that appends a
    # newline does not put one in the header the value ends up in.
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-secret"
    assert env["NPM_TOKEN"] == "npm_tok"


def test_a_profile_with_no_custom_variables_adds_none(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    backend = MagicMock()
    env = build_env(Profile(name="bare"), backend, pid=223).env
    assert set(env) == {"MIEN_PROFILE", "MIEN_EPHEMERAL_DIR"}
    backend.get.assert_not_called()


def test_a_dangling_custom_ref_says_which_variable_of_which_profile(monkeypatch, tmp_path):
    """The backend raises with the ref alone; that is not enough to act on."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    backend = MagicMock()
    backend.get.side_effect = SecretNotFound("anthropic-ref")
    prof = Profile(name="work", custom={"ANTHROPIC_API_KEY": "anthropic-ref"})
    with pytest.raises(SecretNotFound) as excinfo:
        build_env(prof, backend, pid=225)
    message = str(excinfo.value)
    assert "anthropic-ref" in message
    assert "ANTHROPIC_API_KEY" in message
    assert "'work'" in message


def test_a_custom_variable_is_no_secret_on_disk(monkeypatch, tmp_path):
    """It goes in the environment, not into an ephemeral file.

    File delivery is deliberately out of scope: the value is exported directly,
    so nothing here writes it anywhere `mien doctor --gc` would have to reclaim.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    backend = MagicMock()
    backend.get.return_value = b"sk-ant-secret"
    bundle = build_env(
        Profile(name="work", custom={"ANTHROPIC_API_KEY": "ref"}), backend, pid=224)
    assert bundle.ephemeral_files == []


def _full_profile(**overrides):
    """A profile carrying every service, so the parity check below covers them all."""
    base = dict(
        name="every",
        google=GoogleService(email="me@example.com", oauth_client_id="cid",
                             oauth_client_secret_ref="csec-ref",
                             refresh_token_ref="refresh-ref", adc_ref=None,
                             gcloud_config_name="every", default_project="proj"),
        github=GitHubService(username="octo", host="github.com",
                             token_ref="gh-token-ref", ssh_key_path="/keys/id_ed25519"),
        slack=[SlackWorkspace(workspace="team-a", user_token_ref="slack-team-a-ref")],
        aws=AWSService(access_key_id_ref="aws-key-ref", secret_access_key_ref="aws-sec-ref",
                       profile="work", region="us-west-1"),
        oci=OCIService(profile="DEFAULT", config_file="/oci/config"),
        atlassian=AtlassianService(email="me@example.com", api_token_ref="atl-ref",
                                   base_url="https://example.atlassian.net"),
        notion=NotionService(api_token_ref="notion-ref"),
        custom={"ANTHROPIC_API_KEY": "anthropic-ref"},
    )
    base.update(overrides)
    return Profile(**base)


@pytest.fixture
def any_backend():
    """Returns bytes for any ref: these tests are about which keys appear, not
    which secret each carries, so the shared fixture's fixed ref map only gets
    in the way."""
    b = MagicMock()
    b.get.side_effect = lambda ref: f"value-for-{ref}".encode()
    return b


class TestPlanEnvMatchesBuildEnv:
    """`plan_env` mirrors `build_env` without reading a secret, so nothing forces
    the two to agree except this test. It is the honesty of the mirror: a
    variable added to `build_env` and forgotten in `plan_env` makes the
    discovery path quietly lie about what a profile exports."""

    def _keys(self, prof, backend, pid):
        actual = set(build_env(prof, backend, pid=pid).env)
        planned = {p.var for p in plan_env(prof) if p.set}
        return actual, planned

    def test_every_service_agrees(self, monkeypatch, tmp_path, any_backend):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        actual, planned = self._keys(_full_profile(), any_backend, 900)
        assert actual == planned

    def test_a_bare_profile_agrees(self, monkeypatch, tmp_path, any_backend):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        actual, planned = self._keys(Profile(name="bare"), any_backend, 901)
        assert actual == planned

    @pytest.mark.parametrize("overrides,absent", [
        ({"google": GoogleService(email="m@x", oauth_client_id="c",
                                  oauth_client_secret_ref=None, refresh_token_ref=None,
                                  adc_ref=None, gcloud_config_name="g",
                                  default_project=None)},
         {"GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CORE_PROJECT"}),
        ({"github": GitHubService(username="o", host="github.com", token_ref=None,
                                  ssh_key_path="/k")}, {"GH_TOKEN"}),
        ({"aws": AWSService(access_key_id_ref=None, secret_access_key_ref=None,
                            profile="work", region=None)},
         {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"}),
        ({"slack": [SlackWorkspace(workspace="team-a", user_token_ref="slack-team-a-ref"),
                    SlackWorkspace(workspace="team-b", user_token_ref="slack-team-b-ref")]},
         {"MIEN_SLACK_DEFAULT_TOKEN"}),
        ({"oci": OCIService(profile=None, config_file=None)},
         {"OCI_CLI_PROFILE", "OCI_CLI_CONFIG_FILE"}),
    ])
    def test_the_conditional_cases_agree(self, monkeypatch, tmp_path, any_backend,
                                         overrides, absent):
        """The half that matters: a variable a service *can* set but this profile
        does not get. `exec` overlays without scrubbing, so those are the ones an
        ambient value from another identity survives into."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        prof = _full_profile(**overrides)
        actual, planned = self._keys(prof, any_backend, 902)
        assert actual == planned
        assert not (absent & actual), f"build_env unexpectedly set {absent & actual}"
        unset = {p.var for p in plan_env(prof) if not p.set}
        assert absent <= unset, f"plan_env did not report {absent - unset} as unset"
        for var in absent:
            assert next(p.note for p in plan_env(prof) if p.var == var), \
                f"{var} is unset with no reason given"
