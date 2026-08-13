import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from mien.backends.base import SecretNotFound
from mien.cli import main
from mien.secret_naming import BUILTIN_DEFAULT, BUILTIN_SLACK_TOKEN


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mien_cfg(monkeypatch, tmp_path):
    p = tmp_path / "mien.json"
    monkeypatch.setenv("MIEN_CONFIG", str(p))
    return p


def test_list_no_config(runner, mien_cfg):
    result = runner.invoke(main, ["list"])
    assert result.exit_code != 0
    assert "mien init" in result.output


def test_status_when_unset(runner, mien_cfg, monkeypatch):
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "no profile active" in result.output.lower()


def test_status_active(runner, mien_cfg, monkeypatch):
    monkeypatch.setenv("MIEN_PROFILE", "personal")
    result = runner.invoke(main, ["status"])
    assert "personal" in result.output


def test_status_prints_a_value_only_for_the_pinned_non_secret_vars(
        runner, mien_cfg, monkeypatch):
    """`status` masks on an allowlist, so the allowlist is the security boundary.

    Every built-in set to a marker; the marker may appear only for the names
    spelled out here. Adding a secret-bearing name to `NON_SECRET_VARS` — say
    `AWS_SECRET_ACCESS_KEY` — makes `status` print it verbatim and fails here.
    """
    from mien.env import BUILTIN_VARS, MIEN_INTERNAL_OWNER
    for var in BUILTIN_VARS:
        monkeypatch.setenv(var, "MARKER-" + var)
    monkeypatch.setenv("MIEN_PROFILE", "personal")
    out = runner.invoke(main, ["status"]).output
    visible = {ln.split("=", 1)[0].strip() for ln in out.splitlines()
               if "=MARKER-" in ln}
    assert visible == {
        "CLOUDSDK_ACTIVE_CONFIG_NAME", "CLOUDSDK_CORE_PROJECT",
        "GIT_SSH_COMMAND", "MIEN_SLACK_TOKENS", "AWS_PROFILE",
        "AWS_DEFAULT_REGION", "OCI_CLI_PROFILE", "OCI_CLI_CONFIG_FILE",
        "ATLASSIAN_EMAIL", "ATLASSIAN_BASE_URL",
    }
    # GOOGLE_APPLICATION_CREDENTIALS is allowlisted but never reaches this
    # display: `main()` pops it from the environment on every invocation.
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in out
    # Everything else this display reaches is masked.
    masked = {v for v, s in BUILTIN_VARS.items()
              if s != MIEN_INTERNAL_OWNER} - visible - {"GOOGLE_APPLICATION_CREDENTIALS"}
    assert all(f"  {v}=<set>" in out for v in masked)


def test_init_writes_keychain_skeleton(runner, mien_cfg):
    result = runner.invoke(main, ["init"], input="2\nmien-\n")
    assert result.exit_code == 0, result.output
    payload = json.loads(mien_cfg.read_text())
    assert payload["secrets_backend"]["type"] == "macos_keychain"
    assert payload["profiles"] == {}


def test_list_after_init(runner, mien_cfg):
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(main, ["list"])
    assert result.exit_code == 0
    assert "no profiles" in result.output.lower()


def test_whoami_unknown_profile(runner, mien_cfg):
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(main, ["whoami", "nope"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def _rich_profile_cfg(tmp_path, monkeypatch):
    from mien.config import (AtlassianService, AWSService, BackendConfig, Config,
                             GitHubService, GoogleService, NotionService, Profile,
                             SecretNaming, SlackWorkspace, save_config)
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "c.json"))
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"work": Profile(
            name="work",
            google=GoogleService(email="me@acme.example", oauth_client_id="c",
                                 oauth_client_secret_ref=None, refresh_token_ref=None,
                                 adc_ref=None, gcloud_config_name="work",
                                 default_project=None),
            github=GitHubService(username="octocat", host="github.com", token_ref="r"),
            slack=[SlackWorkspace(workspace="team-a", user_token_ref="r")],
            aws=AWSService(profile="acme", region="us-west-1",
                           access_key_id_ref=None, secret_access_key_ref=None),
            atlassian=AtlassianService(email="me@acme.example", api_token_ref="r",
                                       base_url="https://acme.atlassian.net"),
            notion=NotionService(api_token_ref="r"),
            owns_remotes=["github.com/acme-*/*"],
            default_for=["*/Projects/acme*"],
        )},
    ))


def test_whoami_card_shows_the_whole_bundled_identity(runner, tmp_path, monkeypatch):
    _rich_profile_cfg(tmp_path, monkeypatch)
    result = runner.invoke(main, ["whoami", "work"])
    assert result.exit_code == 0
    out = result.output
    assert "one identity, every provider" in out
    for token in ["me@acme.example", "octocat", "team-a", "acme (us-west-1)",
                  "acme.atlassian.net", "notion", "github.com/acme-*/*",
                  "*/Projects/acme*"]:
        assert token in out, token


def test_whoami_card_omits_absent_providers(runner, tmp_path, monkeypatch):
    from mien.config import (BackendConfig, Config, GitHubService, Profile,
                             SecretNaming, save_config)
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "c.json"))
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"solo": Profile(
            name="solo",
            github=GitHubService(username="octocat", host="github.com", token_ref="r"))},
    ))
    result = runner.invoke(main, ["whoami", "solo"])
    assert result.exit_code == 0
    assert "github" in result.output and "octocat" in result.output
    # No google/aws/slack *identity* line for a profile that doesn't have them —
    # they appear only in the `no creds` warning, which is the point of that row.
    identity = [ln for ln in result.output.splitlines()
                if "no creds" not in ln and "stripped" not in ln]
    assert not any("google" in ln or "aws" in ln for ln in identity)


def test_whoami_names_the_variables_of_a_service_the_profile_lacks(
        runner, tmp_path, monkeypatch):
    """The silent case: `exec` overlays without scrubbing, so a service this
    profile has no credential for is fully ambient — an ambient GH_TOKEN acts as
    another identity. The card and the JSON both have to say so."""
    from mien.config import (BackendConfig, Config, NotionService, Profile,
                             SecretNaming, save_config)
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "c.json"))
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT,
                                                 slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"noted": Profile(name="noted",
                                   notion=NotionService(api_token_ref="n"))},
    ))
    card = runner.invoke(main, ["whoami", "noted"])
    assert card.exit_code == 0
    (row,) = [ln for ln in card.output.splitlines() if "no creds" in ln]
    assert "github (GH_TOKEN, GIT_SSH_COMMAND)" in row
    assert "ambient" in row

    out = runner.invoke(main, ["whoami", "noted", "--json"])
    assert out.exit_code == 0
    env = {v["var"]: v for v in json.loads(out.output)["env"]}
    assert env["GH_TOKEN"] == {"var": "GH_TOKEN", "service": "github",
                               "set": False, "configured": False,
                               "note": "this profile configures no github"}
    assert env["NOTION_TOKEN"]["set"] and env["NOTION_TOKEN"]["configured"]


def test_whoami_json_flag_still_emits_machine_readable(runner, tmp_path, monkeypatch):
    _rich_profile_cfg(tmp_path, monkeypatch)
    result = runner.invoke(main, ["whoami", "work", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["name"] == "work" and data["github"] == "octocat"
    assert data["owns_remotes"] == ["github.com/acme-*/*"]


def _project_env(tmp_path, monkeypatch, *profiles):
    from mien.config import (BackendConfig, Config, Profile, SecretNaming,
                             save_config)
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={p: Profile(name=p) for p in profiles},
    ))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr("mien.cli._logical_cwd", lambda: str(ws))
    return ws


def test_which_refuses_an_unapproved_mien(tmp_path, monkeypatch):
    """Security: a checked-out .mien must NOT drive the acting identity until the
    user approves it — `which`/`run` fail loud rather than silently route."""
    ws = _project_env(tmp_path, monkeypatch, "work")
    (ws / ".mien").write_text("work\n")
    result = CliRunner().invoke(main, ["which"])
    assert result.exit_code != 0
    assert "not allowed" in result.output and "mien allow" in result.output


def test_allow_then_which_routes_to_the_declared_profile(tmp_path, monkeypatch):
    ws = _project_env(tmp_path, monkeypatch, "work")
    (ws / ".mien").write_text("work\n")
    assert CliRunner().invoke(main, ["allow"]).exit_code == 0
    result = CliRunner().invoke(main, ["which"])
    assert result.exit_code == 0 and result.output.strip() == "work"


def test_claim_writes_allows_and_routes_in_one_step(tmp_path, monkeypatch):
    ws = _project_env(tmp_path, monkeypatch, "arinyaho")
    assert CliRunner().invoke(main, ["claim", "arinyaho"]).exit_code == 0
    assert (ws / ".mien").read_text().strip() == "arinyaho"
    assert CliRunner().invoke(main, ["which"]).output.strip() == "arinyaho"
    # .mien added to the global git ignore, not the repo.
    assert ".mien" in (tmp_path / "xdg" / "git" / "ignore").read_text()


def test_claim_with_no_arg_picks_a_profile_interactively(tmp_path, monkeypatch):
    ws = _project_env(tmp_path, monkeypatch, "arinyaho", "work")  # sorted: arinyaho, work
    result = CliRunner().invoke(main, ["claim"], input="2\n")  # pick 'work'
    assert result.exit_code == 0, result.output
    assert (ws / ".mien").read_text().strip() == "work"
    assert CliRunner().invoke(main, ["which"]).output.strip() == "work"


def test_claim_with_no_arg_approves_an_existing_declaration(tmp_path, monkeypatch):
    ws = _project_env(tmp_path, monkeypatch, "work")
    (ws / ".mien").write_text("work\n")
    result = CliRunner().invoke(main, ["claim"])  # no prompt — .mien already names it
    assert result.exit_code == 0 and "approving" in result.output
    assert CliRunner().invoke(main, ["which"]).output.strip() == "work"


def test_claim_rejects_an_unknown_profile(tmp_path, monkeypatch):
    _project_env(tmp_path, monkeypatch, "arinyaho")
    result = CliRunner().invoke(main, ["claim", "ghost"])
    assert result.exit_code != 0 and "not found" in result.output


def test_active_profile_overrides_an_unapproved_declaration(tmp_path, monkeypatch):
    ws = _project_env(tmp_path, monkeypatch, "work", "arinyaho")
    (ws / ".mien").write_text("work\n")
    monkeypatch.setenv("MIEN_PROFILE", "arinyaho")
    result = CliRunner().invoke(main, ["which"])
    assert result.exit_code == 0 and "arinyaho" in result.output


def test_changing_the_declaration_reblocks_until_reapproved(tmp_path, monkeypatch):
    ws = _project_env(tmp_path, monkeypatch, "work", "arinyaho")
    (ws / ".mien").write_text("work\n")
    CliRunner().invoke(main, ["allow"])
    (ws / ".mien").write_text("arinyaho\n")  # edited to a different profile
    result = CliRunner().invoke(main, ["which"])
    assert result.exit_code != 0 and "not allowed" in result.output





def _github_profile(runner, mien_cfg, mocker, username="octocat"):
    mocker.patch("mien.cli.load_backend").return_value.put.return_value = "ref://gh"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(main, ["login", "personal", "--service", "github"],
                  input=f"y\n{username}\nghp_x\nn\n")


def test_whoami_offline_still_prints_config_without_probing(runner, mien_cfg, mocker):
    """The default path must not touch the network — it is the fast, offline view."""
    _github_profile(runner, mien_cfg, mocker)
    probe = mocker.patch("mien.cli.probe_github")
    result = runner.invoke(main, ["whoami", "personal"])
    assert result.exit_code == 0
    assert "octocat" in result.output
    probe.assert_not_called()


def test_whoami_live_reports_match(runner, mien_cfg, mocker):
    from mien.verify import ProbeResult, Status
    _github_profile(runner, mien_cfg, mocker)
    mocker.patch("mien.cli.build_env").return_value.env = {"GH_TOKEN": "t"}
    mocker.patch("mien.cli.probe_github",
                 return_value=ProbeResult("github", "octocat", "octocat", Status.MATCH))
    result = runner.invoke(main, ["whoami", "personal", "--live"])
    assert result.exit_code == 0, result.output
    assert "octocat" in result.output
    assert "match" in result.output.lower()


def test_whoami_live_mismatch_exits_nonzero(runner, mien_cfg, mocker):
    """The whole point: a wrong live identity must fail, so it can gate a
    destructive action chained after it."""
    from mien.verify import ProbeResult, Status
    _github_profile(runner, mien_cfg, mocker)
    mocker.patch("mien.cli.build_env").return_value.env = {"GH_TOKEN": "t"}
    mocker.patch("mien.cli.probe_github",
                 return_value=ProbeResult("github", "octocat", "someone-else",
                                          Status.MISMATCH))
    result = runner.invoke(main, ["whoami", "personal", "--live"])
    assert result.exit_code != 0
    assert "someone-else" in result.output
    assert "mismatch" in result.output.lower()


def test_whoami_live_unauthorized_exits_nonzero(runner, mien_cfg, mocker):
    """A revoked token is a problem to surface, distinct from a mismatch."""
    from mien.verify import ProbeResult, Status
    _github_profile(runner, mien_cfg, mocker)
    mocker.patch("mien.cli.build_env").return_value.env = {"GH_TOKEN": "t"}
    mocker.patch("mien.cli.probe_github",
                 return_value=ProbeResult("github", "octocat", None,
                                          Status.UNAUTHORIZED, "HTTP 401"))
    result = runner.invoke(main, ["whoami", "personal", "--live"])
    assert result.exit_code != 0
    assert "unauthorized" in result.output.lower()


def test_whoami_live_cleans_ephemeral_credential_files(runner, tmp_path, monkeypatch, mocker):
    """--live builds the profile env, which writes plaintext credential files to
    $TMPDIR/mien. A verification command must not leave credentials on disk."""
    from mien.config import (BackendConfig, Config, Profile, SecretNaming,
                             SlackWorkspace, GitHubService, save_config)
    from mien.verify import ProbeResult, Status
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"personal": Profile(
            name="personal",
            github=GitHubService(username="octocat", host="github.com", token_ref="ref://gh"),
            slack=[SlackWorkspace(workspace="team-a", user_token_ref="ref://slack")],
        )},
    ))
    mocker.patch("mien.cli.load_backend").return_value.get.return_value = b"xoxp-secret"
    mocker.patch("mien.cli.probe_github",
                 return_value=ProbeResult("github", "octocat", "octocat", Status.MATCH))

    result = runner.invoke(main, ["whoami", "personal", "--live"])
    assert result.exit_code == 0, result.output
    # The slack workspace makes build_env write an ephemeral file; it must be gone.
    assert list((tmp_path / "mien").iterdir()) == []


def test_whoami_live_names_google_when_it_cannot_be_probed(runner, mien_cfg, mocker):
    """A gcloud-login-only google (no client-secret/refresh-token ref) cannot be
    verified by the refresh-token probe. It must be named as unchecked, not
    silently dropped — otherwise a clean report gives false confidence that
    google was verified when it never was."""
    from mien.config import (BackendConfig, Config, Profile, SecretNaming,
                             GoogleService, GitHubService, save_config)
    from mien.verify import ProbeResult, Status
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"personal": Profile(
            name="personal",
            github=GitHubService(username="octocat", host="github.com", token_ref="ref://gh"),
            google=GoogleService(
                email="me@example.com", oauth_client_id="cid",
                oauth_client_secret_ref=None, refresh_token_ref="",
                adc_ref=None, gcloud_config_name="personal",
                default_project=None,
            ),
        )},
    ))
    mocker.patch("mien.cli.build_env").return_value.env = {}
    mocker.patch("mien.cli.probe_github",
                 return_value=ProbeResult("github", "octocat", "octocat", Status.MATCH))
    result = runner.invoke(main, ["whoami", "personal", "--live"])
    assert result.exit_code == 0, result.output
    assert "not checked" in result.output.lower()
    assert "google" in result.output.lower()


def _save_one_profile(prof) -> None:
    from mien.config import (BackendConfig, Config, SecretNaming, save_config)
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT,
                                                slack_token=BUILTIN_SLACK_TOKEN),
        profiles={prof.name: prof},
    ))


@pytest.mark.parametrize("profile_kwargs, id_", [
    # A gcloud-login-only google: google IS configured, and the refresh-token
    # probe still cannot touch it. The message must therefore not claim google is
    # a supported provider, which is how it read before — a contradiction on the
    # one profile that hits this raise.
    (dict(google=dict(email="me@example.com", oauth_client_id="cid",
                      oauth_client_secret_ref=None, refresh_token_ref=None,
                      adc_ref=None, gcloud_config_name="personal",
                      default_project=None)), "gcloud-only-google"),
    # A custom-only profile: mien is told a variable name, never what the
    # credential is for, so no probe exists at all.
    (dict(custom={"NPM_TOKEN": "ref://npm"}), "custom-only"),
])
def test_whoami_live_says_it_could_not_check_when_nothing_is_probeable(
        runner, mien_cfg, mocker, profile_kwargs, id_):
    """No probeable provider fails closed — but as "could not check", not as a
    wrong identity, and naming what a probe actually needs."""
    from mien.config import GoogleService, Profile
    kwargs = dict(profile_kwargs)
    if "google" in kwargs:
        kwargs["google"] = GoogleService(**kwargs["google"])
    _save_one_profile(Profile(name="personal", **kwargs))
    mocker.patch("mien.cli.load_backend")
    mocker.patch("mien.cli.build_env").return_value.env = {}

    result = runner.invoke(main, ["whoami", "personal", "--live"])
    assert result.exit_code != 0
    assert "no provider `--live` can probe" in result.output
    assert "a google with a stored refresh token" in result.output
    assert "mien login --service google" in result.output
    assert "could not check" in result.output
    # And it must not read as a verdict on the identity...
    assert "not a wrong identity" in result.output
    # ...nor print a report that looks like one.
    assert "live identity check" not in result.output


def test_whoami_live_names_unchecked_services(runner, mien_cfg, mocker):
    """A profile with slack/notion must not read as fully verified when only
    github was probed."""
    from mien.config import (BackendConfig, Config, Profile, SecretNaming,
                             SlackWorkspace, GitHubService, NotionService, save_config)
    from mien.verify import ProbeResult, Status
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"personal": Profile(
            name="personal",
            github=GitHubService(username="octocat", host="github.com", token_ref="ref://gh"),
            slack=[SlackWorkspace(workspace="team-a", user_token_ref="ref://s")],
            notion=NotionService(api_token_ref="ref://n"),
        )},
    ))
    mocker.patch("mien.cli.build_env").return_value.env = {}
    mocker.patch("mien.cli.probe_github",
                 return_value=ProbeResult("github", "octocat", "octocat", Status.MATCH))
    result = runner.invoke(main, ["whoami", "personal", "--live"])
    assert result.exit_code == 0, result.output
    assert "not checked" in result.output.lower()
    assert "slack" in result.output and "notion" in result.output


def test_whoami_live_unreachable_does_not_fail(runner, mien_cfg, mocker):
    """Could-not-check is not the same as wrong. A network blip must not report a
    problem that isn't there — it is surfaced, but does not fail the command."""
    from mien.verify import ProbeResult, Status
    _github_profile(runner, mien_cfg, mocker)
    mocker.patch("mien.cli.build_env").return_value.env = {"GH_TOKEN": "t"}
    mocker.patch("mien.cli.probe_github",
                 return_value=ProbeResult("github", "octocat", None,
                                          Status.UNREACHABLE, "timed out"))
    result = runner.invoke(main, ["whoami", "personal", "--live"])
    assert result.exit_code == 0
    assert "unreachable" in result.output.lower()


def test_login_github_stores_token_and_updates_config(runner, mien_cfg, mocker):
    mocker.patch("mien.cli.load_backend").return_value.put.return_value = "ref://gh-token"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main,
        ["login", "personal", "--service", "github"],
        input="y\nmyuser\nghp_token123\nn\n",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(mien_cfg.read_text())
    gh = payload["profiles"]["personal"]["github"]
    assert gh["username"] == "myuser"
    assert gh["host"] == "github.com"
    assert gh["token_ref"] == "ref://gh-token"


def test_login_slack_requires_workspace(runner, mien_cfg):
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main,
        ["login", "personal", "--service", "slack"],
        input="y\n",
    )
    assert result.exit_code != 0
    assert "--workspace" in result.output


def test_login_slack_appends_workspace(runner, mien_cfg, mocker):
    mocker.patch("mien.cli.load_backend").return_value.put.side_effect = [
        "ref://slack-a",
        "ref://slack-b",
    ]
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(main, ["login", "personal", "--service", "slack", "--workspace", "team-a"], input="y\nxoxp-aaa\n")
    runner.invoke(main, ["login", "personal", "--service", "slack", "--workspace", "team-b"], input="xoxp-bbb\n")
    payload = json.loads(mien_cfg.read_text())
    workspaces = payload["profiles"]["personal"]["slack"]
    assert [w["workspace"] for w in workspaces] == ["team-a", "team-b"]
    assert workspaces[0]["user_token_ref"] == "ref://slack-a"


def test_login_google_runs_oauth_and_stores(runner, mien_cfg, mocker):
    mocker.patch("mien.cli.google_installed_app_flow", return_value="refresh-zzz")
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.side_effect = [
        "ref://oauth-secret",
        "ref://refresh",
    ]
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main,
        [
            "login", "personal", "--service", "google",
            "--email", "me@example.com",
            "--client-id", "cid",
        ],
        input="y\ncsec\n",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(mien_cfg.read_text())
    g = payload["profiles"]["personal"]["google"]
    assert g["email"] == "me@example.com"
    assert g["oauth_client_id"] == "cid"
    assert g["refresh_token_ref"] == "ref://refresh"
    assert g["oauth_client_secret_ref"] == "ref://oauth-secret"
    assert g["gcloud_config_name"] == "personal"


def test_use_routes_secrets_through_ephemeral_file(runner, mien_cfg, mocker, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    runner.invoke(main, ["init"], input="2\nmien-\n")
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://gh"
    runner.invoke(main, ["login", "personal", "--service", "github"], input="y\nme\nghp_xxx\nn\n")

    backend.get.return_value = b"ghp_xxx"
    result = runner.invoke(main, ["use", "personal"])
    assert result.exit_code == 0, result.output

    # Token must never appear on stdout: that was the original leak vector
    # (caller forgets `eval`, stdout lands in transcript / shell history / ps).
    assert "ghp_xxx" not in result.output

    # Output is a one-liner that sources a path under TMPDIR/mien and rm's it.
    import re
    m = re.match(r"\. '([^']+)' && rm -f '\1'", result.output.strip())
    assert m, result.output
    script = Path(m.group(1))
    body = script.read_text()
    assert "export MIEN_PROFILE='personal'" in body
    assert "export GH_TOKEN='ghp_xxx'" in body


def _use_setup(runner, mocker, tmp_path, monkeypatch, *, slack=True):
    """A profile that makes build_env write an ephemeral file, with a scoped
    TMPDIR so the files land under tmp_path/mien."""
    from mien.config import (BackendConfig, Config, Profile, SecretNaming,
                             SlackWorkspace, GitHubService, save_config)
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    ws = [SlackWorkspace(workspace="team-a", user_token_ref="ref://s")] if slack else []
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"personal": Profile(
            name="personal",
            github=GitHubService(username="me", host="github.com", token_ref="ref://gh"),
            slack=ws,
        )},
    ))
    mocker.patch("mien.cli.load_backend").return_value.get.return_value = b"xoxp-secret"


def test_use_attributes_files_to_the_owner_pid(runner, tmp_path, monkeypatch, mocker):
    """--owner-pid keys the ephemeral files to the calling shell, not the
    short-lived mien process. The wrapper passes $$ so the files live as long as
    the shell that sourced them — otherwise gc, seeing mien's already-dead pid,
    deletes credentials the shell is still using."""
    _use_setup(runner, mocker, tmp_path, monkeypatch)
    result = runner.invoke(main, ["use", "personal", "--print", "--owner-pid", "999999"])
    assert result.exit_code == 0, result.output
    files = list((tmp_path / "mien").iterdir())
    # Every ephemeral file (slack token map, env loader) is keyed to 999999.
    keyed = [f for f in files if f.name.startswith("999999-")]
    assert keyed, f"no files attributed to the owner pid: {[f.name for f in files]}"


def test_use_leaves_the_files_on_disk_for_the_shell_to_source(runner, tmp_path, monkeypatch, mocker):
    """The activation contract: unlike exec/run, `use` must NOT clean up — the
    calling shell sources these after the process exits. A well-meaning cleanup
    added to use_cmd would break activation silently; this pins against it."""
    _use_setup(runner, mocker, tmp_path, monkeypatch)
    result = runner.invoke(main, ["use", "personal", "--print", "--owner-pid", "999999"])
    assert result.exit_code == 0, result.output
    # The credential files (keyed to the owner pid) must survive — a cleanup
    # added to use_cmd would delete exactly these, which is what breaks
    # activation. Checking the pid-keyed files, not just "any file", is what
    # makes this bite: the env loader has a different name and would survive a
    # pid-scoped cleanup, hiding the break.
    remaining = [f.name for f in (tmp_path / "mien").iterdir()]
    assert any(n.startswith("999999-") for n in remaining), \
        f"use must leave its owner-pid credential files on disk; found {remaining}"


def test_use_refuses_when_stdout_is_a_tty(runner, mien_cfg, mocker, monkeypatch):
    runner.invoke(main, ["init"], input="2\nmien-\n")
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://gh"
    runner.invoke(main, ["login", "personal", "--service", "github"], input="y\nme\nghp_xxx\nn\n")

    # Force the TTY heuristic on Click's StringIO so we can test the guard.
    mocker.patch("mien.cli._stdout_is_tty", return_value=True)
    result = runner.invoke(main, ["use", "personal"])

    # Must exit non-zero with a hint at the wrapper / eval form. Crucially,
    # the secret must NOT have been resolved or printed.
    assert result.exit_code != 0
    assert "ghp_xxx" not in result.output
    assert "eval" in result.output or "mien-use" in result.output


def test_use_print_flag_overrides_tty_guard(runner, mien_cfg, mocker, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    runner.invoke(main, ["init"], input="2\nmien-\n")
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://gh"
    runner.invoke(main, ["login", "personal", "--service", "github"], input="y\nme\nghp_xxx\nn\n")

    backend.get.return_value = b"ghp_xxx"
    mocker.patch("mien.cli._stdout_is_tty", return_value=True)
    result = runner.invoke(main, ["use", "personal", "--print"])
    assert result.exit_code == 0, result.output
    # Even with --print, stdout carries the loader (not the raw token).
    assert "ghp_xxx" not in result.output


def test_exec_removes_ephemeral_files_after_child_exits(
    runner, mien_cfg, mocker, tmp_path, monkeypatch
):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    runner.invoke(main, ["init"], input="2\nmien-\n")
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://slack"
    # A slack workspace guarantees build_env writes an ephemeral token file,
    # so the assertions below can't pass vacuously.
    runner.invoke(
        main,
        ["login", "demo", "--service", "slack", "--workspace", "acme"],
        input="y\nxoxp-secret\n",
    )

    backend.get.return_value = b"xoxp-secret"
    seen: dict = {}

    def fake_call(argv, env=None):
        path = Path(env["MIEN_SLACK_TOKENS"])
        seen["path"] = path
        seen["existed"] = path.exists()
        seen["body"] = path.read_text() if seen["existed"] else ""
        return 0

    mocker.patch("mien.cli.subprocess.call", side_effect=fake_call)
    result = runner.invoke(main, ["exec", "demo", "--", "true"])
    assert result.exit_code == 0, result.output

    # The child really did get a plaintext credential file...
    assert seen["existed"]
    assert "xoxp-secret" in seen["body"]
    # ...and nothing survives the exec.
    assert not seen["path"].exists()
    assert list((tmp_path / "mien").iterdir()) == []


def test_exec_removes_ephemeral_files_when_child_fails(
    runner, mien_cfg, mocker, tmp_path, monkeypatch
):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    runner.invoke(main, ["init"], input="2\nmien-\n")
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://slack"
    runner.invoke(
        main,
        ["login", "demo", "--service", "slack", "--workspace", "acme"],
        input="y\nxoxp-secret\n",
    )

    backend.get.return_value = b"xoxp-secret"
    written: list[Path] = []

    def boom(argv, env=None):
        written.append(Path(env["MIEN_SLACK_TOKENS"]))
        raise KeyboardInterrupt

    mocker.patch("mien.cli.subprocess.call", side_effect=boom)
    result = runner.invoke(main, ["exec", "demo", "--", "true"])
    assert result.exit_code != 0
    assert written and not written[0].exists()
    assert list((tmp_path / "mien").iterdir()) == []


def test_unset_emits_unsets(runner, mien_cfg):
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(main, ["unset"])
    assert "unset MIEN_PROFILE" in result.output


def test_token_google_prints_access_token(runner, mien_cfg, mocker):
    runner.invoke(main, ["init"], input="2\nmien-\n")
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.side_effect = ["ref://oauth", "ref://refresh"]
    backend.get.side_effect = lambda r: {
        "ref://oauth": b"csec",
        "ref://refresh": b"refresh-zzz",
    }[r]
    mocker.patch("mien.cli.google_installed_app_flow", return_value="refresh-zzz")
    mocker.patch("mien.cli.exchange_refresh_token", return_value="ya29-access")

    runner.invoke(
        main,
        ["login", "personal", "--service", "google",
         "--email", "me@x.com", "--client-id", "cid"],
        input="y\ncsec\n",
    )

    result = runner.invoke(main, ["token", "google"], env={"MIEN_PROFILE": "personal", "MIEN_CONFIG": str(mien_cfg)})
    assert result.exit_code == 0
    assert "ya29-access" in result.output


def test_token_google_accepts_explicit_profile_without_env(runner, mien_cfg, mocker, monkeypatch):
    """`mien token` must work without an ambient MIEN_PROFILE.

    AI agent harnesses (Claude Code, Codex) start a fresh shell per tool call, so
    env vars set by a previous `eval "$(mien use ...)"` are gone by the next call.
    Without an explicit --profile the agent has no reliable way to mint a token.
    """
    # CliRunner's env= overlays os.environ rather than replacing it, so an
    # exported MIEN_PROFILE on the developer's machine would otherwise mask
    # whether --profile did any work at all.
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    runner.invoke(main, ["init"], input="2\nmien-\n")
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.side_effect = ["ref://oauth", "ref://refresh"]
    backend.get.side_effect = lambda r: {
        "ref://oauth": b"csec",
        "ref://refresh": b"refresh-zzz",
    }[r]
    mocker.patch("mien.cli.google_installed_app_flow", return_value="refresh-zzz")
    mocker.patch("mien.cli.exchange_refresh_token", return_value="ya29-access")

    runner.invoke(
        main,
        ["login", "personal", "--service", "google",
         "--email", "me@x.com", "--client-id", "cid"],
        input="y\ncsec\n",
    )

    result = runner.invoke(
        main,
        ["token", "google", "--profile", "personal"],
        env={"MIEN_CONFIG": str(mien_cfg)},
    )
    assert result.exit_code == 0
    assert "ya29-access" in result.output


def test_token_google_explicit_profile_beats_env(runner, mien_cfg, mocker):
    """--profile must win over a conflicting ambient MIEN_PROFILE.

    The env var is only a fallback (`profile or $MIEN_PROFILE`). MIEN_PROFILE is
    pinned here to a profile that does not exist, so if --profile were ignored the
    command would fail with "not found" instead of minting the token.
    """
    runner.invoke(main, ["init"], input="2\nmien-\n")
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.side_effect = ["ref://oauth", "ref://refresh"]
    backend.get.side_effect = lambda r: {
        "ref://oauth": b"csec",
        "ref://refresh": b"refresh-zzz",
    }[r]
    mocker.patch("mien.cli.google_installed_app_flow", return_value="refresh-zzz")
    mocker.patch("mien.cli.exchange_refresh_token", return_value="ya29-access")

    runner.invoke(
        main,
        ["login", "personal", "--service", "google",
         "--email", "me@x.com", "--client-id", "cid"],
        input="y\ncsec\n",
    )

    result = runner.invoke(
        main,
        ["token", "google", "--profile", "personal"],
        env={"MIEN_PROFILE": "someone-else", "MIEN_CONFIG": str(mien_cfg)},
    )
    assert result.exit_code == 0, result.output
    assert "ya29-access" in result.output
    assert "someone-else" not in result.output


def test_token_google_without_stored_oauth_fails_cleanly(runner, mien_cfg, mocker):
    """A gcloud-login-only google has no refresh token to exchange. That must be
    a ClickException, not a crash — the keychain backend aborts the process when
    handed a None reference."""
    from mien.config import GoogleService, Profile
    _save_one_profile(Profile(name="personal", google=GoogleService(
        email="me@example.com", oauth_client_id="cid",
        oauth_client_secret_ref=None, refresh_token_ref=None,
        adc_ref=None, gcloud_config_name="personal", default_project=None,
    )))
    backend = mocker.patch("mien.cli.load_backend").return_value

    result = runner.invoke(
        main,
        ["token", "google", "--profile", "personal", "--force"],
        env={"MIEN_CONFIG": str(mien_cfg)},
    )
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "no stored google OAuth credentials" in result.output
    assert "mien exec personal" in result.output
    assert "mien login personal --service google" in result.output
    backend.get.assert_not_called()


def test_token_without_profile_or_env_names_both_remedies(runner, mien_cfg, mocker, monkeypatch):
    """The error must name both remedies: the --profile flag and the eval pattern."""
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(main, ["token", "google"], env={"MIEN_CONFIG": str(mien_cfg)})
    assert result.exit_code != 0
    assert "--profile" in result.output
    assert "MIEN_PROFILE" in result.output
    assert 'eval "$(mien use' in result.output


def test_init_non_interactive_keychain(runner, mien_cfg, mocker):
    mocker.patch("mien.cli.load_backend").return_value.health_check.return_value = None
    result = runner.invoke(
        main,
        ["init", "--backend", "macos_keychain", "--service-prefix", "mien-"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(mien_cfg.read_text())
    assert payload["secrets_backend"]["type"] == "macos_keychain"


def test_init_non_interactive_gcp(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    mocker.patch("mien.cli.subprocess.run")
    result = runner.invoke(
        main,
        ["init",
         "--backend", "gcp_secret_manager",
         "--project", "my-proj-1",
         "--bootstrap-email", "me@x.com"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(mien_cfg.read_text())
    assert payload["secrets_backend"]["project"] == "my-proj-1"
    assert payload["bootstrap"]["gcp_account"] == "me@x.com"


def test_init_yes_overwrites_existing(runner, mien_cfg, mocker):
    mien_cfg.parent.mkdir(parents=True, exist_ok=True)
    mien_cfg.write_text("{}")
    mocker.patch("mien.cli.load_backend").return_value.health_check.return_value = None
    result = runner.invoke(
        main,
        ["init", "-y", "--backend", "macos_keychain", "--service-prefix", "mien-"],
    )
    assert result.exit_code == 0, result.output


def test_login_github_token_stdin(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://gh"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main,
        ["login", "personal", "--service", "github", "--username", "me", "--token-stdin"],
        input="y\nghp_pasted_token\n",
    )
    assert result.exit_code == 0, result.output
    args = backend.put.call_args[0]
    assert args[1] == b"ghp_pasted_token"


def test_login_github_ssh_key_path_skips_pat_prompt(runner, mien_cfg, mocker, tmp_path):
    mocker.patch("mien.cli.load_backend")
    runner.invoke(main, ["init"], input="2\nmien-\n")
    keyfile = tmp_path / "id_test"
    keyfile.write_text("PRIVATE")
    result = runner.invoke(
        main,
        ["login", "personal", "--service", "github", "--ssh-key-path", str(keyfile)],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(mien_cfg.read_text())
    gh = payload["profiles"]["personal"]["github"]
    assert gh["ssh_key_path"] == str(keyfile)
    assert gh["token_ref"] is None
    assert gh["ssh_key_ref"] is None


def test_login_github_ssh_key_stored_in_backend(runner, mien_cfg, mocker, tmp_path):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://ssh"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    keyfile = tmp_path / "id_test"
    keyfile.write_bytes(b"PRIVATE-KEY")
    result = runner.invoke(
        main,
        ["login", "personal", "--service", "github", "--ssh-key", str(keyfile)],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    backend.put.assert_called_once()
    args = backend.put.call_args[0]
    assert args[1] == b"PRIVATE-KEY"
    payload = json.loads(mien_cfg.read_text())
    gh = payload["profiles"]["personal"]["github"]
    assert gh["ssh_key_ref"] == "ref://ssh"
    assert gh["token_ref"] is None


def test_login_github_interactive_ssh_prompt_path(runner, mien_cfg, mocker, tmp_path):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://gh"
    keyfile = tmp_path / "id_test"
    keyfile.write_text("PRIVATE")
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main,
        ["login", "personal", "--service", "github"],
        input=f"y\nme\ntok\ny\n{keyfile}\npath\n",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(mien_cfg.read_text())
    gh = payload["profiles"]["personal"]["github"]
    assert gh["token_ref"] == "ref://gh"
    assert gh["ssh_key_path"] == str(keyfile)
    assert gh["ssh_key_ref"] is None


def test_login_github_pat_and_ssh_compose_across_calls(runner, mien_cfg, mocker, tmp_path):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://gh"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(main, ["login", "work2", "--service", "github"], input="y\nme\ntok\nn\n")
    keyfile = tmp_path / "id_test"
    keyfile.write_text("PRIVATE")
    runner.invoke(
        main,
        ["login", "work2", "--service", "github", "--ssh-key-path", str(keyfile)],
    )
    payload = json.loads(mien_cfg.read_text())
    gh = payload["profiles"]["work2"]["github"]
    assert gh["token_ref"] == "ref://gh"
    assert gh["ssh_key_path"] == str(keyfile)


def test_preflight_keychain_passes(runner, mocker):
    # Preflight checks the real backend path (in-process Keychain), not the
    # security CLI — so a reachable Keychain is a healthy backend.
    mocker.patch("mien.backends.keychain._MacKeyring").return_value.get_password.return_value = None
    result = runner.invoke(main, ["preflight", "--backend", "macos_keychain", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["backend"] == "macos_keychain"
    assert all(c["ok"] for c in payload["checks"])
    assert any("keychain" in c["check"].lower() for c in payload["checks"])


def test_preflight_gcp_reports_missing_pieces(runner, mien_cfg, mocker):
    # gcloud --version succeeds, project describe fails, services list returns empty,
    # ADC file missing → preflight should exit non-zero with structured findings.
    import subprocess as _sp
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["gcloud", "--version"]:
            return _sp.CompletedProcess(cmd, 0, stdout="Google Cloud SDK 999.0.0\n", stderr="")
        if cmd[:3] == ["gcloud", "projects", "describe"]:
            return _sp.CompletedProcess(cmd, 1, stdout="", stderr="permission denied")
        if cmd[:3] == ["gcloud", "services", "list"]:
            return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")
        return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")
    mocker.patch("mien.cli.subprocess.run", side_effect=fake_run)
    mocker.patch("mien.cli.Path.exists", return_value=False)  # ADC missing
    result = runner.invoke(
        main,
        ["preflight", "--backend", "gcp_secret_manager",
         "--project", "missing-proj", "--account", "me@x.com", "--json"],
    )
    assert result.exit_code != 0
    payload = json.loads(result.output)
    checks = {c["check"]: c for c in payload["checks"]}
    assert checks["gcloud installed"]["ok"]
    assert not checks["project 'missing-proj' accessible"]["ok"]
    assert not checks["Secret Manager API enabled"]["ok"]


def test_logout_removes_service(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://gh"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(main, ["login", "personal", "--service", "github"], input="y\nme\ntok\nn\n")
    result = runner.invoke(main, ["logout", "personal", "--service", "github"])
    assert result.exit_code == 0
    backend.delete.assert_called_with("ref://gh")
    payload = json.loads(mien_cfg.read_text())
    assert payload["profiles"]["personal"]["github"] is None


def test_doctor_runs_health_check(runner, mien_cfg, mocker):
    runner.invoke(main, ["init"], input="2\nmien-\n")
    backend = mocker.patch("mien.cli.load_backend").return_value
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0
    backend.health_check.assert_called_once()
    assert "OK" in result.output


def test_doctor_reports_failure(runner, mien_cfg, mocker):
    runner.invoke(main, ["init"], input="2\nmien-\n")
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.side_effect = RuntimeError("boom")
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code != 0
    assert "boom" in result.output


def test_doctor_gc_sweeps(runner, mien_cfg, mocker):
    runner.invoke(main, ["init"], input="2\nmien-\n")
    mocker.patch("mien.cli.load_backend").return_value
    gc = mocker.patch("mien.cli.EphemeralStore.gc")
    runner.invoke(main, ["doctor", "--gc"])
    gc.assert_called_once()


TOKEN = "ghp_" + "0" * 36


def _repo(tmp_path, monkeypatch, remotes):
    """A throwaway git repo with ``remotes`` ({name: url}), isolated from real config."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "gitconfig-system"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    for name, url in remotes.items():
        subprocess.run(["git", "-C", str(repo), "remote", "add", name, url], check=True)
    return repo


def _doctor(runner, mien_cfg, mocker, repo):
    runner.invoke(main, ["init"], input="2\nmien-\n")
    mocker.patch("mien.cli.load_backend")
    mocker.patch("mien.cli._logical_cwd", return_value=str(repo))
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert TOKEN not in result.output  # the URL is the secret; never print it.
    return result.output


def test_doctor_names_only_the_credentialed_remote(
    runner, mien_cfg, mocker, monkeypatch, tmp_path
):
    repo = _repo(tmp_path, monkeypatch, {
        "tidy": "https://github.com/example/tidy.git",
        "leaky": f"https://x-access-token:{TOKEN}@github.com/example/leaky.git",
    })
    out = _doctor(runner, mien_cfg, mocker, repo)
    assert "leaky (fetch" in out
    assert "tidy" not in out
    # Nothing printed may be a runnable command built from this repo's own
    # remotes: a generated command cannot be right for a rewrite rule it cannot
    # see, and a wrong one destroys config. Commands stay placeholder prose.
    for line in out.splitlines():
        if "git " not in line:
            continue
        # No actual remote name or URL interpolated into a command line...
        assert "leaky" not in line and "github.com" not in line, line
        # ...and nothing that rewrites config on its own.
        assert not any(s in line for s in ("$(", "sed ", "--unset")), line
    # No suggested command may print the credential: a rewrite rule's config key
    # IS the credentialed URL, so any listing of it prints the token.
    assert "--list" not in out
    assert "--get-regexp" not in out or "do not list it with" in out


def test_doctor_finds_credential_only_on_the_push_side(
    runner, mien_cfg, mocker, monkeypatch, tmp_path
):
    # Fetch URL tidy, push URL dirty: plain `git remote get-url` shows nothing.
    repo = _repo(tmp_path, monkeypatch, {"origin": "https://github.com/example/r.git"})
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "--push", "origin",
                    f"https://x-access-token:{TOKEN}@github.com/example/r.git"], check=True)
    out = _doctor(runner, mien_cfg, mocker, repo)
    assert "origin (push)" in out


def test_doctor_finds_credential_in_an_extra_remote_url(
    runner, mien_cfg, mocker, monkeypatch, tmp_path
):
    # A second `remote.<n>.url` acts as a push URL; `get-url` without --all hides it.
    repo = _repo(tmp_path, monkeypatch, {"origin": "https://github.com/example/r.git"})
    subprocess.run(["git", "-C", str(repo), "config", "--add", "remote.origin.url",
                    f"https://x-access-token:{TOKEN}@example.com/mirror.git"], check=True)
    out = _doctor(runner, mien_cfg, mocker, repo)
    assert "origin (fetch, push)" in out


def test_doctor_finds_credential_injected_by_insteadof(
    runner, mien_cfg, mocker, monkeypatch, tmp_path
):
    # The remote URL is tidy; a global rewrite rule puts the token in front of it.
    repo = _repo(tmp_path, monkeypatch, {"origin": "https://github.com/example/r.git"})
    subprocess.run(["git", "-C", str(repo), "config", "--global",
                    f"url.https://x-access-token:{TOKEN}@github.com/.insteadOf",
                    "https://github.com/"], check=True)
    out = _doctor(runner, mien_cfg, mocker, repo)
    assert "origin (fetch, push)" in out
    # The remedy must point at the rewrite rule, which is not on the remote — and
    # send the user to the editor, since the rule's config key IS the token.
    assert "git config --global --edit" in out
    assert "do not list it with" in out


def test_doctor_finds_credential_injected_by_pushinsteadof(
    runner, mien_cfg, mocker, monkeypatch, tmp_path
):
    # `pushInsteadOf` rewrites only the push side, and only for `get-url --push`:
    # the raw config keys carry no token at all.
    repo = _repo(tmp_path, monkeypatch, {"origin": "https://github.com/example/r.git"})
    subprocess.run(["git", "-C", str(repo), "config", "--global",
                    f"url.https://x-access-token:{TOKEN}@github.com/.pushInsteadOf",
                    "https://github.com/"], check=True)
    out = _doctor(runner, mien_cfg, mocker, repo)
    assert "origin (push)" in out
    assert "git config --global --edit" in out


def test_doctor_silent_on_clean_remotes(runner, mien_cfg, mocker, monkeypatch, tmp_path):
    repo = _repo(tmp_path, monkeypatch, {"origin": "https://github.com/example/clean.git"})
    runner.invoke(main, ["init"], input="2\nmien-\n")
    mocker.patch("mien.cli.load_backend")
    mocker.patch("mien.cli._logical_cwd", return_value=str(repo))
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "remotes:" not in result.output


def test_doctor_outside_a_repository_does_not_error(
    runner, mien_cfg, mocker, monkeypatch, tmp_path
):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    plain = tmp_path / "plain"
    plain.mkdir()
    runner.invoke(main, ["init"], input="2\nmien-\n")
    mocker.patch("mien.cli.load_backend")
    mocker.patch("mien.cli._logical_cwd", return_value=str(plain))
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "remotes:" not in result.output


def test_init_rejects_project_name_with_space(runner, mien_cfg):
    result = runner.invoke(main, ["init"], input="1\nMy First Project\n")
    assert result.exit_code != 0
    assert "PROJECT_ID" in result.output
    assert "gcloud projects list" in result.output
    assert not mien_cfg.exists()


def test_init_rejects_uppercase_project_id(runner, mien_cfg):
    result = runner.invoke(main, ["init"], input="1\nMy-Project\n")
    assert result.exit_code != 0
    assert "PROJECT_ID" in result.output


def test_init_strips_markdown_email(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    mocker.patch("mien.cli.subprocess.run")  # don't touch real gcloud ADC
    result = runner.invoke(
        main,
        ["init"],
        input="1\nmy-proj-1\n[a@b.com](mailto:a@b.com)\n",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(mien_cfg.read_text())
    assert payload["bootstrap"]["gcp_account"] == "a@b.com"


def test_init_aborts_on_health_check_failure_with_actionable_message(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.side_effect = RuntimeError("permission denied: caller lacks role")
    result = runner.invoke(main, ["init"], input="1\nmy-proj-1\nme@x.com\n")
    assert result.exit_code != 0
    out = result.output
    assert "permission denied" in out.lower()
    assert "gcloud auth application-default login --account=me@x.com" in out
    assert "mien doctor" in out
    # Config should still be on disk so user can fix without re-prompting.
    assert mien_cfg.exists()


def test_init_keychain_runs_health_check(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    result = runner.invoke(main, ["init"], input="2\nmien-\n")
    assert result.exit_code == 0
    backend.health_check.assert_called_once()
    assert "OK" in result.output


def test_init_sets_quota_project_for_gcp(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    sub = mocker.patch("mien.cli.subprocess.run")
    result = runner.invoke(main, ["init"], input="1\nside-proj\nme@x.com\n")
    assert result.exit_code == 0, result.output
    quota_calls = [
        c for c in sub.call_args_list
        if c[0][0][:4] == ["gcloud", "auth", "application-default", "set-quota-project"]
    ]
    assert len(quota_calls) == 1
    assert quota_calls[0][0][0][4] == "side-proj"
    assert "quota project to side-proj" in result.output


def test_init_warns_when_quota_project_set_fails(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    mocker.patch("mien.cli.subprocess.run", side_effect=FileNotFoundError("gcloud"))
    result = runner.invoke(main, ["init"], input="1\nside-proj\nme@x.com\n")
    assert result.exit_code == 0
    assert "could not set ADC quota project" in result.output
    assert "gcloud auth application-default set-quota-project side-proj" in result.output


def test_login_google_shows_oauth_hint_when_client_id_missing(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    backend.put.side_effect = ["ref://oauth", "ref://refresh"]
    mocker.patch("mien.cli.google_installed_app_flow", return_value="refresh-zzz")
    mocker.patch("mien.cli.subprocess.run")  # set-quota-project no-op
    runner.invoke(main, ["init"], input="1\nside-proj\nme@x.com\n")
    result = runner.invoke(
        main,
        ["login", "personal", "--service", "google", "--email", "me@x.com"],
        input="y\nmy-cid\nmy-csec\n",
    )
    assert result.exit_code == 0, result.output
    assert "OAuth Desktop client" in result.output
    assert "console.cloud.google.com/apis/credentials" in result.output
    assert "side-proj" in result.output


def test_login_google_skips_hint_when_client_id_provided(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    backend.put.side_effect = ["ref://oauth", "ref://refresh"]
    mocker.patch("mien.cli.google_installed_app_flow", return_value="refresh-zzz")
    mocker.patch("mien.cli.subprocess.run")
    runner.invoke(main, ["init"], input="1\nside-proj\nme@x.com\n")
    result = runner.invoke(
        main,
        ["login", "personal", "--service", "google",
         "--email", "me@x.com", "--client-id", "cid"],
        input="y\ncsec\n",
    )
    assert result.exit_code == 0, result.output
    assert "OAuth Desktop client" not in result.output


def test_login_github_pushes_manifest(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://gh-token"
    mocker.patch("mien.cli.pull_manifest", return_value=None)
    push = mocker.patch("mien.cli.push_manifest")
    runner.invoke(
        main,
        ["init", "--backend", "gcp_secret_manager",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
    )
    result = runner.invoke(
        main,
        ["login", "personal", "--service", "github"],
        input="y\nmyuser\nghp_token123\nn\n",
    )
    assert result.exit_code == 0, result.output
    push.assert_called_once()


def test_login_manifest_push_failure_is_nonfatal(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://gh-token"
    mocker.patch("mien.cli.pull_manifest", return_value=None)
    mocker.patch("mien.cli.push_manifest", side_effect=RuntimeError("network down"))
    runner.invoke(
        main,
        ["init", "--backend", "gcp_secret_manager",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
    )
    result = runner.invoke(
        main,
        ["login", "personal", "--service", "github"],
        input="y\nmyuser\nghp_token123\nn\n",
    )
    assert result.exit_code == 0, result.output
    assert "could not sync config manifest" in result.output
    assert "network down" in result.output


def test_login_keychain_does_not_push_manifest(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://gh-token"
    push = mocker.patch("mien.cli.push_manifest")
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main,
        ["login", "personal", "--service", "github"],
        input="y\nmyuser\nghp_token123\nn\n",
    )
    assert result.exit_code == 0, result.output
    push.assert_not_called()


def test_logout_pushes_manifest(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://gh"
    mocker.patch("mien.cli.pull_manifest", return_value=None)
    push = mocker.patch("mien.cli.push_manifest")
    runner.invoke(
        main,
        ["init", "--backend", "gcp_secret_manager",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
    )
    runner.invoke(main, ["login", "personal", "--service", "github"],
                  input="y\nme\ntok\nn\n")
    push.reset_mock()
    result = runner.invoke(main, ["logout", "personal", "--service", "github"])
    assert result.exit_code == 0, result.output
    push.assert_called_once()


def test_login_rejects_reserved_manifest_profile_name(runner, mien_cfg, mocker):
    mocker.patch("mien.cli.load_backend")
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main,
        ["login", "mien-config-manifest", "--service", "github", "--username", "u"],
        input="y\n",
    )
    assert result.exit_code != 0
    assert "reserved" in result.output.lower()


def _manifest_cfg():
    from mien.config import (BackendConfig, Config, GitHubService, Profile,
                            SecretNaming)
    return Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="gcp_secret_manager", options={"project": "p1"}),
        bootstrap={"gcp_account": "me@x.com"},
        secret_naming=SecretNaming(default=BUILTIN_DEFAULT,
                                   slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"work": Profile(name="work",
                                  github=GitHubService(username="u", host="github.com",
                                                       token_ref="ref://x"))},
    )


def test_init_offers_and_imports_manifest(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    mocker.patch("mien.cli.subprocess.run")
    mocker.patch("mien.cli.pull_manifest", return_value=_manifest_cfg())
    result = runner.invoke(
        main,
        ["init", "--backend", "gcp_secret_manager",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "Imported 1 profiles" in result.output
    payload = json.loads(mien_cfg.read_text())
    assert "work" in payload["profiles"]


def test_init_no_import_flag_skips_manifest(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    mocker.patch("mien.cli.subprocess.run")
    mocker.patch("mien.cli.pull_manifest", return_value=_manifest_cfg())
    result = runner.invoke(
        main,
        ["init", "--backend", "gcp_secret_manager", "--no-import",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(mien_cfg.read_text())
    assert payload["profiles"] == {}


def test_init_keychain_never_pulls_manifest(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    pull = mocker.patch("mien.cli.pull_manifest")
    result = runner.invoke(main, ["init"], input="2\nmien-\n")
    assert result.exit_code == 0, result.output
    pull.assert_not_called()


def test_init_user_declines_manifest_import(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    mocker.patch("mien.cli.subprocess.run")
    mocker.patch("mien.cli.pull_manifest", return_value=_manifest_cfg())
    result = runner.invoke(
        main,
        ["init", "--backend", "gcp_secret_manager",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    assert "Next:" in result.output
    payload = json.loads(mien_cfg.read_text())
    assert payload["profiles"] == {}


def test_sync_dry_run_reports_diff_and_writes_nothing(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    mocker.patch("mien.cli.subprocess.run")
    mocker.patch("mien.cli.pull_manifest", return_value=None)
    runner.invoke(
        main,
        ["init", "--backend", "gcp_secret_manager", "--no-import",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
    )
    before = mien_cfg.read_text()
    mocker.patch("mien.cli.pull_manifest", return_value=_manifest_cfg())
    result = runner.invoke(main, ["sync", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "+ add:" in result.output and "work" in result.output
    assert mien_cfg.read_text() == before  # unchanged


def test_sync_applies_with_yes(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    mocker.patch("mien.cli.subprocess.run")
    mocker.patch("mien.cli.pull_manifest", return_value=None)
    runner.invoke(
        main,
        ["init", "--backend", "gcp_secret_manager", "--no-import",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
    )
    mocker.patch("mien.cli.pull_manifest", return_value=_manifest_cfg())
    result = runner.invoke(main, ["sync", "-y"])
    assert result.exit_code == 0, result.output
    payload = json.loads(mien_cfg.read_text())
    assert "work" in payload["profiles"]


def test_sync_no_manifest_errors(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    mocker.patch("mien.cli.subprocess.run")
    mocker.patch("mien.cli.pull_manifest", return_value=None)
    runner.invoke(
        main,
        ["init", "--backend", "gcp_secret_manager", "--no-import",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
    )
    result = runner.invoke(main, ["sync"])
    assert result.exit_code != 0
    assert "no manifest" in result.output.lower()


def test_sync_unparseable_manifest_blames_the_manifest_not_local_config(
    runner, mien_cfg, mocker
):
    """A manifest written by another mien must not read as "your config is broken"."""
    from mien.manifest import MANIFEST_SECRET_NAME
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    mocker.patch("mien.cli.subprocess.run")
    runner.invoke(
        main,
        ["init", "--backend", "gcp_secret_manager", "--no-import",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
    )
    before = mien_cfg.read_text()
    # real pull_manifest, fed a manifest this build cannot parse
    backend.list.return_value = [f"ref://{MANIFEST_SECRET_NAME}/versions/latest"]
    backend.get.return_value = (
        b'{"$schema_version": 99, "secrets_backend": {"type": "macos_keychain"}, '
        b'"bootstrap": {}, "secret_naming": {}, "profiles": {}}'
    )
    result = runner.invoke(main, ["sync"])
    assert result.exit_code != 0
    assert MANIFEST_SECRET_NAME in result.output
    assert str(mien_cfg) not in result.output  # never points at the local config
    assert "stops enforcing" not in result.output  # guard is fine — local config parses
    assert "mien push" in result.output  # the repair that actually applies
    assert mien_cfg.read_text() == before  # local config untouched


def test_unparseable_local_config_still_blames_the_local_config(runner, mien_cfg):
    mien_cfg.write_text(
        '{"$schema_version": 1, "secrets_backend": {"type": "macos_keychain"}, '
        '"bootstrap": {}, "secret_naming": {}, "profiles": {"work": {"defualt_for": []}}}'
    )
    result = runner.invoke(main, ["list"])
    assert result.exit_code != 0
    assert str(mien_cfg) in result.output
    assert "status line shows a warning" in result.output
    assert "stops enforcing" in result.output
    assert "mien-config-manifest" not in result.output


def test_sync_requires_cloud_backend(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(main, ["sync"])
    assert result.exit_code != 0
    assert "cloud backend" in result.output.lower()


def test_sync_already_in_sync(runner, mien_cfg, mocker):
    import mien.config as miencfg
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    mocker.patch("mien.cli.subprocess.run")
    mocker.patch("mien.cli.pull_manifest", return_value=None)
    runner.invoke(
        main,
        ["init", "--backend", "gcp_secret_manager", "--no-import",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
    )
    # make local config identical to the manifest we'll pull
    mien_cfg.write_text(miencfg.serialize_config(_manifest_cfg()))
    mocker.patch("mien.cli.pull_manifest", return_value=_manifest_cfg())
    result = runner.invoke(main, ["sync"])
    assert result.exit_code == 0, result.output
    assert "already in sync" in result.output


def test_sync_user_declines_confirm_aborts(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    mocker.patch("mien.cli.subprocess.run")
    mocker.patch("mien.cli.pull_manifest", return_value=None)
    runner.invoke(
        main,
        ["init", "--backend", "gcp_secret_manager", "--no-import",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
    )
    before = mien_cfg.read_text()
    mocker.patch("mien.cli.pull_manifest", return_value=_manifest_cfg())
    result = runner.invoke(main, ["sync"], input="n\n")
    assert result.exit_code != 0
    assert mien_cfg.read_text() == before  # nothing written


def test_push_command_pushes_manifest(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    mocker.patch("mien.cli.subprocess.run")
    mocker.patch("mien.cli.pull_manifest", return_value=None)
    runner.invoke(
        main,
        ["init", "--backend", "gcp_secret_manager", "--no-import",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
    )
    push = mocker.patch("mien.cli.push_manifest")
    result = runner.invoke(main, ["push"])
    assert result.exit_code == 0, result.output
    push.assert_called_once()
    assert "pushed config manifest" in result.output


def test_push_command_noop_on_keychain(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    runner.invoke(main, ["init"], input="2\nmien-\n")
    push = mocker.patch("mien.cli.push_manifest")
    result = runner.invoke(main, ["push"])
    assert result.exit_code == 0, result.output
    push.assert_not_called()
    assert "no-op" in result.output.lower()


def _config_with_retired_backend() -> None:
    """A config as an older mien left it: naming a backend that no longer exists."""
    from mien.config import (BackendConfig, Config, GitHubService, Profile,
                             SecretNaming, save_config)
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="oci_vault", options={"vault_id": "v1"}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"work": Profile(
            name="work",
            github=GitHubService(username="octocat", host="github.com", token_ref="r"))},
    ))


def test_retired_backend_type_is_actionable_not_a_traceback(runner, mien_cfg):
    _config_with_retired_backend()
    result = runner.invoke(main, ["use", "work"])
    assert result.exit_code != 0
    # click renders a ClickException itself; anything that escaped the CLI's
    # error funnel would show up here as the exception object — a traceback.
    assert result.exception is None or isinstance(result.exception, SystemExit)
    out = result.output
    assert "oci_vault" in out                             # names the offending type
    assert "no longer reachable" in out                   # honest about the secrets
    assert "mien init" in out and "mien login" in out     # states the recovery path


def test_push_does_not_claim_success_on_a_retired_backend(runner, mien_cfg, mocker):
    _config_with_retired_backend()
    push = mocker.patch("mien.cli.push_manifest")
    result = runner.invoke(main, ["push"])
    assert result.exit_code != 0, result.output
    push.assert_not_called()
    assert "no-op" not in result.output.lower()  # a retired backend is not "local"
    assert "oci_vault" in result.output


def test_sync_on_a_retired_backend_says_the_backend_was_removed(runner, mien_cfg):
    _config_with_retired_backend()
    result = runner.invoke(main, ["sync"])
    assert result.exit_code != 0
    assert "oci_vault" in result.output and "removed" in result.output


def test_init_yes_auto_imports_manifest_without_prompt(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.health_check.return_value = None
    mocker.patch("mien.cli.subprocess.run")
    mocker.patch("mien.cli.pull_manifest", return_value=_manifest_cfg())
    result = runner.invoke(
        main,
        ["init", "-y", "--backend", "gcp_secret_manager",
         "--project", "p1", "--bootstrap-email", "me@x.com"],
    )
    assert result.exit_code == 0, result.output
    assert "Imported 1 profiles" in result.output
    payload = json.loads(mien_cfg.read_text())
    assert "work" in payload["profiles"]


def test_login_atlassian_stores_token_and_updates_config(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://atl-token"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main,
        [
            "login", "personal", "--service", "atlassian",
            "--atlassian-email", "me@company.com",
            "--base-url", "https://company.atlassian.net",
            "--token-stdin",
        ],
        input="y\nmy-api-token\n",
    )
    assert result.exit_code == 0, result.output
    assert "stored atlassian identity" in result.output
    payload = json.loads(mien_cfg.read_text())
    atl = payload["profiles"]["personal"]["atlassian"]
    assert atl["email"] == "me@company.com"
    assert atl["base_url"] == "https://company.atlassian.net"
    assert atl["api_token_ref"] == "ref://atl-token"


def test_list_shows_atlassian(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://atl-token"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(
        main,
        [
            "login", "personal", "--service", "atlassian",
            "--atlassian-email", "me@company.com",
            "--base-url", "https://company.atlassian.net",
            "--token-stdin",
        ],
        input="y\nmy-api-token\n",
    )
    result = runner.invoke(main, ["list"])
    assert result.exit_code == 0, result.output
    assert "atlassian:me@company.com" in result.output


def test_logout_atlassian_removes_service(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://atl-token"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(
        main,
        [
            "login", "personal", "--service", "atlassian",
            "--atlassian-email", "me@company.com",
            "--base-url", "https://company.atlassian.net",
            "--token-stdin",
        ],
        input="y\nmy-api-token\n",
    )
    result = runner.invoke(main, ["logout", "personal", "--service", "atlassian"])
    assert result.exit_code == 0, result.output
    backend.delete.assert_called_with("ref://atl-token")
    payload = json.loads(mien_cfg.read_text())
    assert payload["profiles"]["personal"]["atlassian"] is None


def test_token_atlassian_prints_api_token(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://atl-token"
    backend.get.return_value = b"my-secret-api-token"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(
        main,
        [
            "login", "personal", "--service", "atlassian",
            "--atlassian-email", "me@company.com",
            "--base-url", "https://company.atlassian.net",
            "--token-stdin",
        ],
        input="y\nmy-secret-api-token\n",
    )
    result = runner.invoke(
        main, ["token", "atlassian"],
        env={"MIEN_PROFILE": "personal", "MIEN_CONFIG": str(mien_cfg)},
    )
    assert result.exit_code == 0, result.output
    assert "my-secret-api-token" in result.output


def test_login_notion_stores_token_and_updates_config(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://notion-token"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main,
        ["login", "personal", "--service", "notion", "--token-stdin"],
        input="y\nmy-notion-token\n",
    )
    assert result.exit_code == 0, result.output
    assert "stored notion identity" in result.output
    payload = json.loads(mien_cfg.read_text())
    assert payload["profiles"]["personal"]["notion"]["api_token_ref"] == "ref://notion-token"


def test_list_shows_notion(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://notion-token"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(
        main,
        ["login", "personal", "--service", "notion", "--token-stdin"],
        input="y\nmy-notion-token\n",
    )
    result = runner.invoke(main, ["list"])
    assert result.exit_code == 0, result.output
    assert "notion" in result.output


def test_logout_notion_removes_service(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://notion-token"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(
        main,
        ["login", "personal", "--service", "notion", "--token-stdin"],
        input="y\nmy-notion-token\n",
    )
    result = runner.invoke(main, ["logout", "personal", "--service", "notion"])
    assert result.exit_code == 0, result.output
    backend.delete.assert_called_with("ref://notion-token")
    payload = json.loads(mien_cfg.read_text())
    assert payload["profiles"]["personal"]["notion"] is None


def test_token_notion_prints_api_token(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://notion-token"
    backend.get.return_value = b"my-secret-notion-token"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(
        main,
        ["login", "personal", "--service", "notion", "--token-stdin"],
        input="y\nmy-secret-notion-token\n",
    )
    result = runner.invoke(
        main, ["token", "notion"],
        env={"MIEN_PROFILE": "personal", "MIEN_CONFIG": str(mien_cfg)},
    )
    assert result.exit_code == 0, result.output
    assert "my-secret-notion-token" in result.output


def _notion_profile(runner, mocker, secret=b"my-secret-notion-token"):
    """A configured profile whose notion token is `secret`."""
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://notion-token"
    backend.get.return_value = secret
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(
        main,
        ["login", "personal", "--service", "notion", "--token-stdin"],
        input="y\n" + secret.decode() + "\n",
    )


def test_token_refuses_where_stdout_is_recorded(runner, mien_cfg, mocker):
    """The accident this guards: a secret printed into an agent transcript.

    Piping `mien token` into a program lets any echo of its input (a traceback, a
    usage error) capture the secret durably. The refusal points at `mien exec`,
    which hands the credential over in the environment instead.
    """
    _notion_profile(runner, mocker)
    result = runner.invoke(
        main, ["token", "notion"],
        env={"MIEN_PROFILE": "personal", "MIEN_CONFIG": str(mien_cfg),
             "CLAUDECODE": "1"},
    )
    assert result.exit_code != 0
    assert "my-secret-notion-token" not in result.output  # the point of all this
    assert "mien exec" in result.output
    assert "NOTION_TOKEN" in result.output
    assert "Bearer $NOTION_TOKEN" in result.output  # Notion really is Bearer
    assert "CLAUDECODE" in result.output


def _atlassian_profile(runner, mocker, secret=b"my-secret-api-token"):
    """A configured profile whose atlassian API token is `secret`."""
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://atl-token"
    backend.get.return_value = secret
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(
        main,
        [
            "login", "personal", "--service", "atlassian",
            "--atlassian-email", "me@company.com",
            "--base-url", "https://company.atlassian.net",
            "--token-stdin",
        ],
        input="y\n" + secret.decode() + "\n",
    )


def test_token_refusal_uses_basic_auth_for_atlassian(runner, mien_cfg, mocker):
    """Atlassian Cloud authenticates with HTTP Basic (email:token), not Bearer.

    A `Authorization: Bearer $ATLASSIAN_API_TOKEN` example would be advice that
    never works, so the refusal must show the `curl -u` form the recipe uses.
    """
    _atlassian_profile(runner, mocker)
    result = runner.invoke(
        main, ["token", "atlassian"],
        env={"MIEN_PROFILE": "personal", "MIEN_CONFIG": str(mien_cfg),
             "CLAUDECODE": "1"},
    )
    assert result.exit_code != 0
    assert "my-secret-api-token" not in result.output
    assert "ATLASSIAN_API_TOKEN" in result.output
    assert 'curl -u "$ATLASSIAN_EMAIL:$ATLASSIAN_API_TOKEN"' in result.output
    assert "Bearer $ATLASSIAN_API_TOKEN" not in result.output


def test_token_refusal_does_not_offer_google_adc_path_as_a_bearer_token(
    runner, mien_cfg, mocker
):
    """$GOOGLE_APPLICATION_CREDENTIALS is a file path — Bearer-ing it always 401s.

    The refusal already says there is no env form of a bare access token; the
    HTTP hint must not then contradict it by sending the path as a token.
    """
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.side_effect = ["ref://oauth", "ref://refresh"]
    mocker.patch("mien.cli.google_installed_app_flow", return_value="refresh-zzz")
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(
        main,
        ["login", "personal", "--service", "google",
         "--email", "me@x.com", "--client-id", "cid"],
        input="y\ncsec\n",
    )
    result = runner.invoke(
        main, ["token", "google"],
        env={"MIEN_PROFILE": "personal", "MIEN_CONFIG": str(mien_cfg),
             "CLAUDECODE": "1"},
    )
    assert result.exit_code != 0
    assert "Bearer $GOOGLE_APPLICATION_CREDENTIALS" not in result.output
    assert "file path" in result.output
    # Points at the honest remedies instead.
    assert "gcloud auth application-default print-access-token" in result.output
    assert "--force" in result.output


def test_a_missing_identity_fails_loud_even_under_a_recorded_context(
    runner, mien_cfg, mocker
):
    """The identity check must outrank the capture refusal.

    Refusing first would answer "this profile has no notion identity" with
    advice to read `$NOTION_TOKEN` — a variable `mien exec` never sets for such
    a profile. Since `exec` overlays the environment without scrubbing, another
    identity's ambient token could satisfy that advice and the call would
    succeed as the wrong person. Misconfiguration must stay loud.
    """
    _atlassian_profile(runner, mocker)  # 'personal' exists, but has no notion
    result = runner.invoke(
        main, ["token", "notion"],
        env={"MIEN_PROFILE": "personal", "MIEN_CONFIG": str(mien_cfg),
             "CLAUDECODE": "1"},
    )
    assert result.exit_code != 0
    assert "has no notion identity" in result.output
    assert "NOTION_TOKEN" not in result.output  # not the refusal's advice


@pytest.mark.parametrize("override", [
    {"MIEN_TOKEN": "capture-ok"},
    {"MIEN_TOKEN": "off"},
])
def test_token_capture_refusal_is_overridable(runner, mien_cfg, mocker, override):
    """Guides rather than traps — the same doctrine as `mien guard`."""
    _notion_profile(runner, mocker)
    result = runner.invoke(
        main, ["token", "notion"],
        env={"MIEN_PROFILE": "personal", "MIEN_CONFIG": str(mien_cfg),
             "CLAUDECODE": "1", **override},
    )
    assert result.exit_code == 0, result.output
    assert "my-secret-notion-token" in result.output


def test_token_capture_refusal_is_overridable_by_force_flag(runner, mien_cfg, mocker):
    _notion_profile(runner, mocker)
    result = runner.invoke(
        main, ["token", "notion", "--force"],
        env={"MIEN_PROFILE": "personal", "MIEN_CONFIG": str(mien_cfg),
             "CLAUDECODE": "1"},
    )
    assert result.exit_code == 0, result.output
    assert "my-secret-notion-token" in result.output


def test_token_prints_normally_outside_a_recorded_context(runner, mien_cfg, mocker):
    """No harness marker, no refusal: a human at a terminal is unaffected."""
    _notion_profile(runner, mocker)
    result = runner.invoke(
        main, ["token", "notion"],
        env={"MIEN_PROFILE": "personal", "MIEN_CONFIG": str(mien_cfg)},
    )
    assert result.exit_code == 0, result.output
    assert "my-secret-notion-token" in result.output


def test_token_refusal_precedes_touching_the_secrets_backend(runner, mien_cfg, mocker):
    """Refuse before minting: a blocked call must not spend a credential."""
    _notion_profile(runner, mocker)
    # Re-patch so the mock counts only this invocation, not the setup above.
    backend = mocker.patch("mien.cli.load_backend")
    result = runner.invoke(
        main, ["token", "notion"],
        env={"MIEN_PROFILE": "personal", "MIEN_CONFIG": str(mien_cfg),
             "CLAUDECODE": "1"},
    )
    # Pin *why* it stopped: the refusal, not some unrelated early failure.
    assert result.exit_code != 0
    assert "refusing to print a raw secret" in result.output
    backend.assert_not_called()


def _pinned_config(tmp_path, monkeypatch, **scopes):
    """Write a config whose profiles claim directories via default_for."""
    from mien.config import BackendConfig, Config, Profile, SecretNaming, save_config
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "config.json"))
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={n: Profile(name=n, default_for=list(g)) for n, g in scopes.items()},
    ))


def test_which_resolves_profile_from_cwd(runner, tmp_path, monkeypatch):
    work = tmp_path / "Projects" / "acme" / "src"
    work.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch, work=["*/Projects/acme"])
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    monkeypatch.chdir(work)
    result = runner.invoke(main, ["which"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "work"


def test_which_resolves_through_a_symlinked_path_via_pwd(runner, tmp_path, monkeypatch):
    """`os.getcwd()` resolves symlinks and the shell's `$PWD` does not, so a scope
    the generated `case "$PWD/" in ...` matches must match here too — otherwise the
    same directory has an ambient env from one profile and no identity at all."""
    (tmp_path / "real" / "acme").mkdir(parents=True)
    (tmp_path / "Projects").symlink_to(tmp_path / "real")
    logical = tmp_path / "Projects" / "acme"
    _pinned_config(tmp_path, monkeypatch, work=["*/Projects/acme"])
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    monkeypatch.chdir(logical)
    assert "/Projects/" not in os.getcwd()      # the physical path really differs
    monkeypatch.setenv("PWD", str(logical))     # what the shell reports there
    result = runner.invoke(main, ["which"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "work"


def test_which_ignores_a_pwd_naming_a_different_directory(runner, tmp_path, monkeypatch):
    """`PWD` is inherited and goes stale in any subprocess that chdir'd. Trusting
    it past `samefile` would hand out another project's credentials."""
    here = tmp_path / "elsewhere"
    here.mkdir()
    claimed = tmp_path / "Projects" / "acme"
    claimed.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch, work=["*/Projects/acme"])
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    monkeypatch.chdir(here)
    monkeypatch.setenv("PWD", str(claimed))     # a real directory, but not this one
    result = runner.invoke(main, ["which"])
    assert result.exit_code != 0
    assert result.stdout.strip() == ""


def test_which_falls_back_to_getcwd_when_pwd_is_unusable(runner, tmp_path, monkeypatch):
    work = tmp_path / "Projects" / "acme"
    work.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch, work=["*/Projects/acme"])
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    monkeypatch.chdir(work)

    monkeypatch.setenv("PWD", "/no/such/directory/anywhere")   # points nowhere
    assert runner.invoke(main, ["which"]).stdout.strip() == "work"

    monkeypatch.setenv("PWD", "not/absolute")                  # not a real cwd
    assert runner.invoke(main, ["which"]).stdout.strip() == "work"

    monkeypatch.delenv("PWD", raising=False)                   # unset entirely
    assert runner.invoke(main, ["which"]).stdout.strip() == "work"


def test_which_exits_nonzero_with_no_output_when_unclaimed(runner, tmp_path, monkeypatch):
    """Callers substitute this into other commands, so an unresolved directory
    must not print a profile name that would then be used."""
    loose = tmp_path / "elsewhere"
    loose.mkdir()
    _pinned_config(tmp_path, monkeypatch, work=["*/Projects/acme"])
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    monkeypatch.chdir(loose)
    result = runner.invoke(main, ["which"])
    assert result.exit_code != 0
    assert result.stdout.strip() == ""


def test_which_prefers_an_explicitly_activated_profile(runner, tmp_path, monkeypatch):
    """Someone who ran `mien use` said what they wanted; a directory default
    must not silently override a deliberate act."""
    work = tmp_path / "Projects" / "acme"
    work.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch, work=["*/Projects/acme"], personal=[])
    monkeypatch.setenv("MIEN_PROFILE", "personal")
    monkeypatch.chdir(work)
    result = runner.invoke(main, ["which"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "personal"


def test_which_rejects_an_active_profile_absent_from_config(runner, tmp_path, monkeypatch):
    """MIEN_PROFILE is an arbitrary string — a renamed or deleted profile leaves a
    stale one exported. `which` feeds other commands, so a name that resolves to
    nothing must fail here rather than downstream, and stdout must stay empty."""
    work = tmp_path / "Projects" / "acme"
    work.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch, work=["*/Projects/acme"])
    monkeypatch.setenv("MIEN_PROFILE", "deleted-profile")
    monkeypatch.chdir(work)
    result = runner.invoke(main, ["which"])
    assert result.exit_code != 0
    assert "deleted-profile" in result.output
    assert "not found" in result.output
    assert result.stdout.strip() == ""


def test_which_warns_when_active_profile_contradicts_the_directory(runner, tmp_path, monkeypatch):
    """The override is honoured, but silently acting against the directory's
    default is exactly the confusion this feature exists to remove."""
    work = tmp_path / "Projects" / "acme"
    work.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch, work=["*/Projects/acme"], personal=[])
    monkeypatch.setenv("MIEN_PROFILE", "personal")
    monkeypatch.chdir(work)
    result = runner.invoke(main, ["which"], catch_exceptions=False)
    assert "work" in result.stderr
    assert result.stdout.strip() == "personal"


def test_which_refuses_to_guess_between_equally_specific_scopes(runner, tmp_path, monkeypatch):
    shared = tmp_path / "Projects" / "shared"
    shared.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch,
                   alpha=["*/Projects/shared"], bravo=["*/Projects/shared"])
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    monkeypatch.chdir(shared)
    result = runner.invoke(main, ["which"])
    assert result.exit_code != 0
    assert "claimed with equal specificity by: alpha, bravo" in result.output


def test_which_prefers_an_activated_profile_over_an_ambiguous_directory(
    runner, tmp_path, monkeypatch
):
    """An explicit `mien use` leaves nothing to guess, so a directory two
    profiles claim equally must not abort the command."""
    shared = tmp_path / "Projects" / "shared"
    shared.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch,
                   alpha=["*/Projects/shared"], bravo=["*/Projects/shared"],
                   personal=[])
    monkeypatch.setenv("MIEN_PROFILE", "personal")
    monkeypatch.chdir(shared)
    result = runner.invoke(main, ["which"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "personal"


def test_which_warns_when_the_directory_is_ambiguous_under_an_override(
    runner, tmp_path, monkeypatch
):
    """Proceeding under the override is right, but the clashing scopes are a
    real misconfiguration the user should hear about."""
    shared = tmp_path / "Projects" / "shared"
    shared.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch,
                   alpha=["*/Projects/shared"], bravo=["*/Projects/shared"],
                   personal=[])
    monkeypatch.setenv("MIEN_PROFILE", "personal")
    monkeypatch.chdir(shared)
    result = runner.invoke(main, ["which"], catch_exceptions=False)
    assert "claimed by several profiles with equal specificity" in result.stderr
    assert "using 'personal'" in result.stderr
    assert result.stdout.strip() == "personal"


def test_run_uses_the_activated_profile_when_the_directory_is_ambiguous(
    runner, tmp_path, monkeypatch, mocker
):
    shared = tmp_path / "Projects" / "shared"
    shared.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch,
                   alpha=["*/Projects/shared"], bravo=["*/Projects/shared"],
                   personal=[])
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("MIEN_PROFILE", "personal")
    monkeypatch.chdir(shared)
    mocker.patch("mien.cli.load_backend")
    called = mocker.patch("mien.cli.subprocess.call", return_value=0)

    result = runner.invoke(main, ["run", "--", "printenv", "MIEN_PROFILE"])
    assert result.exit_code == 0, result.output
    assert called.call_args.kwargs["env"]["MIEN_PROFILE"] == "personal"


def test_run_executes_under_the_directory_profile(runner, tmp_path, monkeypatch, mocker):
    work = tmp_path / "Projects" / "acme"
    work.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch, work=["*/Projects/acme"])
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    monkeypatch.chdir(work)
    mocker.patch("mien.cli.load_backend")
    called = mocker.patch("mien.cli.subprocess.call", return_value=0)

    result = runner.invoke(main, ["run", "--", "printenv", "MIEN_PROFILE"])
    assert result.exit_code == 0, result.output
    assert called.call_args.args[0] == ["printenv", "MIEN_PROFILE"]
    assert called.call_args.kwargs["env"]["MIEN_PROFILE"] == "work"


def test_run_honours_an_inherited_profile_over_the_directory(runner, tmp_path, monkeypatch, mocker):
    """The child must run as the activated profile, not the directory's.

    An agent session launched from a terminal where someone ran `mien-use work`
    inherits MIEN_PROFILE into every command, so this is the routine case, not an
    exotic one — SKILL.md says so and the whole override contract rests on it.
    Pinned here because it was not: making `run` alone prefer the directory left
    the entire suite green while inverting credential routing for those sessions.
    """
    pinned = tmp_path / "Projects" / "acme"
    pinned.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch, work=["*/Projects/acme"], personal=[])
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("MIEN_PROFILE", "personal")
    monkeypatch.chdir(pinned)
    mocker.patch("mien.cli.load_backend")
    called = mocker.patch("mien.cli.subprocess.call", return_value=0)

    result = runner.invoke(main, ["run", "--", "true"])
    assert result.exit_code == 0, result.output
    # The directory says 'work'; the activated profile must still win.
    assert called.call_args.kwargs["env"]["MIEN_PROFILE"] == "personal"


def test_run_lets_the_profile_override_the_ambient_environment(runner, tmp_path, monkeypatch, mocker):
    """The profile must win the merge, not the shell it was launched from.

    `_run_as_profile` builds the child env as {**os.environ, **bundle.env}. If
    that order were ever reversed, a shell already carrying GH_TOKEN from an
    earlier `mien use` would hand the child THAT token while the child still
    reports the requested profile — a silent cross-identity leak, and the exact
    failure this tool exists to prevent. Nothing pinned the order before.
    """
    work = tmp_path / "Projects" / "acme"
    work.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch, work=["*/Projects/acme"])
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    # A stale value from some earlier activation, still exported in this shell.
    monkeypatch.setenv("MIEN_EPHEMERAL_DIR", "/stale/from/an/earlier/shell")
    monkeypatch.chdir(work)
    mocker.patch("mien.cli.load_backend")
    called = mocker.patch("mien.cli.subprocess.call", return_value=0)

    result = runner.invoke(main, ["run", "--", "true"])
    assert result.exit_code == 0, result.output
    child_env = called.call_args.kwargs["env"]
    # build_env sets MIEN_EPHEMERAL_DIR; the profile's value must displace the
    # ambient one rather than the other way round.
    assert child_env["MIEN_EPHEMERAL_DIR"] != "/stale/from/an/earlier/shell"
    assert child_env["MIEN_PROFILE"] == "work"
    # Ambient values the profile does NOT define still pass through.
    assert child_env["TMPDIR"] == str(tmp_path)


def test_run_rejects_a_stale_active_profile(runner, tmp_path, monkeypatch, mocker):
    """`run` indexes cfg.profiles[name] directly, on the strength of the check in
    _resolve_cwd_profile. If that check ever moves, this fails loudly here rather
    than as a KeyError traceback."""
    work = tmp_path / "Projects" / "acme"
    work.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch, work=["*/Projects/acme"])
    monkeypatch.setenv("MIEN_PROFILE", "deleted-profile")
    monkeypatch.chdir(work)
    called = mocker.patch("mien.cli.subprocess.call", return_value=0)

    result = runner.invoke(main, ["run", "--", "true"])
    assert result.exit_code != 0
    assert "deleted-profile" in result.output
    assert "not found" in result.output
    called.assert_not_called()


def test_run_refuses_an_ambiguous_directory_without_an_override(runner, tmp_path, monkeypatch, mocker):
    """Only the override variant was pinned; the refusal itself was not."""
    shared = tmp_path / "Projects" / "shared"
    shared.mkdir(parents=True)
    _pinned_config(tmp_path, monkeypatch,
                   alpha=["*/Projects/shared"], bravo=["*/Projects/shared"])
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    monkeypatch.chdir(shared)
    called = mocker.patch("mien.cli.subprocess.call", return_value=0)

    result = runner.invoke(main, ["run", "--", "true"])
    assert result.exit_code != 0
    assert "claimed with equal specificity by: alpha, bravo" in result.output
    called.assert_not_called()


def test_run_removes_ephemeral_files_after_child_exits(runner, tmp_path, monkeypatch, mocker):
    """`run` spawns the child, so like `exec` it owns the plaintext credential
    files build_env writes — and no shell EXIT trap sweeps them on this path."""
    from mien.config import (BackendConfig, Config, Profile, SecretNaming,
                             SlackWorkspace, save_config)
    work = tmp_path / "Projects" / "acme"
    work.mkdir(parents=True)
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"work": Profile(
            name="work",
            default_for=["*/Projects/acme"],
            slack=[SlackWorkspace(workspace="team-a", user_token_ref="ref://slack")],
        )},
    ))
    mocker.patch("mien.cli.load_backend").return_value.get.return_value = b"xoxp-secret"
    monkeypatch.chdir(work)

    seen = {}

    def fake_call(argv, env=None, **kw):
        # The file must exist WHILE the child runs, or this test would pass
        # vacuously against a build_env that wrote nothing.
        seen["path"] = env["MIEN_SLACK_TOKENS"]
        seen["body"] = Path(env["MIEN_SLACK_TOKENS"]).read_text()
        return 0

    mocker.patch("mien.cli.subprocess.call", side_effect=fake_call)
    result = runner.invoke(main, ["run", "--", "true"])
    assert result.exit_code == 0, result.output
    assert "xoxp-secret" in seen["body"]
    assert not Path(seen["path"]).exists()
    assert list((tmp_path / "mien").iterdir()) == []


def test_run_names_the_remedy_when_the_directory_is_unclaimed(runner, tmp_path, monkeypatch):
    loose = tmp_path / "elsewhere"
    loose.mkdir()
    _pinned_config(tmp_path, monkeypatch, work=["*/Projects/acme"])
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    monkeypatch.chdir(loose)
    result = runner.invoke(main, ["run", "--", "true"])
    assert result.exit_code != 0
    assert "default_for" in result.output or "mien exec" in result.output


def test_env_sync_writes_ambient_and_wires_zshenv(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from mien.cli import main
    from mien.config import (Config, BackendConfig, SecretNaming, Profile,
                             ProjectEnvScope, save_config)
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("HOME", str(tmp_path))
    save_config(Config(schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"work": Profile(name="work", project_env=[
            ProjectEnvScope(match="*/work", env={"AWS_PROFILE": "work"})])}))
    res = CliRunner().invoke(main, ["env", "sync"])
    assert res.exit_code == 0, res.output
    ambient = (tmp_path / "config.json").parent / "ambient.zsh"
    assert 'export AWS_PROFILE="work"' in ambient.read_text()
    assert str(ambient) in (tmp_path / ".zshenv").read_text()
    assert "work" in res.output


def _env_sync_with_scope(monkeypatch, tmp_path, scope):
    from click.testing import CliRunner
    from mien.cli import main
    from mien.config import (Config, BackendConfig, SecretNaming, Profile,
                             ProjectEnvScope, save_config)
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("HOME", str(tmp_path))
    save_config(Config(schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"work": Profile(name="work", project_env=[
            ProjectEnvScope(match=scope, env={"AWS_PROFILE": "work"})])}))
    return CliRunner().invoke(main, ["env", "sync"])


def test_env_sync_warns_when_a_scope_variable_is_unset_in_zshenv(monkeypatch, tmp_path):
    # "$WORK_ROOT/$TEAM/*" is empty in ~/.zshenv (read before ~/.zshrc), so the
    # emitted pattern collapses to "//*" and AWS_PROFILE would be exported
    # everywhere.
    #
    # Two variables, deliberately: the message also echoes the scope verbatim, so
    # asserting on "$WORK_ROOT" alone passes even if the rendered reference list is
    # garbage. The contiguous "$WORK_ROOT, $TEAM" appears nowhere in the scope text
    # ("$WORK_ROOT/$TEAM/*"), so only the list itself can put it on stderr.
    res = _env_sync_with_scope(monkeypatch, tmp_path, "$WORK_ROOT/$TEAM/*")
    assert res.exit_code == 0, res.output
    assert "$WORK_ROOT, $TEAM" in res.stderr             # every offender, in order,
    assert "$WORK_ROOT" in res.stderr                    # each rendered as a reference
    assert "'work'" in res.stderr                        # names the profile
    assert "~/.zshenv" in res.stderr and "~/.zshrc" in res.stderr    # says why
    assert "literal path" in res.stderr                  # says what to do
    assert "warning" in res.stderr
    # warned, not rejected: the file is still written, unchanged by the check
    ambient = (tmp_path / "config.json").parent / "ambient.zsh"
    assert 'case "$PWD/" in $WORK_ROOT/$TEAM/*)' in ambient.read_text()
    assert 'export AWS_PROFILE="work"' in ambient.read_text()


@pytest.mark.parametrize("scope", ["~/Projects/acme", "$HOME/Projects/acme",
                                   "*/work/arinyaho"])
def test_env_sync_is_silent_for_scopes_that_survive_zshenv(monkeypatch, tmp_path, scope):
    res = _env_sync_with_scope(monkeypatch, tmp_path, scope)
    assert res.exit_code == 0, res.output
    assert "warning" not in res.stderr and "~/.zshrc" not in res.stderr
    ambient = (tmp_path / "config.json").parent / "ambient.zsh"
    assert f'case "$PWD/" in {scope}/*)' in ambient.read_text()


def _custom_cfg(tmp_path, monkeypatch, **customs):
    """A config whose profiles carry `custom` maps: {profile: {VAR: ref}}."""
    from mien.config import (BackendConfig, Config, Profile, SecretNaming,
                             save_config)
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "c.json"))
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={},
        secret_naming=SecretNaming(default=BUILTIN_DEFAULT,
                                   slack_token=BUILTIN_SLACK_TOKEN),
        profiles={name: Profile(name=name, custom=dict(custom))
                  for name, custom in customs.items()},
    ))


def test_login_custom_stores_a_ref_and_never_the_secret(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://anthropic"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main,
        ["login", "work", "--service", "custom", "--name", "ANTHROPIC_API_KEY",
         "--token-stdin"],
        input="y\nsk-ant-super-secret\n",
    )
    assert result.exit_code == 0, result.output
    assert "stored ANTHROPIC_API_KEY for work" in result.output
    # The secret goes to the backend under the `default` template, with the
    # variable name verbatim as the kind.
    name, payload = backend.put.call_args[0]
    assert name == "mien-work-custom-ANTHROPIC_API_KEY"
    assert payload == b"sk-ant-super-secret"
    # Only the reference lands in the config — this is the whole difference from
    # `project_env`, whose values are stored verbatim.
    raw = mien_cfg.read_text()
    assert "sk-ant-super-secret" not in raw
    assert json.loads(raw)["profiles"]["work"]["custom"] == {
        "ANTHROPIC_API_KEY": "ref://anthropic"}


def test_the_manifest_carries_the_custom_name_but_not_its_secret(runner, mien_cfg, mocker):
    """`mien push` uploads config.json, so a secret in it would be uploaded too.

    The real `push_manifest` runs here — mocking it would prove nothing about
    what it sends.
    """
    from mien.manifest import MANIFEST_SECRET_NAME
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://anthropic"
    mocker.patch("mien.cli.pull_manifest", return_value=None)
    runner.invoke(main, ["init", "--backend", "gcp_secret_manager",
                         "--project", "p1", "--bootstrap-email", "me@x.com"])
    result = runner.invoke(
        main,
        ["login", "work", "--service", "custom", "--name", "ANTHROPIC_API_KEY",
         "--token-stdin"],
        input="y\nsk-ant-super-secret\n",
    )
    assert result.exit_code == 0, result.output
    manifests = [c.args[1] for c in backend.put.call_args_list
                 if c.args[0] == MANIFEST_SECRET_NAME]
    assert manifests, backend.put.call_args_list
    body = manifests[-1].decode("utf-8")
    assert "sk-ant-super-secret" not in body
    assert "ANTHROPIC_API_KEY" in body and "ref://anthropic" in body


def test_login_custom_requires_a_name(runner, mien_cfg, mocker):
    mocker.patch("mien.cli.load_backend")
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(main, ["login", "work", "--service", "custom"], input="y\n")
    assert result.exit_code != 0
    assert "--name is required for --service custom" in result.output


@pytest.mark.parametrize("service", ["github", "notion", "slack"])
def test_login_name_on_another_service_is_an_error_not_a_silent_ignore(
    runner, mien_cfg, mocker, service
):
    """Ignoring it is how someone believes they stored a key and overwrote a token."""
    mocker.patch("mien.cli.load_backend")
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main, ["login", "work", "--service", service, "--name", "ANTHROPIC_API_KEY"],
        input="y\n",
    )
    assert result.exit_code != 0
    assert "--name is only meaningful with --service custom" in result.output
    assert f"--service {service} has no such name" in result.output


@pytest.mark.parametrize("name, expect", [
    ("2FA", "is not a usable environment variable name"),
    ("MY VAR", "is not a usable environment variable name"),
    ("api-key", "is not a usable environment variable name"),
    ("GH_TOKEN", "already uses for github"),
    ("AWS_PROFILE", "already uses for aws"),
    ("MIEN_PROFILE", "already uses for mien itself"),
    # Not mien's, but the shell's — and the scrub is the union over all profiles,
    # so accepting one of these makes every `mien use` anywhere strip it.
    ("PATH", "how the shell — and mien's own loader — finds every program it runs"),
    ("HOME", "where the shell, mien and every other tool look for files"),
    ("TMPDIR", "where mien writes the ephemeral files that carry your credentials"),
    # Not read as an instruction but as a safety signal, and mien reads its
    # absence as "no agent is watching": accepting one of these makes every
    # `mien use` anywhere emit `unset CLAUDECODE`, disarming the refusals in
    # `mien token` and `mien exec` with nothing to show that it happened.
    ("CLAUDECODE", "mien reads it to decide whether an agent is driving"),
    ("MIEN_CAPTURED", "the marker you set yourself to tell mien this harness records"),
])
def test_login_custom_refuses_an_unusable_name_before_reading_a_secret(
    runner, mien_cfg, mocker, name, expect
):
    """Refused at the flag, and refused before anything is stored or prompted."""
    backend = mocker.patch("mien.cli.load_backend").return_value
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main, ["login", "work", "--service", "custom", "--name", name,
               "--token-stdin"],
        input="y\nsk-ant-super-secret\n",
    )
    assert result.exit_code != 0
    assert expect in result.output
    backend.put.assert_not_called()
    # And the flag error blames the flag, not the config it never read.
    assert "The config is at" not in result.output


@pytest.mark.parametrize("name", ["MY_PATH", "PATH_TO_KEY", "HOMEBREW_TOKEN",
                                  "MY_CLAUDECODE", "CLAUDECODE_EXTRA"])
def test_login_custom_accepts_a_name_that_merely_contains_a_critical_one(
    runner, mien_cfg, mocker, name
):
    """The shell-critical and capture-marker rules are exact match, not substring.

    A substring test would refuse ordinary credentials — `PATH_TO_KEY` is a name
    someone reasonably picks — while protecting nothing extra: the loader emits
    `unset PATH_TO_KEY`, which touches no variable the shell reads, and `unset
    MY_CLAUDECODE` clears no marker `capture_context` ever looks at.
    """
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://custom"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main, ["login", "work", "--service", "custom", "--name", name,
               "--token-stdin"],
        input="y\nsk-secret\n",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(mien_cfg.read_text())["profiles"]["work"]["custom"] == {
        name: "ref://custom"}


def test_login_custom_composes_several_variables_on_one_profile(runner, mien_cfg, mocker):
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.side_effect = ["ref://anthropic", "ref://npm"]
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(main, ["login", "work", "--service", "custom", "--name",
                         "ANTHROPIC_API_KEY", "--token-stdin"], input="y\nsk-a\n")
    result = runner.invoke(main, ["login", "work", "--service", "custom", "--name",
                                  "NPM_TOKEN", "--token-stdin"], input="npm-t\n")
    assert result.exit_code == 0, result.output
    assert json.loads(mien_cfg.read_text())["profiles"]["work"]["custom"] == {
        "ANTHROPIC_API_KEY": "ref://anthropic", "NPM_TOKEN": "ref://npm"}


def test_login_custom_gives_two_names_differing_only_in_case_two_secrets(
    runner, mien_cfg, mocker
):
    """`TOKEN` and `token` are two shell variables, so they are two stored secrets.

    Case-folding the rendered kind made one backend name for both: the second
    login overwrote the first login's secret (`put` is a plain overwrite) and the
    config kept two variables pointing at the survivor — one credential silently
    destroyed, and a dangling reference the moment either was logged out.
    """
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.side_effect = lambda name, value: f"ref://{name}"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    runner.invoke(main, ["login", "work", "--service", "custom", "--name",
                         "TOKEN", "--token-stdin"], input="y\nupper-secret\n")
    result = runner.invoke(main, ["login", "work", "--service", "custom", "--name",
                                  "token", "--token-stdin"], input="lower-secret\n")
    assert result.exit_code == 0, result.output
    stored = {c.args[0]: c.args[1] for c in backend.put.call_args_list}
    assert stored == {"mien-work-custom-TOKEN": b"upper-secret",
                      "mien-work-custom-token": b"lower-secret"}
    custom = json.loads(mien_cfg.read_text())["profiles"]["work"]["custom"]
    assert custom == {"TOKEN": "ref://mien-work-custom-TOKEN",
                      "token": "ref://mien-work-custom-token"}
    # The point of the two asserts above, said once more as the property: no two
    # variables may share a reference, or `logout` of one guts the other.
    assert len(set(custom.values())) == 2


@pytest.mark.parametrize("template", [
    # `{kind}` dropped outright.
    "mien-{profile}-{service}",
    # `{kind}` spent through a truncating format spec, which renders
    # `mien-work-custom-A` for ANTHROPIC_API_KEY and for AWS_THING alike. This is
    # the same destruction by another spelling, and it used to pass the check: the
    # token was "present", so both logins exited 0 and the second secret replaced
    # the first. The rule is structural now, so the spec cannot buy its way in.
    "mien-{profile}-{service}-{kind:.1s}",
    # The degenerate spec, byte-identical in effect to the first case.
    "mien-{profile}-{service}-{kind:.0s}",
])
def test_two_custom_logins_cannot_both_succeed_under_a_collapsing_template(
    runner, mien_cfg, mocker, template
):
    """The same silent overwrite, reached through `secret_naming` instead of case.

    `secret_naming.default` is hand-editable and a pulled manifest can carry it,
    and `{kind}` is the *only* thing that keeps one custom variable's secret apart
    from the next. With it dropped, both logins used to exit 0, both refs pointed
    at one secret, `mien exec` handed the same value out under both variable names,
    and `mien logout` of either left the other dangling — a profile that cannot be
    activated at all.

    Nothing is stored now: the config that says so cannot be loaded, so the first
    login already fails, before a secret is read or written.
    """
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.side_effect = lambda name, value: f"ref://{name}"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    payload = json.loads(mien_cfg.read_text())
    payload["secret_naming"]["default"] = template
    mien_cfg.write_text(json.dumps(payload))
    for name in ("ANTHROPIC_API_KEY", "NPM_TOKEN"):
        result = runner.invoke(
            main, ["login", "work", "--service", "custom", "--name", name,
                   "--token-stdin"],
            input="y\nsecret-for-%s\n" % name,
        )
        assert result.exit_code != 0, result.output
        assert "does not use {kind}" in result.output
    backend.put.assert_not_called()
    # And the config is untouched: nothing claims to have stored anything.
    assert json.loads(mien_cfg.read_text())["profiles"] == {}


@pytest.mark.parametrize("template", [
    # A token `slack_token` is never rendered with — the one that belongs to the
    # other template.
    "mien-{profile}-slack-{workspace}-{kind}",
    # A positional field, which no check over field names can judge.
    "mien-{profile}-slack-{workspace}-{}",
])
def test_a_bad_slack_token_template_does_not_crash_an_unrelated_login(
    runner, mien_cfg, mocker, template
):
    """The surprising blast radius: `slack_token` breaks a github login.

    `_reject_reserved_secret_name` renders BOTH templates on every `mien login`, to
    check neither one collides with the config-manifest secret. So a `slack_token`
    that cannot render took down `--service github`, which has nothing to do with
    Slack — exit 1, empty stdout, and an uncaught `KeyError` (a traceback in a real
    terminal) rather than any message naming the template at fault.

    It still fails, and it must: the config names a template mien cannot render, so
    refusing to load it is the honest answer. What this pins is that the failure is
    now a legible config error that names `slack_token` and says what to write —
    not an exception from the middle of a login.
    """
    mocker.patch("mien.cli.load_backend")
    runner.invoke(main, ["init"], input="2\nmien-\n")
    payload = json.loads(mien_cfg.read_text())
    payload["secret_naming"]["slack_token"] = template
    mien_cfg.write_text(json.dumps(payload))
    result = runner.invoke(
        main, ["login", "work", "--service", "github", "--token-stdin"],
        input="y\nghp-token\n",
    )
    assert result.exit_code != 0
    # Translated, not propagated: a traceback would leave the raw exception here.
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "KeyError" not in result.output and "Traceback" not in result.output
    assert "secret_naming: 'slack_token' template" in result.output
    assert "mien-{profile}-slack-{workspace}-token" in result.output


def test_login_custom_accepts_a_secret_cmd_reference(runner, mien_cfg, mocker):
    """The existing secret-reading path, unchanged — no new way to supply one."""
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.put.return_value = "ref://anthropic"
    runner.invoke(main, ["init"], input="2\nmien-\n")
    result = runner.invoke(
        main, ["login", "work", "--service", "custom", "--name", "ANTHROPIC_API_KEY",
               "--secret-cmd", "printf sk-from-vault"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert backend.put.call_args[0][1] == b"sk-from-vault"


def test_logout_custom_deletes_the_secret_and_forgets_the_name(runner, tmp_path, monkeypatch, mocker):
    _custom_cfg(tmp_path, monkeypatch,
                work={"ANTHROPIC_API_KEY": "ref://a", "NPM_TOKEN": "ref://n"})
    backend = mocker.patch("mien.cli.load_backend").return_value
    result = CliRunner().invoke(
        main, ["logout", "work", "--service", "custom", "--name", "ANTHROPIC_API_KEY"])
    assert result.exit_code == 0, result.output
    assert "removed custom variable ANTHROPIC_API_KEY from work" in result.output
    backend.delete.assert_called_once_with("ref://a")
    payload = json.loads((tmp_path / "c.json").read_text())
    assert payload["profiles"]["work"]["custom"] == {"NPM_TOKEN": "ref://n"}


def test_logout_custom_requires_a_name(runner, tmp_path, monkeypatch, mocker):
    _custom_cfg(tmp_path, monkeypatch, work={"ANTHROPIC_API_KEY": "ref://a"})
    mocker.patch("mien.cli.load_backend")
    result = CliRunner().invoke(main, ["logout", "work", "--service", "custom"])
    assert result.exit_code != 0
    assert "--name is required for --service custom" in result.output


def test_logout_custom_names_a_variable_the_profile_does_not_have(
    runner, tmp_path, monkeypatch, mocker
):
    """"removed" would be a lie about a credential, so a typo fails instead."""
    _custom_cfg(tmp_path, monkeypatch, work={"NPM_TOKEN": "ref://n"})
    backend = mocker.patch("mien.cli.load_backend").return_value
    result = CliRunner().invoke(
        main, ["logout", "work", "--service", "custom", "--name", "ANTHROPIC_API_KEY"])
    assert result.exit_code != 0
    assert "has no custom variable 'ANTHROPIC_API_KEY'" in result.output
    assert "it has: NPM_TOKEN" in result.output
    backend.delete.assert_not_called()


def test_use_clears_a_custom_var_the_new_profile_does_not_define(
    tmp_path, monkeypatch, mocker
):
    """The cross-profile leak, end to end through the CLI.

    `work` defines ANTHROPIC_API_KEY and `personal` does not, so switching to
    `personal` in a shell that activated `work` must clear it — otherwise one
    identity hands its API key to another, which is the thing mien exists to stop.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    _custom_cfg(tmp_path, monkeypatch,
                work={"ANTHROPIC_API_KEY": "ref://a"}, personal={})
    mocker.patch("mien.cli.load_backend")
    result = CliRunner().invoke(main, ["use", "personal"])
    assert result.exit_code == 0, result.output
    script = Path(result.output.strip().split("'")[1])
    scrub = next(ln for ln in script.read_text().splitlines()
                 if ln.startswith("unset "))
    assert "ANTHROPIC_API_KEY" in scrub.split()


def test_use_exports_a_custom_var_without_printing_it(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    _custom_cfg(tmp_path, monkeypatch, work={"ANTHROPIC_API_KEY": "ref://a"})
    mocker.patch("mien.cli.load_backend").return_value.get.return_value = b"sk-secret\n"
    result = CliRunner().invoke(main, ["use", "work"])
    assert result.exit_code == 0, result.output
    assert "sk-secret" not in result.output
    body = Path(result.output.strip().split("'")[1]).read_text()
    assert "export ANTHROPIC_API_KEY='sk-secret'" in body


def test_use_names_the_variable_when_its_secret_is_gone_instead_of_crashing(
    tmp_path, monkeypatch, mocker
):
    """A reference whose secret was deleted is an error a person can act on.

    Nothing on this path caught `SecretNotFound`, so a secret removed out of band
    — or one that two references shared before mien stopped folding their names —
    came out as a traceback with a bare ref in it and a profile that would not
    activate.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    _custom_cfg(tmp_path, monkeypatch, work={"ANTHROPIC_API_KEY": "ref://gone"})
    backend = mocker.patch("mien.cli.load_backend").return_value
    backend.get.side_effect = SecretNotFound("ref://gone")
    result = CliRunner().invoke(main, ["use", "work"])
    assert result.exit_code != 0
    # Translated, not propagated: a traceback would leave `result.exception` as
    # the backend error itself.
    assert not isinstance(result.exception, SecretNotFound)
    assert "ref://gone" in result.output          # the reference,
    assert "ANTHROPIC_API_KEY" in result.output   # the variable it delivers,
    assert "'work'" in result.output              # the profile that is stuck,
    assert "mien login" in result.output          # and the way out.


def test_unset_clears_custom_vars_named_by_any_profile(tmp_path, monkeypatch):
    _custom_cfg(tmp_path, monkeypatch,
                work={"ANTHROPIC_API_KEY": "ref://a"}, personal={"NPM_TOKEN": "ref://n"})
    result = CliRunner().invoke(main, ["unset"])
    assert result.exit_code == 0, result.output
    assert "unset ANTHROPIC_API_KEY" in result.output
    assert "unset NPM_TOKEN" in result.output
    assert "unset MIEN_PROFILE" in result.output


def test_unset_on_a_broken_config_clears_the_builtins_and_says_what_it_missed(
    tmp_path, monkeypatch
):
    """Fail open, and never quietly.

    `mien-unset` evals this command's stdout (`clears="$(command mien unset)" ||
    return $?`), so exiting non-zero would mean nothing at all is cleared — the
    built-in scrub lost too. It clears what it can and puts the shortfall on
    stderr, which the wrapper does not capture.
    """
    cfg = tmp_path / "c.json"
    cfg.write_text('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
                   ' "profiles": {"work": {"custm": {}}}}')
    monkeypatch.setenv("MIEN_CONFIG", str(cfg))
    result = CliRunner().invoke(main, ["unset"])
    assert result.exit_code == 0, result.stderr
    assert "unset MIEN_PROFILE" in result.stdout
    assert "unset GH_TOKEN" in result.stdout
    assert "config unreadable" in result.stderr
    assert "custom variable" in result.stderr and "may still be set" in result.stderr
    # The announcement must not reach stdout: the wrapper evals that.
    assert "config unreadable" not in result.stdout


def test_status_shows_a_custom_var_as_set_never_its_value(tmp_path, monkeypatch):
    _custom_cfg(tmp_path, monkeypatch, work={"ANTHROPIC_API_KEY": "ref://a"})
    monkeypatch.setenv("MIEN_PROFILE", "work")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret")
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY=<set>" in result.output
    assert "sk-ant-super-secret" not in result.output


def test_status_omits_a_custom_var_that_is_not_set(tmp_path, monkeypatch):
    _custom_cfg(tmp_path, monkeypatch, work={"ANTHROPIC_API_KEY": "ref://a"})
    monkeypatch.setenv("MIEN_PROFILE", "work")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = CliRunner().invoke(main, ["status"])
    assert "ANTHROPIC_API_KEY" not in result.output


def test_status_on_a_broken_config_still_reports_and_says_what_it_cannot_name(
    tmp_path, monkeypatch
):
    cfg = tmp_path / "c.json"
    cfg.write_text('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
                   ' "profiles": {"work": {"custm": {}}}}')
    monkeypatch.setenv("MIEN_CONFIG", str(cfg))
    monkeypatch.setenv("MIEN_PROFILE", "work")
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0, result.stderr
    assert "active: work" in result.stdout
    assert "GH_TOKEN=<set>" in result.stdout
    assert "config unreadable" in result.stderr


def test_list_and_whoami_show_custom_names_only(tmp_path, monkeypatch):
    _custom_cfg(tmp_path, monkeypatch,
                work={"ANTHROPIC_API_KEY": "ref://a", "NPM_TOKEN": "ref://n"})
    runner = CliRunner()
    listing = runner.invoke(main, ["list"])
    assert listing.exit_code == 0, listing.output
    assert "custom:[ANTHROPIC_API_KEY, NPM_TOKEN]" in listing.output
    assert "ref://a" not in listing.output

    card = runner.invoke(main, ["whoami", "work"])
    assert card.exit_code == 0, card.output
    assert "ANTHROPIC_API_KEY, NPM_TOKEN" in card.output
    assert "ref://a" not in card.output

    as_json = runner.invoke(main, ["whoami", "work", "--json"])
    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.output)
    assert data["custom"] == ["ANTHROPIC_API_KEY", "NPM_TOKEN"]
    assert "ref://a" not in as_json.output


def test_a_profile_with_no_custom_vars_says_nothing_about_them(tmp_path, monkeypatch):
    _custom_cfg(tmp_path, monkeypatch, work={})
    runner = CliRunner()
    assert "custom" not in runner.invoke(main, ["list"]).output
    assert "custom" not in runner.invoke(main, ["whoami", "work"]).output
    assert json.loads(runner.invoke(main, ["whoami", "work", "--json"]).output)["custom"] == []


def test_token_has_no_custom_service(runner, tmp_path, monkeypatch):
    """`token` prints a raw secret on stdout; `exec` covers this need instead."""
    _custom_cfg(tmp_path, monkeypatch, work={"ANTHROPIC_API_KEY": "ref://a"})
    result = CliRunner().invoke(main, ["token", "custom", "--profile", "work"])
    assert result.exit_code != 0
    assert "custom" in result.output  # click's invalid-choice message


# --- discover: git remote owners, and claiming one -------------------------


def _remote_cfg(tmp_path, monkeypatch, **owns):
    from mien.config import (BackendConfig, Config, Profile, SecretNaming,
                             save_config)
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "c.json"))
    save_config(Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={},
        secret_naming=SecretNaming(default=BUILTIN_DEFAULT,
                                   slack_token=BUILTIN_SLACK_TOKEN),
        profiles={n: Profile(name=n, owns_remotes=list(pats))
                  for n, pats in owns.items()},
    ))


def _git_repo(path, url):
    """A real repository with a real `origin`, so the scan reads git, not a stub."""
    import subprocess
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url],
                   check=True, capture_output=True)
    return path


def test_discover_reports_remote_owners_as_owned_or_unclaimed(
        runner, tmp_path, monkeypatch):
    _remote_cfg(tmp_path, monkeypatch, work=["github.com/acme-*/*"])
    home = tmp_path / "home"
    _git_repo(home / "Projects" / "api", "git@github.com:acme-inc/api.git")
    _git_repo(home / "Projects" / "blog", "https://github.com/me/blog.git")
    # The credential half is another machine's business here.
    monkeypatch.setattr("mien.cli.discover_all", lambda: [])

    result = runner.invoke(main, ["discover", "--scan-root", str(home)])
    assert result.exit_code == 0, result.output
    assert "✓ github.com/acme-inc — owned by work" in result.output
    assert "· github.com/me" in result.output and "no profile owns it" in result.output
    assert "mien discover --own github.com/me --profile <profile>" in result.output


def test_discover_own_writes_a_glob_that_actually_claims_the_repo(
        runner, tmp_path, monkeypatch):
    from mien.config import load_config
    from mien.resolve import resolve_remote_profile

    _remote_cfg(tmp_path, monkeypatch, work=["github.com/acme-*/*"])
    home = tmp_path / "home"
    _git_repo(home / "code" / "blog", "git@github.com:me/blog.git")
    assert resolve_remote_profile(load_config().profiles, "github.com/me/blog") is None

    result = runner.invoke(main, ["discover", "--scan-root", str(home),
                                  "--own", "github.com/me", "--profile", "work"])
    assert result.exit_code == 0, result.output
    saved = load_config().profiles["work"].owns_remotes
    assert saved == ["github.com/acme-*/*", "github.com/me/*"]
    assert resolve_remote_profile(load_config().profiles,
                                  "git@github.com:me/blog.git") == "work"


def test_discover_own_does_not_let_a_star_in_a_remote_claim_the_host(
        runner, tmp_path, monkeypatch):
    """A repository's own remote URL must not be able to widen a claim: owning
    `github.com/*` claims that one owner literally, not everyone on the host."""
    from mien.config import load_config
    from mien.resolve import resolve_remote_profile

    _remote_cfg(tmp_path, monkeypatch, work=[])
    home = tmp_path / "home"
    _git_repo(home / "code" / "x", "https://github.com/*/x.git")

    result = runner.invoke(main, ["discover", "--scan-root", str(home),
                                  "--own", "github.com/*", "--profile", "work"])
    assert result.exit_code == 0, result.output
    profiles = load_config().profiles
    assert profiles["work"].owns_remotes == ["github.com/[*]/*"]
    assert resolve_remote_profile(profiles, "https://github.com/*/x.git") == "work"
    assert resolve_remote_profile(
        profiles, "git@github.com:unrelated-org/svc.git") is None
    assert resolve_remote_profile(profiles, "https://github.com/anyone/anything") is None


def test_discover_own_refuses_an_owner_no_repo_here_has(
        runner, tmp_path, monkeypatch):
    from mien.config import load_config
    _remote_cfg(tmp_path, monkeypatch, work=[])
    home = tmp_path / "home"
    _git_repo(home / "code" / "blog", "git@github.com:me/blog.git")
    result = runner.invoke(main, ["discover", "--scan-root", str(home),
                                  "--own", "github.com/typo", "--profile", "work"])
    assert result.exit_code != 0
    assert "github.com/me" in result.output  # tells you what it did find
    assert load_config().profiles["work"].owns_remotes == []


def test_discover_own_refuses_to_claim_another_profiles_owner(
        runner, tmp_path, monkeypatch):
    from mien.config import load_config
    _remote_cfg(tmp_path, monkeypatch, work=["github.com/acme-*/*"], personal=[])
    home = tmp_path / "home"
    _git_repo(home / "code" / "api", "git@github.com:acme-inc/api.git")
    result = runner.invoke(main, ["discover", "--scan-root", str(home),
                                  "--own", "github.com/acme-inc",
                                  "--profile", "personal"])
    assert result.exit_code != 0
    assert "already owned by 'work'" in result.output
    assert load_config().profiles["personal"].owns_remotes == []


def test_discover_own_refuses_to_split_a_partly_owned_owner(
        runner, tmp_path, monkeypatch):
    """One repository owned by another profile is enough to refuse: claiming the
    rest would leave the owner split between two identities."""
    from mien.config import load_config
    _remote_cfg(tmp_path, monkeypatch, work=["github.com/me/blog"], personal=[])
    home = tmp_path / "home"
    _git_repo(home / "code" / "blog", "git@github.com:me/blog.git")
    _git_repo(home / "code" / "other", "git@github.com:me/other.git")

    result = runner.invoke(main, ["discover", "--scan-root", str(home),
                                  "--own", "github.com/me", "--profile", "personal"])
    assert result.exit_code != 0
    # Names the other profile and the repository it already owns.
    assert "already owned by 'work' (github.com/me/blog)" in result.output
    assert load_config().profiles["personal"].owns_remotes == []


def test_discover_own_claims_an_owner_nobody_owns(runner, tmp_path, monkeypatch):
    from mien.config import load_config
    _remote_cfg(tmp_path, monkeypatch, work=[], personal=[])
    home = tmp_path / "home"
    _git_repo(home / "code" / "blog", "git@github.com:me/blog.git")
    _git_repo(home / "code" / "other", "git@github.com:me/other.git")

    result = runner.invoke(main, ["discover", "--scan-root", str(home),
                                  "--own", "github.com/me", "--profile", "personal"])
    assert result.exit_code == 0, result.output
    assert load_config().profiles["personal"].owns_remotes == ["github.com/me/*"]


def test_discover_own_finishes_a_partly_owned_owner(runner, tmp_path, monkeypatch):
    """Owning one repository of an owner must not dead-end the rest: the report
    calls that partly owned, and the claim widens it to the whole owner."""
    from mien.config import load_config
    from mien.resolve import resolve_remote_profile

    _remote_cfg(tmp_path, monkeypatch, work=["github.com/me/blog"])
    home = tmp_path / "home"
    _git_repo(home / "code" / "blog", "git@github.com:me/blog.git")
    _git_repo(home / "code" / "other", "git@github.com:me/other.git")
    monkeypatch.setattr("mien.cli.discover_all", lambda: [])

    report = runner.invoke(main, ["discover", "--scan-root", str(home)])
    assert "~ github.com/me — partly owned by work" in report.output
    assert "✓ github.com/me" not in report.output

    result = runner.invoke(main, ["discover", "--scan-root", str(home),
                                  "--own", "github.com/me", "--profile", "work"])
    assert result.exit_code == 0, result.output
    profiles = load_config().profiles
    assert profiles["work"].owns_remotes == ["github.com/me/blog", "github.com/me/*"]
    assert resolve_remote_profile(profiles, "github.com/me/other") == "work"
    assert resolve_remote_profile(profiles, "github.com/me/blog") == "work"

    # Now every repository of the owner resolves — the report says so, and a
    # second claim has nothing left to add.
    report = runner.invoke(main, ["discover", "--scan-root", str(home)])
    assert "✓ github.com/me — owned by work" in report.output
    again = runner.invoke(main, ["discover", "--scan-root", str(home),
                                 "--own", "github.com/me", "--profile", "work"])
    assert again.exit_code != 0
    assert "already owns every repository of github.com/me" in again.output


def test_discover_own_needs_a_profile(runner, tmp_path, monkeypatch):
    _remote_cfg(tmp_path, monkeypatch, work=[])
    result = runner.invoke(main, ["discover", "--scan-root", str(tmp_path),
                                  "--own", "github.com/me"])
    assert result.exit_code != 0
    assert "--profile" in result.output


class TestProfileExportsAreDiscoverable:
    """The failure this exists to prevent, verbatim: a profile holds a slack
    credential, `list` and `whoami` say so, and nothing anywhere names the
    variable it arrives as. The only command that answered — `exec … -- env` —
    is a secret dump an agent sandbox blocks, so the reasonable conclusion from
    a blocked dump was that mien does not support slack, and the next hour went
    into finding a way around a tool that already did the job.

    Both halves are load-bearing. Discoverable, and without a value: a view that
    leaks the token is one a sandbox blocks again, which puts us back here.
    """

    def test_the_card_names_the_variable_a_slack_credential_arrives_as(
            self, runner, tmp_path, monkeypatch):
        _rich_profile_cfg(tmp_path, monkeypatch)
        out = runner.invoke(main, ["whoami", "work"]).output
        assert "slack" in out                      # the fact, as before
        assert "MIEN_SLACK_TOKENS" in out          # and now the way in

    def test_the_json_form_names_it_with_its_source(self, runner, tmp_path, monkeypatch):
        _rich_profile_cfg(tmp_path, monkeypatch)
        env = json.loads(runner.invoke(main, ["whoami", "work", "--json"]).output)["env"]
        slack = {e["var"]: e for e in env if e["service"] == "slack"}
        assert slack["MIEN_SLACK_TOKENS"]["set"] is True
        # one workspace, so the convenience variable is there too
        assert slack["MIEN_SLACK_DEFAULT_TOKEN"]["set"] is True
        assert all("value" not in e for e in env)

    def test_a_variable_the_profile_does_not_get_is_reported_as_unset(
            self, runner, tmp_path, monkeypatch):
        """The dangerous half: `exec` overlays without scrubbing, so a variable
        mien does not set is one another identity's ambient value survives into.
        Silence there reads as "fine"."""
        _rich_profile_cfg(tmp_path, monkeypatch)
        env = json.loads(runner.invoke(main, ["whoami", "work", "--json"]).output)["env"]
        adc = next(e for e in env if e["var"] == "GOOGLE_APPLICATION_CREDENTIALS")
        assert adc["set"] is False and adc["note"]

    def test_no_view_prints_a_value(self, runner, tmp_path, monkeypatch, mocker):
        """The discovery path has to survive a policy that blocks secret dumps,
        so it must never become one. The backend hands out obviously
        secret-shaped values to anything that asks, so a view that resolves one
        prints it — and asking at all is itself the failure."""
        _rich_profile_cfg(tmp_path, monkeypatch)
        backend = mocker.patch("mien.cli.load_backend").return_value
        backend.get.return_value = b"xoxp-111-222-deadbeef"
        for argv in (["whoami", "work"], ["whoami", "work", "--json"], ["list"]):
            out = runner.invoke(main, argv).output
            for leak in ("xox", "ghp_", "AKIA", "ntn_", "ATATT"):
                assert leak not in out, f"{argv} leaked {leak}"
            backend.get.assert_not_called()

    def test_token_slack_points_at_the_command_that_works(self, runner, tmp_path, monkeypatch):
        """Typing `mien token slack` is the right goal at the wrong door. A bare
        "invalid choice" sends the reader looking for another tool."""
        _rich_profile_cfg(tmp_path, monkeypatch)
        result = runner.invoke(main, ["token", "slack", "--profile", "work"])
        assert result.exit_code != 0
        assert "MIEN_SLACK_TOKENS" in result.output
        assert "mien exec" in result.output
        assert "whoami" in result.output
