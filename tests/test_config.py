import json
from pathlib import Path

import pytest

from mien.config import (
    BackendConfig,
    ConfigError,
    Config,
    GitHubService,
    GoogleService,
    Profile,
    SecretNaming,
    SlackWorkspace,
    config_path,
    deserialize_config,
    load_config,
    save_config,
    serialize_config,
)
from mien.secret_naming import BUILTIN_DEFAULT, BUILTIN_SLACK_TOKEN


def test_config_path_uses_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "custom.json"))
    assert config_path() == tmp_path / "custom.json"


def test_config_path_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MIEN_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_path() == tmp_path / ".config" / "mien" / "config.json"


def test_load_config_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "nope.json"))
    assert load_config() is None


def test_save_then_load_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "c.json"))
    cfg = Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={"service_prefix": "mien-"}),
        bootstrap={},
        secret_naming=SecretNaming(
            default="mien-{profile}-{service}-{kind}",
            slack_token="mien-{profile}-slack-{workspace}-token",
        ),
        profiles={
            "personal": Profile(
                name="personal",
                google=GoogleService(
                    email="me@example.com",
                    oauth_client_id="cid",
                    oauth_client_secret_ref=None,
                    refresh_token_ref="mien-personal-google-refresh",
                    adc_ref=None,
                    gcloud_config_name="personal",
                    default_project=None,
                ),
                github=GitHubService(username="me", host="github.com", token_ref="mien-personal-github-token"),
                slack=[SlackWorkspace(workspace="team-a", user_token_ref="mien-personal-slack-team-a-token")],
            )
        },
    )
    save_config(cfg)
    loaded = load_config()
    assert loaded == cfg


def test_owns_remotes_survives_a_roundtrip_and_rejects_a_bare_string(monkeypatch, tmp_path):
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "c.json"))
    cfg = Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"personal": Profile(
            name="personal",
            owns_remotes=["github.com/me/*", "github.com/me-labs/*"],
        )},
    )
    save_config(cfg)
    assert load_config() == cfg
    assert load_config().profiles["personal"].owns_remotes == [
        "github.com/me/*", "github.com/me-labs/*"]

    # A bare string must be rejected, not exploded into one glob per character
    # (a "*" element would claim every remote).
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "$schema_version": 1,
        "secrets_backend": {"type": "macos_keychain"},
        "profiles": {"x": {"owns_remotes": "github.com/x/*"}},
    }))
    with pytest.raises(ValueError, match="owns_remotes"):
        load_config()


def test_git_identity_fields_survive_a_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "c.json"))
    cfg = Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"work": Profile(name="work", git_email="me@x.example")},
    )
    save_config(cfg)
    loaded = load_config()
    assert loaded == cfg
    assert loaded.profiles["work"].git_email == "me@x.example"
    # Absent by default.
    assert Profile(name="p").git_email is None


def _raw(profiles: dict) -> dict:
    """A minimal loadable config carrying just the profiles under test."""
    return {"$schema_version": 1, "secrets_backend": {"type": "keyring"},
            "bootstrap": {}, "secret_naming": {}, "profiles": profiles}


# A complete google block: every one of the seven keys is required when the block
# is present, so tests that vary one key start from this.
_GOOGLE = {
    "email": "me@x.example", "oauth_client_id": "cid",
    "oauth_client_secret_ref": "s", "refresh_token_ref": "r", "adc_ref": None,
    "gcloud_config_name": "work", "default_project": None,
}


def test_retired_fields_in_an_older_config_are_ignored():
    raw = {
        "$schema_version": 1,
        "secrets_backend": {"type": "macos_keychain"},
        "bootstrap": {},
        "secret_naming": {},
        "profiles": {
            "work": {
                "google": {
                    "email": "me@x.example",
                    "oauth_client_id": "cid",
                    "oauth_client_secret_ref": None,
                    "refresh_token_ref": "r",
                    "adc_ref": None,
                    "gcloud_config_name": "work",
                    "default_project": None,
                    "gcloud_login_required": False,
                },
                "slack": [{"workspace": "team-a", "team_id": None, "user_token_ref": "r"}],
                "git_name": "Me",
            }
        },
    }
    prof = deserialize_config(raw).profiles["work"]
    assert prof.google.email == "me@x.example"
    assert prof.slack == [SlackWorkspace(workspace="team-a", user_token_ref="r")]


@pytest.mark.parametrize("service, block", [
    ("aws", {"profile_name": "work"}),          # meant `profile`
    ("oci", {"profil": "WORK"}),                # meant `profile`
    ("github", {"username": "octo", "host": "github.com",
                 "ssh_keypath": "/k"}),   # meant `ssh_key_path`
])
def test_a_misspelled_service_key_still_fails_loudly(service, block):
    """Tolerating retired keys must not become tolerating typos.

    A dropped `profile` key leaves the service unconfigured, so mien exports no
    `AWS_PROFILE`/`OCI_CLI_PROFILE` and the tool falls back to its own default
    account — the command then succeeds as somebody else. Wrong identity is the
    failure this project exists to prevent, so it has to be loud.
    """
    raw = {
        "$schema_version": 1,
        "secrets_backend": {"type": "macos_keychain"},
        "bootstrap": {}, "secret_naming": {},
        "profiles": {"work": {service: block}},
    }
    with pytest.raises(ConfigError, match="Valid keys are"):
        deserialize_config(raw)


def test_a_retired_key_holding_a_meaningful_value_is_reported():
    """`gcloud_login_required: true` once suppressed the ADC export.

    Dropping it silently would start exporting GOOGLE_APPLICATION_CREDENTIALS
    for a profile that had asked mien not to, changing behaviour without saying
    so. Only the no-op value (False) is tolerated.
    """
    raw = {
        "$schema_version": 1,
        "secrets_backend": {"type": "macos_keychain"},
        "bootstrap": {}, "secret_naming": {},
        "profiles": {"work": {"google": {
            "email": "me@x.example", "oauth_client_id": "cid",
            "oauth_client_secret_ref": "s", "refresh_token_ref": "r",
            "adc_ref": None, "gcloud_config_name": "work",
            "default_project": None, "gcloud_login_required": True,
        }}},
    }
    with pytest.raises(ValueError, match="no longer supported") as exc:
        deserialize_config(raw)
    msg = str(exc.value)
    # The remedy must not be "remove it, or you meant something else": both of
    # those *are* the silent behaviour change this raise exists to prevent. The
    # reader of `gcloud_login_required` is gone, so deleting the key starts the
    # ADC export for the one profile that asked for no ADC, and there is no other
    # setting to have meant. So the message has to name the consequence and the
    # way to keep the old behaviour.
    assert "GOOGLE_APPLICATION_CREDENTIALS" in msg
    assert "not a no-op" in msg
    assert "oauth_client_secret_ref' to null" in msg


def _gcloud_login_required_remedy() -> str:
    raw = {
        "$schema_version": 1,
        "secrets_backend": {"type": "keyring"},
        "profiles": {"work": {"google": dict(_GOOGLE, gcloud_login_required=True)}},
    }
    with pytest.raises(ConfigError) as exc:
        deserialize_config(raw)
    return str(exc.value)


def test_the_remedy_names_every_command_that_stops_working():
    """The list of what nulling the ref costs is presented as exhaustive.

    `mien logout --service google` guards its delete with
    `if prof.google.oauth_client_secret_ref:`, so after following this remedy
    logout stops deleting the stored OAuth client secret — and drops the google
    block that recorded its name — leaving it in the backend forever. A remedy
    that lists consequences has to list that one.
    """
    msg = _gcloud_login_required_remedy()
    assert "mien token google" in msg
    assert "mien logout --service" in msg
    assert "orphan" in msg


def test_the_remedy_points_at_a_command_that_exists():
    """`mien doctor` takes only `--gc`; the live google probe is `mien whoami --live`."""
    msg = _gcloud_login_required_remedy()
    assert "doctor --live" not in msg.replace("\n", " ")
    assert "whoami --live" in msg.replace("\n", " ")


def test_a_retired_slack_key_holding_a_value_names_what_removing_it_costs():
    """`team_id` was only ever written as null, so a value here is hand-typed.

    Unlike `gcloud_login_required` nothing ever read it, so the message may say
    "deleting it changes nothing" — but it still has to say that, rather than
    leaving the reader to guess whether a delete is safe.
    """
    raw = {
        "$schema_version": 1,
        "secrets_backend": {"type": "keyring"},
        "profiles": {"work": {"slack": [
            {"workspace": "team-a", "user_token_ref": "r", "team_id": "T0001"}]}},
    }
    with pytest.raises(ConfigError, match="no longer supported") as exc:
        deserialize_config(raw)
    assert "deleting the key changes nothing" in str(exc.value)


@pytest.mark.parametrize("profiles, expect", [
    # A typo inside a service block has to name the profile like every other
    # check does, not just the dataclass: with several profiles carrying an `aws`
    # block, `AWSService: ...` leaves the reader to guess which one to edit.
    ({"home": {"aws": {"region": "eu-west-1"}}, "work": {"aws": {"profil": "w"}}},
     "profile 'work': aws: unknown key 'profil'."),
    ({"work": {"github": {"username": "octo", "host": "github.com",
                          "ssh_keypath": "/k"}}},
     "profile 'work': github: unknown key 'ssh_keypath'."),
    # A list entry has to name its index too, or nothing says which workspace.
    ({"work": {"slack": [{"workspace": "a", "user_token_ref": "r"},
                         {"workspac": "b", "user_token_ref": "r"}]}},
     "profile 'work': slack[1]: unknown key 'workspac'."),
])
def test_a_service_block_key_error_names_the_profile(profiles, expect):
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw(profiles))
    assert expect in str(exc.value)
    assert "Valid keys are" in str(exc.value)


@pytest.mark.parametrize("profiles, expect", [
    ({"home": {"google": dict(_GOOGLE)},
      "work": {"google": dict(_GOOGLE, gcloud_login_required=True)}},
     "profile 'work': google: 'gcloud_login_required'=True"),
    ({"work": {"slack": [{"workspace": "a", "user_token_ref": "r"},
                         {"workspace": "b", "user_token_ref": "r",
                          "team_id": "T0001"}]}},
     "profile 'work': slack[1]: 'team_id'='T0001'"),
])
def test_a_retired_service_key_error_names_the_profile(profiles, expect):
    """The retired-key report has the same job as the unknown-key one.

    It tells the operator to go and edit a block, so it has to say which block —
    the remedy for `gcloud_login_required` is a multi-step edit, and following it
    on the wrong profile changes the wrong identity.
    """
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw(profiles))
    assert expect in str(exc.value)
    assert "no longer supported" in str(exc.value)


def test_an_empty_service_block_is_validated_not_dropped():
    """`"google": {}` is a truncated block, not "this profile has no google".

    Reading it as falsy silently left the profile with no google at all: the
    exact silent loss of an identity this parser exists to stop. It must be
    validated like any other block, so its missing required fields are reported.
    """
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw({"work": {"google": {}}}))
    msg = str(exc.value)
    assert msg.startswith("profile 'work': google: missing required keys ")
    assert "'email'" in msg and "'refresh_token_ref'" in msg
    assert "Valid keys are" in msg


def test_a_partial_service_block_names_the_one_field_it_is_missing():
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw({"work": {"github": {"username": "octo"}}}))
    assert "profile 'work': github: missing required key 'host'." in str(exc.value)


def test_an_empty_block_parses_when_every_field_is_optional():
    """`aws` and `oci` are legitimately constructible from `{}`.

    No "must be non-empty" rule is invented here: `{}` just goes through the
    normal checks, and for these two there is nothing to complain about.
    """
    prof = deserialize_config(_raw({"work": {"aws": {}, "oci": {}}})).profiles["work"]
    assert prof.aws is not None and prof.aws.region is None
    assert prof.oci is not None and prof.oci.profile is None


def test_a_null_service_block_still_means_no_such_service():
    prof = deserialize_config(_raw({"work": {"github": None}})).profiles["work"]
    assert prof.github is None


def test_an_absent_service_block_still_means_no_such_service():
    prof = deserialize_config(_raw({"work": {}})).profiles["work"]
    assert (prof.github, prof.google, prof.aws, prof.oci) == (None, None, None, None)


@pytest.mark.parametrize("profiles, expect", [
    # Falsy non-objects used to slip past the `if not value` short-circuit and be
    # read as "no such service" instead of keeping their shape error.
    ({"work": {"github": False}}, "profile 'work': github must be a JSON object"),
    ({"work": {"aws": []}}, "profile 'work': aws must be a JSON object"),
    ({"work": {"notion": 0}}, "profile 'work': notion must be a JSON object"),
    ({"work": {"atlassian": ""}}, "profile 'work': atlassian must be a JSON object"),
    ({"work": {"slack": [None]}}, "profile 'work': slack[0] must be a JSON object"),
    ({"work": {"slack": [{}]}}, "profile 'work': slack[0]: missing required keys"),
])
def test_a_present_but_unusable_service_block_is_reported(profiles, expect):
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw(profiles))
    assert expect in str(exc.value)


def test_the_same_slack_workspace_twice_is_refused():
    """`build_env` keys its token map by workspace name, so a second entry for
    one workspace silently replaces the first one's token — a credential lost
    without a word. Refused at parse instead."""
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw({"work": {"slack": [
            {"workspace": "team-a", "user_token_ref": "r1"},
            {"workspace": "team-a", "user_token_ref": "r2"},
        ]}}))
    assert "profile 'work': slack lists the same workspace twice: team-a" in str(exc.value)


def test_save_creates_parent_dir_and_chmods_600(monkeypatch, tmp_path):
    target = tmp_path / "deep" / "nested" / "config.json"
    monkeypatch.setenv("MIEN_CONFIG", str(target))
    cfg = Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={},
        secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={},
    )
    save_config(cfg)
    assert target.exists()
    assert (target.stat().st_mode & 0o777) == 0o600


def test_load_rejects_unknown_schema_version(monkeypatch, tmp_path):
    p = tmp_path / "c.json"
    monkeypatch.setenv("MIEN_CONFIG", str(p))
    p.write_text(json.dumps({"$schema_version": 99, "secrets_backend": {"type": "macos_keychain"}, "profiles": {}}))
    with pytest.raises(ValueError, match="schema_version"):
        load_config()


def _cfg() -> Config:
    return Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="gcp_secret_manager", options={"project": "p1"}),
        bootstrap={"gcp_account": "me@x.com"},
        secret_naming=SecretNaming(
            default="mien-{profile}-{service}-{kind}",
            slack_token="mien-{profile}-slack-{workspace}-token",
        ),
        profiles={
            "work": Profile(
                name="work",
                github=GitHubService(username="u", host="github.com", token_ref="ref://gh"),
            )
        },
    )


def test_serialize_then_deserialize_roundtrips():
    cfg = _cfg()
    restored = deserialize_config(serialize_config(cfg))
    assert restored == cfg


def test_deserialize_accepts_dict_and_str():
    cfg = _cfg()
    as_str = serialize_config(cfg)
    from_str = deserialize_config(as_str)
    from_dict = deserialize_config(json.loads(as_str))
    assert from_str == from_dict


def test_deserialize_rejects_bad_schema_version():
    bad = json.dumps({"$schema_version": 99, "secrets_backend": {"type": "macos_keychain"},
                       "bootstrap": {}, "secret_naming": {}, "profiles": {}})
    with pytest.raises(ValueError, match="schema_version"):
        deserialize_config(bad)


def test_project_env_round_trips():
    from mien.config import ProjectEnvScope
    cfg = Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={},
        secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"work": Profile(name="work", project_env=[
            ProjectEnvScope(match="*/work/arinyaho", env={"AWS_PROFILE": "work", "WORK_ROOT": "$HOME/work/arinyaho"}),
            ProjectEnvScope(match="*/arinyaho-ai*", env={"PYTHONPATH": "$HOME/x/src"}),
        ])},
    )
    back = deserialize_config(serialize_config(cfg))
    scopes = back.profiles["work"].project_env
    assert [s.match for s in scopes] == ["*/work/arinyaho", "*/arinyaho-ai*"]
    assert scopes[0].env["AWS_PROFILE"] == "work"


def test_config_without_project_env_defaults_empty():
    raw = {"$schema_version": 1, "secrets_backend": {"type": "macos_keychain"},
           "bootstrap": {}, "secret_naming": {},
           "profiles": {"p": {"github": None}}}
    assert deserialize_config(raw).profiles["p"].project_env == []


def _raw_with_default_for(value) -> dict:
    return {"$schema_version": 1, "secrets_backend": {"type": "macos_keychain"},
            "bootstrap": {}, "secret_naming": {},
            "profiles": {"work": {"default_for": value}}}


def test_default_for_scalar_string_is_rejected():
    # A bare string must not be char-split into globs: the resulting "*" would
    # claim every directory on the machine and misroute credentials.
    with pytest.raises(ValueError) as exc:
        deserialize_config(_raw_with_default_for("*/Projects/acme"))
    assert "profile 'work': default_for must be a list of directory glob strings" in str(exc.value)
    assert "got str" in str(exc.value)


def test_default_for_non_string_entry_is_rejected():
    with pytest.raises(ValueError) as exc:
        deserialize_config(_raw_with_default_for([123]))
    assert "profile 'work': default_for entries must be directory glob strings" in str(exc.value)
    assert "got int" in str(exc.value)


def test_default_for_list_of_strings_is_accepted():
    cfg = deserialize_config(_raw_with_default_for(["*/Projects/acme", "*/work/*"]))
    assert cfg.profiles["work"].default_for == ["*/Projects/acme", "*/work/*"]


def test_default_for_missing_defaults_empty():
    raw = _raw_with_default_for(None)
    raw["profiles"]["work"].pop("default_for")
    assert deserialize_config(raw).profiles["work"].default_for == []


@pytest.mark.parametrize("raw, expect", [
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},',  # trailing comma
     "invalid config JSON"),
    ('{"$schema_version": 2, "secrets_backend": {"type": "keyring"}}',
     "Unsupported schema_version"),
    ('{"$schema_version": 1, "secrets_backend": {"project": "p"}}',
     "secrets_backend.type is missing"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"w": {"default_for": "a-string"}}}',
     "must be a list"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"w": {"defualt_for": ["/x"]}}}',
     "unknown key"),
    # Wrong-typed blocks: every one of these is read as a dict/list further down.
    ('["not", "a", "config"]', "config must be a JSON object"),
    ('{"$schema_version": 1, "secrets_backend": "keyring"}',
     "secrets_backend must be a JSON object"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "bootstrap": "me@example.com"}',
     "bootstrap must be a JSON object"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "secret_naming": "mien-{profile}"}',
     "secret_naming must be a JSON object"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": ["work"]}',
     "profiles must be a JSON object"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": null}}',
     "profile 'work' must be a JSON object"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"github": "octocat"}}}',
     "profile 'work': github must be a JSON object"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"aws": ["eu-west-1"]}}}',
     "profile 'work': aws must be a JSON object"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"slack": "team-a"}}}',
     "profile 'work': slack must be a list"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"slack": ["team-a"]}}}',
     r"profile 'work': slack\[0\] must be a JSON object"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env": {"match": "*"}}}}',
     "profile 'work': project_env must be a list"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env": ["*/acme/*"]}}}',
     r"profile 'work': project_env\[0\] must be a JSON object"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env": [{"match": "*", "env": "A=1"}]}}}',
     "profile 'work': project_env '\\*' env must be a JSON object"),
    # `match` is the one value in a project_env entry that reached the scope
    # unchecked while the key set and `env`'s shape were both checked. `mien env
    # sync` then died on a bare TypeError out of `_VAR_RE.finditer` (unexpandable
    # -scope warning) or an AttributeError out of `match_base` — neither of which
    # `env_sync_cmd` catches, so the operator got a traceback instead of the
    # "here is what is wrong with your config" the parser owes them.
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env": [{"match": null}]}}}',
     "'match' must be a directory glob string.*got NoneType"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env": [{"match": ["*/a", "*/b"]}]}}}',
     "'match' must be a directory glob string.*got list"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env": [{"match": 7}]}}}',
     "'match' must be a directory glob string.*got int"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env": [{"match": {"dir": "*/work"}}]}}}',
     "'match' must be a directory glob string.*got dict"),
    # An empty glob is not "an empty scope": `match_base("")` is `""`, so `mien
    # env sync` emits `case "$PWD/" in /*)`, which fires in every directory and
    # exports the scope's env everywhere — the same silent widening as the `"*"`
    # element `_glob_list_from_raw` rejects, arriving by a quieter route.
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env": [{"match": "",'
     ' "env": {"AWS_PROFILE": "work"}}]}}}',
     "has an empty 'match' glob"),
    # A mistyped backend option used to be swept into `options` unchecked and
    # escape much later as a bare `KeyError: 'project'` out of `load_backend` —
    # from `mien doctor`, the command mien's own markers tell you to run.
    ('{"$schema_version": 1,'
     ' "secrets_backend": {"type": "gcp_secret_manager", "projct": "x"}}',
     "unknown option 'projct'"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "gcp_secret_manager"}}',
     "requires option 'project'"),
    ('{"$schema_version": 1,'
     ' "secrets_backend": {"type": "keyring", "servce_prefix": "mien-"}}',
     "unknown option 'servce_prefix'"),
    ('{"$schema_version": 1,'
     ' "secrets_backend": {"type": "macos_keychain", "project": "p"}}',
     "unknown option 'project'"),
    # `type` was the one leaf popped out of its block and handed to
    # `BackendConfig` unchecked. An unhashable one died in
    # `ensure_known_backend_options`'s `BACKEND_OPTIONS.get(...)` as a bare
    # `TypeError` out of `deserialize_config` — which every surface calls, so
    # `mien guard` exited 0 with both streams empty and `mien list` printed a
    # traceback. `is_cloud_backend`'s set lookup is the same crash one command
    # further on. Hashable-but-wrong types are rejected here too: `null` and `5`
    # are not backend names, so "unknown secrets backend 5" would send the reader
    # hunting for a backend called 5 rather than saying the value is the wrong
    # shape, and deferring them would make the message depend on hashability.
    ('{"$schema_version": 1, "secrets_backend": {"type": ["keyring"]}}',
     r"secrets_backend: 'type' must be a string, got list: \['keyring'\]"),
    ('{"$schema_version": 1, "secrets_backend": {"type": {"name": "keyring"}}}',
     r"secrets_backend: 'type' must be a string, got dict: \{'name': 'keyring'\}"),
    ('{"$schema_version": 1, "secrets_backend": {"type": null}}',
     "secrets_backend: 'type' must be a string, got NoneType: None"),
    ('{"$schema_version": 1, "secrets_backend": {"type": 5}}',
     "secrets_backend: 'type' must be a string, got int: 5"),
    ('{"$schema_version": 1, "secrets_backend": {"type": true}}',
     "secrets_backend: 'type' must be a string, got bool: True"),
    # `envs` builds a scope that matches and exports nothing: no AWS_PROFILE, the
    # tool falls back to its own default account, the command runs as somebody
    # else. Same class of failure as a mistyped profile key, same treatment.
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env":'
     ' [{"match": "*/work", "envs": {"AWS_PROFILE": "work"}}]}}}',
     "unknown key 'envs'"),
    # `profles` used to parse into a config with zero profiles: every identity
    # gone, `mien which` resolving to nothing, and no error anywhere.
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profles": {"work": {}}}',
     "config: unknown key 'profles'"),
    # `bootstrp` left the bootstrap account empty, so the GCP backend's
    # "you are logged in as someone else" diagnostic named nobody.
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "bootstrp": {"gcp_account": "me@example.com"}}',
     "config: unknown key 'bootstrp'"),
    # The sharpest one: a typo'd template silently reverts to the built-in name,
    # so `mien login` writes secrets somewhere other than the config says.
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "secret_naming": {"defalt": "acme-{profile}-{service}-{kind}"}}',
     "secret_naming: unknown key 'defalt'"),
    # A *falsy* wrong-typed block is the same defect as a truthy one, and used to
    # be swept under `or {}` before the shape check ever ran: `[]` read as "empty
    # block", so `"profiles": []` meant zero identities, `"secret_naming": []`
    # meant the built-in templates back in force (secrets written somewhere other
    # than the config says), and `"bootstrap": []` meant no bootstrap account.
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": []}',
     r"profiles must be a JSON object, got list: \[\]"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "secret_naming": []}',
     r"secret_naming must be a JSON object, got list: \[\]"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "bootstrap": []}',
     r"bootstrap must be a JSON object, got list: \[\]"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env": [{"match": "*", "env": []}]}}}',
     r"profile 'work': project_env '\*' env must be a JSON object, got list"),
    # An `env` map had its SHAPE checked and its CONTENTS trusted, while the
    # sibling `match` two lines above was checked value and all. A non-string
    # value reaches `ambient._emit_value` and dies on `.replace` as a bare
    # AttributeError, which `env_sync_cmd` does not catch and `MienGroup` does
    # not translate: `mien env sync` exits 1 with a raw traceback instead of an
    # actionable error. An unquoted number is the likeliest hand-edit of the four.
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env":'
     ' [{"match": "*/work", "env": {"PORT": 8080}}]}}}',
     r"project_env '\*/work' env: 'PORT' must be a string, got int: 8080"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env":'
     ' [{"match": "*/work", "env": {"AWS_PROFILE": null}}]}}}',
     r"project_env '\*/work' env: 'AWS_PROFILE' must be a string, got NoneType"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env":'
     ' [{"match": "*/work", "env": {"PATH_PARTS": ["/a", "/b"]}}]}}}',
     r"project_env '\*/work' env: 'PATH_PARTS' must be a string, got list"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env":'
     ' [{"match": "*/work", "env": {"AWS": {"profile": "work"}}}]}}}',
     r"project_env '\*/work' env: 'AWS' must be a string, got dict"),
    # Keys are checked for the same reason, one gate further out: `zsh -n`
    # *parses* `export 2FA="x"` — "not an identifier" is a run-time error — so
    # `assert_parses` passes, the file is written, and sourcing it abandons
    # every remaining export in `ambient.zsh`, other profiles' scopes included.
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env":'
     ' [{"match": "*/work", "env": {"2FA": "on"}}]}}}',
     r"project_env '\*/work' env: '2FA' is not a usable environment variable name"),
    # Quieter still: `export AWS PROFILE="work"` is valid zsh that exports `AWS`
    # empty and `PROFILE=work`, so the variable the operator wrote is never set
    # and nothing anywhere says so.
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": {"work": {"project_env":'
     ' [{"match": "*/work", "env": {"AWS PROFILE": "work"}}]}}}',
     r"project_env '\*/work' env: 'AWS PROFILE' is not a usable environment "
     r"variable name"),
    # Falsy scalars too — `0`/`false`/`""` are all "present and wrong-typed".
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "profiles": 0}',
     "profiles must be a JSON object, got int"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "secret_naming": false}',
     "secret_naming must be a JSON object, got bool"),
    ('{"$schema_version": 1, "secrets_backend": {"type": "keyring"},'
     ' "bootstrap": ""}',
     "bootstrap must be a JSON object, got str"),
])
def test_every_way_a_config_breaks_is_a_configerror(raw, expect):
    """One type for every parse failure, so the CLI can report them all.

    ConfigError is what `mien guard` keys off to announce that it has stopped
    enforcing. A parse failure that escapes as a bare ValueError/KeyError makes
    the guard exit 0 in silence — the mis-authored commit this tool prevents.
    """
    with pytest.raises(ConfigError, match=expect):
        deserialize_config(raw)


def test_a_wrong_typed_secrets_backend_is_a_shape_error_not_a_missing_type():
    """`[]` is a mis-typed block, not a block that forgot its 'type'.

    Coercing it to `{}` before the shape check made the parser report the one
    key the operator never wrote, sending them to add `"type"` to a value that
    cannot hold keys at all. The truthy `"secrets_backend": "keyring"` already
    said "must be a JSON object"; the falsy one has to say the same thing.
    """
    with pytest.raises(ConfigError) as exc:
        deserialize_config({"$schema_version": 1, "secrets_backend": []})
    assert "secrets_backend must be a JSON object" in str(exc.value)
    assert "type is missing" not in str(exc.value)


def test_an_absent_or_empty_block_still_means_empty():
    """The fix must not invent a "must be non-empty" rule.

    Absence and `{}` are both "nothing configured here" and always were: zero
    profiles, no bootstrap account, the built-in secret-name templates. Only a
    *present and wrong-typed* block changed behaviour.
    """
    absent = {"$schema_version": 1, "secrets_backend": {"type": "keyring"}}
    empty = {"$schema_version": 1, "secrets_backend": {"type": "keyring"},
             "profiles": {}, "bootstrap": {}, "secret_naming": {}}
    for raw in (absent, empty):
        cfg = deserialize_config(raw)
        assert cfg.profiles == {}
        assert cfg.bootstrap == {}
        assert cfg.secret_naming.default == "mien-{profile}-{service}-{kind}"
        assert cfg.secret_naming.slack_token == "mien-{profile}-slack-{workspace}-token"


def test_an_absent_or_empty_project_env_env_still_means_no_exports():
    raw = {"$schema_version": 1, "secrets_backend": {"type": "keyring"},
           "profiles": {"work": {"project_env": [
               {"match": "*/absent"},
               {"match": "*/empty", "env": {}},
           ]}}}
    scopes = deserialize_config(raw).profiles["work"].project_env
    assert [(s.match, s.env) for s in scopes] == [("*/absent", {}), ("*/empty", {})]


def test_string_env_values_including_empty_and_references_still_parse():
    """Checking `env`'s values must not narrow what a working config may say.

    Both of the interesting strings are legitimate against `mien.ambient`: an
    empty one emits `export X=""`, which is how you export a deliberately empty
    variable, and a `$VAR` one is the whole point of `_emit_value`'s double
    quoting — "the value IS evaluated by zsh (that is how references work)".
    Only the TYPE is being constrained here, never the content.
    """
    raw = {"$schema_version": 1, "secrets_backend": {"type": "keyring"},
           "profiles": {"work": {"project_env": [
               {"match": "*/work", "env": {
                   "AWS_PROFILE": "work",
                   "GIT_PAGER": "",
                   "WORK_ROOT": "$HOME/work",
                   "PATH": "${HOME}/bin:$PATH",
                   "_LEADING_UNDERSCORE": "1",
                   "PY3_PATH": "$HOME/x/src",
               }},
           ]}}}
    scope = deserialize_config(raw).profiles["work"].project_env[0]
    assert scope.env == {
        "AWS_PROFILE": "work",
        "GIT_PAGER": "",
        "WORK_ROOT": "$HOME/work",
        "PATH": "${HOME}/bin:$PATH",
        "_LEADING_UNDERSCORE": "1",
        "PY3_PATH": "$HOME/x/src",
    }
    # And what those values become is still a script zsh accepts.
    from mien.ambient import assert_parses, render_ambient
    assert_parses(render_ambient(deserialize_config(raw).profiles))


def test_an_explicitly_null_block_is_absence_not_a_wrong_type():
    """`null` is how "not configured" is spelled elsewhere in this parser.

    `"github": null` already means "this profile has no github", so a null outer
    block has to keep meaning absence too — the shape check applies to a value
    that is *there*.
    """
    raw = {"$schema_version": 1, "secrets_backend": {"type": "keyring"},
           "profiles": None, "bootstrap": None, "secret_naming": None}
    cfg = deserialize_config(raw)
    assert cfg.profiles == {}
    assert cfg.bootstrap == {}
    assert cfg.secret_naming.default == "mien-{profile}-{service}-{kind}"


@pytest.mark.parametrize("backend", [
    {"type": "gcp_secret_manager", "project": "p1"},
    {"type": "macos_keychain"},
    {"type": "macos_keychain", "service_prefix": "acme-"},
    {"type": "keyring"},
    {"type": "keyring", "service_prefix": "acme-"},
])
def test_valid_backend_options_are_still_accepted(backend):
    """Option checking must not reject a config that works today."""
    raw = {"$schema_version": 1, "secrets_backend": backend, "profiles": {}}
    cfg = deserialize_config(raw)
    assert cfg.secrets_backend.type == backend["type"]
    assert cfg.secrets_backend.options == {
        k: v for k, v in backend.items() if k != "type"}


def test_an_unrecognized_backend_type_keeps_its_own_error():
    """Option checking must not steal the migration story from a retired type.

    `oci_vault` has secrets stranded in it; `ensure_known_backend` is what says
    so. Parsing has to get out of the way and let the caller reach that message.
    """
    raw = {"$schema_version": 1,
           "secrets_backend": {"type": "oci_vault", "vault_ocid": "ocid1..."},
           "profiles": {}}
    cfg = deserialize_config(raw)
    assert cfg.secrets_backend.options == {"vault_ocid": "ocid1..."}


def test_a_backend_type_is_checked_for_type_at_parse_time_and_for_value_later():
    """The split the `type` check must keep: wrong shape here, wrong name there.

    A `type` that is not a string cannot name any backend, so it is a config
    error like every other mistyped leaf and is reported while parsing. A `type`
    that *is* a string but names no backend mien has is `ensure_known_backend`'s
    to answer, deliberately deferred to the first command that reaches for the
    backend — so a config-only command (`mien which`, `mien list`) still works.
    Collapsing either direction breaks something: rejecting unknown strings at
    parse time takes the migration story away from a retired backend and makes
    every config-only command fail, while deferring non-strings puts an
    unhashable value into `BACKEND_OPTIONS.get` and `is_cloud_backend`'s set
    lookup, where it escapes as a `TypeError` the fail-open surfaces ignore.
    """
    from mien.backends import UnknownBackendType, ensure_known_backend

    with pytest.raises(ConfigError, match="'type' must be a string"):
        deserialize_config({"$schema_version": 1,
                            "secrets_backend": {"type": ["keyring"]}})

    cfg = deserialize_config({"$schema_version": 1,
                              "secrets_backend": {"type": "keychain"}})
    assert cfg.secrets_backend.type == "keychain"
    with pytest.raises(UnknownBackendType, match="unknown secrets backend"):
        ensure_known_backend(cfg.secrets_backend)


@pytest.mark.parametrize("version_key", ["$schema_version", "schema_version"])
def test_both_schema_version_spellings_still_parse(version_key):
    """The unknown-key check must not reject the unprefixed spelling.

    `deserialize_config` has always read either one, so a config written by hand
    (or by an older mien) with the unprefixed key loads today and has to keep
    loading.
    """
    raw = {version_key: 1, "secrets_backend": {"type": "keyring"},
           "bootstrap": {}, "secret_naming": {},
           "profiles": {"work": {}}}
    cfg = deserialize_config(raw)
    assert cfg.schema_version == 1
    assert list(cfg.profiles) == ["work"]


def test_the_top_level_error_advertises_only_the_canonical_version_spelling():
    """Accepted is not the same as recommended: the alias is not suggested."""
    with pytest.raises(ConfigError) as exc:
        deserialize_config({"$schema_version": 1,
                            "secrets_backend": {"type": "keyring"},
                            "profles": {}})
    valid = str(exc.value).split("Valid keys are: ")[1].rstrip(".").split(", ")
    assert valid == ["$schema_version", "bootstrap", "profiles", "secret_naming",
                     "secrets_backend"]


def test_every_top_level_key_mien_writes_is_accepted():
    """The serializer's own output must survive the check it now runs."""
    cfg = Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={"gcp_account": "me@example.com"},
        secret_naming=SecretNaming(default="acme.{kind}.{service}.{profile}",
                                   slack_token="acme.{workspace}.{profile}"),
        profiles={"work": Profile(name="work")},
    )
    back = deserialize_config(serialize_config(cfg))
    assert back.bootstrap == {"gcp_account": "me@example.com"}
    assert back.secret_naming.default == "acme.{kind}.{service}.{profile}"


def test_secret_naming_with_only_known_keys_is_accepted():
    raw = {"$schema_version": 1, "secrets_backend": {"type": "keyring"},
           "secret_naming": {"default": "acme-{profile}-{service}-{kind}"},
           "profiles": {}}
    cfg = deserialize_config(raw)
    assert cfg.secret_naming.default == "acme-{profile}-{service}-{kind}"
    # The half not given still falls back to the built-in template.
    assert cfg.secret_naming.slack_token == "mien-{profile}-slack-{workspace}-token"


def test_project_env_entry_with_only_known_keys_is_accepted():
    raw = {"$schema_version": 1, "secrets_backend": {"type": "keyring"},
           "profiles": {"work": {"project_env": [
               {"match": "*/work", "env": {"AWS_PROFILE": "work"}},
               {"match": "*/solo"},
           ]}}}
    scopes = deserialize_config(raw).profiles["work"].project_env
    assert [(s.match, s.env) for s in scopes] == [
        ("*/work", {"AWS_PROFILE": "work"}), ("*/solo", {})]


def test_a_string_match_still_parses_in_every_form_a_scope_is_written():
    """The `match` check must not narrow what a working scope may say.

    A glob, a `~` path and a `$VAR` reference are all left to `mien.resolve` and
    `mien.ambient` to expand; parsing only insists that the value is a non-empty
    string.
    """
    raw = {"$schema_version": 1, "secrets_backend": {"type": "keyring"},
           "profiles": {"work": {"project_env": [
               {"match": "*/work", "env": {"AWS_PROFILE": "work"}},
               {"match": "~/Projects/acme/"},
               {"match": "$WORK_ROOT/*"},
           ]}}}
    scopes = deserialize_config(raw).profiles["work"].project_env
    assert [s.match for s in scopes] == [
        "*/work", "~/Projects/acme/", "$WORK_ROOT/*"]
    assert scopes[0].env == {"AWS_PROFILE": "work"}


def test_a_retired_profile_key_still_loads():
    """`git_name` was removed; a config written before that must still open."""
    raw = {
        "$schema_version": 1,
        "secrets_backend": {"type": "keyring"},
        "profiles": {"w": {"git_name": "Me", "owns_remotes": ["github.com/me/*"]}},
    }
    prof = deserialize_config(raw).profiles["w"]
    assert prof.owns_remotes == ["github.com/me/*"]


@pytest.mark.parametrize("profiles, expect", [
    # The status line reads git_email to cross-check the commit author; an int
    # reached `!=` fine and `.split("@")`/format as an AttributeError, into the
    # bare `except Exception: return` that every fail-open surface wraps itself
    # in. The line went blank and `mien guard` exited 0 on stdout and stderr
    # both empty — the wrong-identity commit waved through in total silence.
    ({"work": {"git_email": 123}},
     "profile 'work': 'git_email' must be a string or null, got int: 123"),
    ({"work": {"github": {"username": 42, "host": "github.com"}}},
     "profile 'work': github: 'username' must be a string, got int: 42"),
    ({"work": {"github": {"username": "u", "host": "github.com", "token_ref": 7}}},
     "profile 'work': github: 'token_ref' must be a string or null, got int: 7"),
    ({"work": {"google": dict(_GOOGLE, email=1)}},
     "profile 'work': google: 'email' must be a string, got int: 1"),
    ({"work": {"google": dict(_GOOGLE, default_project=["p"])}},
     "profile 'work': google: 'default_project' must be a string or null, got list"),
    # A list entry names its index, like every other message from a slack block.
    ({"work": {"slack": [{"workspace": "a", "user_token_ref": "r"},
                         {"workspace": {"name": "b"}, "user_token_ref": "r"}]}},
     "profile 'work': slack[1]: 'workspace' must be a string, got dict"),
    ({"work": {"aws": {"profile": 7}}},
     "profile 'work': aws: 'profile' must be a string or null, got int: 7"),
    ({"work": {"oci": {"config_file": True}}},
     "profile 'work': oci: 'config_file' must be a string or null, got bool: True"),
    ({"work": {"atlassian": {"email": "e", "base_url": 8080, "api_token_ref": "r"}}},
     "profile 'work': atlassian: 'base_url' must be a string, got int: 8080"),
    ({"work": {"notion": {"api_token_ref": 0}}},
     "profile 'work': notion: 'api_token_ref' must be a string, got int: 0"),
])
def test_a_wrong_typed_leaf_value_is_a_configerror(profiles, expect):
    """Shape checking has to reach the leaf, not stop at the block.

    Everything above this checked that `github` is an object and `slack` a list
    of objects, then handed the field values straight to a dataclass, which takes
    whatever it is given. A wrong-typed leaf therefore parsed clean and died
    wherever the value was first used as text — as AttributeError/TypeError,
    which the fail-open surfaces do not recognize as a config failure and so
    swallow. Every one of these must arrive as a ConfigError instead, naming the
    profile, the block, the key, what was expected and what was found.
    """
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw(profiles))
    assert expect in str(exc.value)


def test_a_wrong_typed_secret_naming_template_is_a_configerror():
    """A template is `.format()`ed to decide where a secret lives.

    A non-string one dies as an AttributeError inside `mien login`/`mien token`,
    far from the config that caused it. `null` counts too: the `.get(key,
    default)` fallback only fires when the key is absent, so an explicit null
    reaches SecretNaming rather than the built-in template.
    """
    for value, found in ((5, "int"), (None, "NoneType")):
        raw = {"$schema_version": 1, "secrets_backend": {"type": "keyring"},
               "secret_naming": {"default": value}, "profiles": {}}
        with pytest.raises(ConfigError) as exc:
            deserialize_config(raw)
        assert f"secret_naming: 'default' must be a string, got {found}" in str(exc.value)


def _raw_with_naming(**naming) -> dict:
    return {"$schema_version": 1, "secrets_backend": {"type": "keyring"},
            "bootstrap": {}, "secret_naming": naming, "profiles": {"work": {}}}


@pytest.mark.parametrize("template,missing,collapses", [
    # The concrete report: `{kind}` is the only thing keeping one custom
    # variable's secret apart from the next, and github's token from its ssh key.
    ("mien-{profile}-{service}", "{kind}", "every credential of one service"),
    # Pre-existing exposure of the same kind: atlassian and notion both store a
    # kind of `api_token`, so without `{service}` they are one secret.
    ("mien-{profile}-{kind}", "{service}", "every service renders the same name"),
    # And without `{profile}` every identity shares one, which is the whole point
    # of mien.
    ("mien-{service}-{kind}", "{profile}", "every profile renders the same name"),
    ("mien-secret", "{profile}, {service}, {kind}", "every profile"),
])
def test_a_default_template_that_cannot_distinguish_is_refused(
    template, missing, collapses
):
    """A template that drops a token renders one name for two credentials.

    `mien login` then stores the second over the first with nothing said, both
    config entries point at the survivor, and `mien logout` of either deletes the
    secret the other still references. Refused at parse time, so a hand-edited
    config and a manifest pulled from a shared backend are both covered — the
    manifest goes through this same parser.
    """
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw_with_naming(default=template))
    msg = str(exc.value)
    assert f"secret_naming: 'default' template {template!r}" in msg
    assert f"does not use {missing}" in msg
    assert collapses in msg
    # What breaks, and the way out.
    assert "overwrites the first with nothing said" in msg
    assert "mien-{profile}-{service}-{kind}" in msg


@pytest.mark.parametrize("template,missing", [
    # Two workspaces on one profile, one secret: the same collapse, and
    # `slack_token` is exposed to it because it is rendered with its own tokens.
    ("mien-{profile}-slack-token", "{workspace}"),
    ("mien-slack-{workspace}-token", "{profile}"),
])
def test_a_slack_token_template_that_cannot_distinguish_is_refused(template, missing):
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw_with_naming(slack_token=template))
    msg = str(exc.value)
    assert f"secret_naming: 'slack_token' template {template!r}" in msg
    assert f"does not use {missing}" in msg
    assert "mien-{profile}-slack-{workspace}-token" in msg
    # `slack_token` is never rendered with a `{service}`: the literal "slack" is
    # the template's own, so requiring it would refuse the built-in template.
    assert "{service}" not in msg


def test_the_builtin_templates_pass_the_check():
    """The rule may not reject the config mien writes for itself."""
    from mien.secret_naming import BUILTIN_DEFAULT, BUILTIN_SLACK_TOKEN
    cfg = deserialize_config(_raw_with_naming(
        default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN))
    assert cfg.secret_naming.default == BUILTIN_DEFAULT
    # And so does the absent case, which falls back to those same templates.
    assert deserialize_config(_raw_with_naming()).secret_naming.default == BUILTIN_DEFAULT


@pytest.mark.parametrize("default,slack_token", [
    # Renamed prefix, the arrangement mien's own docs show.
    ("acme-{profile}-{service}-{kind}", "acme-{profile}-slack-{workspace}-token"),
    # Reordered, differently separated, and with extra literals: distinguishing
    # is the only requirement, not any particular shape.
    ("{kind}.{service}.{profile}.acme", "{workspace}--{profile}--slack"),
    # A token may also appear more than once.
    ("{profile}/{service}/{kind}/{profile}", "{profile}-{workspace}-{workspace}"),
])
def test_a_custom_template_that_does_distinguish_still_passes(default, slack_token):
    cfg = deserialize_config(_raw_with_naming(default=default, slack_token=slack_token))
    assert cfg.secret_naming.default == default
    assert cfg.secret_naming.slack_token == slack_token


@pytest.mark.parametrize("template", [
    # `{{kind}}` is an escaped literal: it renders the six characters `{kind}`
    # for every credential and substitutes nothing, so a substring search for
    # "{kind}" would wave through a template that collapses everything.
    "mien-{profile}-{service}-{{kind}}",
    # A field that only reaches INTO the token does not reproduce it: two kinds
    # sharing a first character still land on one name.
    "mien-{profile}-{service}-{kind[0]}",
])
def test_a_token_that_looks_present_but_substitutes_nothing_is_refused(template):
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw_with_naming(default=template))
    assert "does not use {kind}" in str(exc.value)


# The two rules the template check enforces are different mistakes with different
# fixes, so each error has to say WHICH one was made. These are the phrases the
# tests below key on; a message may carry one of them, never both.
_COLLAPSE = "as a plain replacement field"          # rule 1, token not spent
_DECORATED = "through a format spec, a conversion"  # rule 1, decoration refused
_UNRENDERABLE = "asks for a field mien does not substitute into it"  # rule 2


@pytest.mark.parametrize("template,missing,field", [
    # The report: a truncating spec. `{kind:.1s}` keeps one character, so
    # ANTHROPIC_API_KEY and AWS_THING both render `mien-work-custom-A` — the same
    # collapse as dropping `{kind}`, which the presence-only rule accepted while
    # refusing the equivalent `{kind[0]}`.
    ("mien-{profile}-{service}-{kind:.1s}", "{kind}", "{kind:.1s}"),
    # And the degenerate one: `.0s` keeps nothing, so the rendered name is
    # byte-identical to the template with `{kind}` deleted.
    ("mien-{profile}-{service}-{kind:.0s}", "{kind}", "{kind:.0s}"),
    # A conversion is not a substitution either: `!r` renders `'token'`, quotes
    # and all, into a backend secret name.
    ("mien-{profile}-{service}-{kind!r}", "{kind}", "{kind!r}"),
    # An unknown conversion never even renders; it is refused for the same
    # structural reason, before anything tries.
    ("mien-{profile}-{service}-{kind!x}", "{kind}", "{kind!x}"),
    # Not only `{kind}`: truncate the profile and `work` and `web` share every
    # secret, which is the whole of what mien is for.
    ("mien-{profile:.1}-{service}-{kind}", "{profile}", "{profile:.1}"),
    # A nested spec hides a second field inside the first. The spec is what makes
    # this rule 1: `{kind}` is not reproduced whatever `{w}` turns out to be.
    ("mien-{profile}-{service}-{kind:{w}}", "{kind}", "{kind:{w}}"),
])
def test_a_token_spent_through_a_spec_or_conversion_does_not_count(
    template, missing, field
):
    """Rule 1 is structural: a required token must appear as a bare `{name}`.

    A probe render cannot establish this. `{kind:.0s}` renders fine — it renders
    the SAME name for every credential, which is the failure — so "it rendered"
    says nothing about whether two credentials stay apart. Only the shape of the
    field does: a plain field reproduces its token verbatim and is therefore
    injective in it by construction, while a spec, a conversion, an attribute or
    an index all compute something shorter or different.
    """
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw_with_naming(default=template))
    msg = str(exc.value)
    assert f"does not use {missing} {_COLLAPSE}" in msg
    # Named the offending field, and said why a decoration is not a substitution.
    assert field in msg
    assert "does not count as spending the token" in msg
    # What breaks, and the way out.
    assert "overwrites the first with nothing said" in msg
    assert "has to appear in it as a bare `{name}`" in msg
    assert "mien-{profile}-{service}-{kind}" in msg
    # This is rule 1, not rule 2, and the message must not blur the two.
    assert _UNRENDERABLE not in msg


def test_a_decorated_field_is_refused_even_beside_a_plain_one():
    """Every field must be plain, not just one field per required token.

    `{profile}` is already spent plainly here, so nothing collapses and rule 2's
    probe would have to render `{w}` — but a spec has no legitimate use in a
    secret name (the tokens are already strings: `:s` is a no-op, a width pads
    with spaces, a precision truncates), so it is refused outright. That is what
    makes "literal text plus plain fields" the exact shape of an accepted
    template, with no third category to reason about later.
    """
    template = "mien-{profile}-{service}-{kind}-{profile:{w}}"
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw_with_naming(default=template))
    msg = str(exc.value)
    assert _DECORATED in msg
    assert "{profile:{w}}" in msg
    assert "`:s` is a no-op" in msg
    # Rule 1 again — a different mistake from rule 2, and said differently.
    assert _UNRENDERABLE not in msg
    assert _COLLAPSE not in msg


@pytest.mark.parametrize("key,template,detail", [
    # An extra token in `default`: `{workspace}` is the OTHER template's, and
    # `default` is never rendered with it. Parsed clean before; then `mien login`
    # died with `KeyError: missing token {workspace} in template`.
    ("default", "mien-{profile}-{service}-{kind}-{workspace}",
     "missing token {workspace} in template"),
    # The mirror: `slack_token` is rendered with `{profile}` and `{workspace}`
    # only, so `{kind}` there has nothing to stand for either.
    ("slack_token", "mien-{profile}-slack-{workspace}-{kind}",
     "missing token {kind} in template"),
    # Positional fields pass any check made on field NAMES — the name is `""` or
    # `"0"` — and raise from `format_map` instead.
    ("default", "mien-{profile}-{service}-{kind}-{}",
     "Format string contains positional fields"),
    ("default", "mien-{profile}-{service}-{kind}-{0}",
     "Format string contains positional fields"),
])
def test_a_field_mien_does_not_supply_is_refused_at_parse_time(key, template, detail):
    """Rule 2 is the other direction: present ⊆ supplied, checked by rendering.

    A set difference over the parser's top-level field names does not cover this:
    `{}` and `{0}` are names a difference cannot judge, and a token buried in a
    nested spec is not in the top-level walk at all. Rendering once with exactly
    the tokens mien supplies decides all of them, and — the point — turns the
    escape into a `ConfigError` at parse time, where the fail-open surfaces
    recognize it as a config failure, instead of a bare exception out of whichever
    command first rendered a name.
    """
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw_with_naming(**{key: template}))
    msg = str(exc.value)
    assert f"secret_naming: {key!r} template {template!r}" in msg
    assert _UNRENDERABLE in msg
    # Quotes the underlying failure, so the offending field is identifiable.
    assert detail in msg
    # And says what it may use instead.
    assert ("{profile}, {service}, {kind}" if key == "default"
            else "{profile}, {workspace}") in msg
    # This is rule 2, not rule 1: nothing collapsed, the template cannot render.
    assert _COLLAPSE not in msg
    assert _DECORATED not in msg


def test_literal_braces_and_repeated_plain_fields_are_still_accepted():
    """The rules bite on decorations and unknown fields, not on ordinary templates.

    `{{...}}` is escaped literal text, not a field, and a plain token may appear
    as often as you like — both were accepted before and must stay accepted, or
    the fix has narrowed what a user may write for no safety gain.
    """
    cfg = deserialize_config(_raw_with_naming(
        default="mien-{profile}-{service}-{kind}-{{literal}}-{kind}",
        slack_token="{profile}/{workspace}/{profile}"))
    assert cfg.secret_naming.default.endswith("-{kind}")
    assert cfg.secret_naming.slack_token == "{profile}/{workspace}/{profile}"


def test_a_malformed_template_is_a_configerror_not_a_bare_valueerror():
    """An unbalanced brace cannot render at all, and must still be a ConfigError.

    The distinctness check parses the template on load, so a template `str.format`
    chokes on would otherwise escape `deserialize_config` as a plain ValueError —
    which the fail-open surfaces (guard, status line, `mien unset`) do not
    recognize as a config failure, so they would go quiet instead of saying they
    have stopped working.
    """
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw_with_naming(default="mien-{profile}-{service}-{kind"))
    msg = str(exc.value)
    assert "is not a usable template" in msg
    assert "mien-{profile}-{service}-{kind}" in msg


def test_null_is_still_accepted_wherever_the_annotation_allows_it():
    """The leaf check must not turn every optional field into a required one.

    `str | None` fields are null in configs mien writes itself — a
    gcloud-login-only google has no stored refresh token, a github identity
    authenticated by ssh key alone has no `token_ref` — so rejecting null here
    would refuse configs that work today.
    """
    raw = _raw({"work": {
        "git_email": None,
        "google": dict(_GOOGLE, oauth_client_secret_ref=None, refresh_token_ref=None,
                       adc_ref=None, default_project=None),
        "github": {"username": "u", "host": "github.com", "token_ref": None,
                   "ssh_key_path": None, "ssh_key_ref": None},
        "aws": {"region": None, "profile": None, "access_key_id_ref": None,
                "secret_access_key_ref": None},
        "oci": {"profile": None, "config_file": None},
    }})
    prof = deserialize_config(raw).profiles["work"]
    assert prof.git_email is None
    assert prof.google.refresh_token_ref is None
    assert prof.github.token_ref is None
    assert prof.aws.region is None and prof.oci.profile is None


def test_the_leaf_check_covers_every_field_of_every_service_block():
    """Derived from the annotations, so a new field is covered as it is declared.

    Hand-listing the fields to check is how a check drifts from the dataclass it
    guards: the field added next release is the one nobody remembers to add
    here. This asserts the derivation actually reaches every field, so a future
    annotation the reader cannot classify (and therefore skips) fails here rather
    than silently opening the gap again.
    """
    from mien.config import (AtlassianService, AWSService, GoogleService,
                             NotionService, OCIService, _leaf_specs)
    from dataclasses import fields as dc_fields
    for cls in (GoogleService, GitHubService, SlackWorkspace, AWSService, OCIService,
                AtlassianService, NotionService):
        assert set(_leaf_specs(cls)) == {f.name for f in dc_fields(cls)}, cls.__name__


def test_the_serializers_own_output_survives_the_leaf_check():
    """A round-trip of a fully populated config, every service present.

    The check runs on what `_config_to_dict` writes, so anything mien serializes
    that the check would reject is a config mien can no longer open.
    """
    from mien.config import (AtlassianService, AWSService, NotionService,
                             OCIService, ProjectEnvScope)
    cfg = Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="keyring", options={"service_prefix": "mien-"}),
        bootstrap={"gcp_account": "me@x.example"},
        secret_naming=SecretNaming(default="acme.{kind}.{service}.{profile}",
                                   slack_token="acme.{workspace}.{profile}"),
        profiles={
            "work": Profile(
                name="work",
                google=GoogleService(
                    email="me@x.example", oauth_client_id="cid",
                    oauth_client_secret_ref=None, refresh_token_ref=None,
                    adc_ref=None, gcloud_config_name="work", default_project=None),
                github=GitHubService(username="u", host="github.com", token_ref="r"),
                slack=[SlackWorkspace(workspace="team-a", user_token_ref="r")],
                aws=AWSService(region="eu-west-1", profile="work"),
                oci=OCIService(profile="WORK"),
                atlassian=AtlassianService(email="me@x.example", base_url="https://x",
                                           api_token_ref="r"),
                notion=NotionService(api_token_ref="r"),
                project_env=[ProjectEnvScope(match="*/work", env={"AWS_PROFILE": "work"})],
                default_for=["*/Projects/acme"], owns_remotes=["github.com/acme/*"],
                git_email="me@x.example",
            ),
            "bare": Profile(name="bare"),
        },
    )
    assert deserialize_config(serialize_config(cfg)) == cfg


def test_a_serialized_slack_workspace_still_carries_a_null_team_id():
    """Deliberate, and not to be tidied away: it is a write-side compat shim.

    `SlackWorkspace.team_id` is gone from the dataclass and nothing reads it, but
    this same JSON is what `mien push` stores as the shared backend manifest, and
    the mien that reads it may be an OLDER one on another machine. There,
    `team_id` was a required positional with no default, so a workspace block
    without the key raises `TypeError: missing 1 required positional argument`
    inside `SlackWorkspace(**w)`. `mien sync` on that machine shows a traceback;
    `mien init` swallows the same exception into "(manifest check skipped: ...)",
    exits 0, and leaves that machine with the empty config init just wrote —
    every identity gone, almost silently. Writing the null keeps those readers
    working, and the read side already drops it (`_RETIRED_SERVICE_KEYS`).

    The other two fields retired alongside it need no shim: both
    `GoogleService.gcloud_login_required` and `Profile.git_name` had defaults, so
    an older mien constructs them fine when the key is absent.
    """
    cfg = Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="gcp_secret_manager", options={"project": "p"}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"work": Profile(name="work", slack=[
            SlackWorkspace(workspace="team-a", user_token_ref="r"),
            SlackWorkspace(workspace="team-b", user_token_ref="r2"),
        ])},
    )
    written = json.loads(serialize_config(cfg))["profiles"]["work"]["slack"]
    assert [w["workspace"] for w in written] == ["team-a", "team-b"]
    for w in written:
        assert "team_id" in w and w["team_id"] is None
    # And reading it back is unaffected: the key is dropped, not resurrected.
    back = deserialize_config(serialize_config(cfg)).profiles["work"].slack
    assert back == cfg.profiles["work"].slack
    assert not hasattr(back[0], "team_id")


def _raw_with_custom(value) -> dict:
    return {"$schema_version": 1, "secrets_backend": {"type": "keyring"},
            "bootstrap": {}, "secret_naming": {},
            "profiles": {"work": {"custom": value}}}


def test_a_config_with_no_custom_block_loads_unchanged():
    """The field is additive: every config written before it still loads.

    `custom` has a default_factory, so absence is an empty map — not a missing
    required key, which is what a bare annotation would have made it, and which
    would have made every existing config unloadable on upgrade.
    """
    raw = {"$schema_version": 1, "secrets_backend": {"type": "keyring"},
           "bootstrap": {}, "secret_naming": {},
           "profiles": {"p": {"github": None}, "q": {}}}
    cfg = deserialize_config(raw)
    assert cfg.profiles["p"].custom == {}
    assert cfg.profiles["q"].custom == {}
    # And an explicit null is absence too, like every other optional block.
    assert deserialize_config(_raw_with_custom(None)).profiles["work"].custom == {}
    assert deserialize_config(_raw_with_custom({})).profiles["work"].custom == {}


def test_custom_round_trips_as_names_pointing_at_refs():
    cfg = Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={"work": Profile(name="work", custom={
            "ANTHROPIC_API_KEY": "ref://mien-work-custom-anthropic_api_key",
            "NPM_TOKEN": "ref://mien-work-custom-npm_token",
        })},
    )
    written = json.loads(serialize_config(cfg))["profiles"]["work"]["custom"]
    assert written == {
        "ANTHROPIC_API_KEY": "ref://mien-work-custom-anthropic_api_key",
        "NPM_TOKEN": "ref://mien-work-custom-npm_token",
    }
    assert deserialize_config(serialize_config(cfg)) == cfg


def test_an_empty_custom_map_is_not_written_at_all():
    """The write-side shim for OLDER readers, and the reason it exists.

    A mien predating `custom` rejects an unknown *profile* key hard, and that
    ConfigError takes the whole config down — not just the profile carrying the
    key. Since this same JSON is what `mien push` stores as the shared manifest,
    writing `"custom": {}` on a profile that uses nothing new would cost an older
    machine every identity it has: `mien sync` fails, and `mien init` swallows it
    into "(manifest check skipped: ...)", exits 0, and leaves the empty config it
    just wrote. The printed remedy makes it worse — `mien push` from there
    uploads a custom-less config and empties the map for everyone.

    Omitting the key cannot save a profile that actually *uses* `custom` (asserted
    below: the key is written then, because the map is the identity). What it buys
    is that the break is confined to those profiles instead of being fleet-wide.
    """
    cfg = Config(
        schema_version=1,
        secrets_backend=BackendConfig(type="macos_keychain", options={}),
        bootstrap={}, secret_naming=SecretNaming(default=BUILTIN_DEFAULT, slack_token=BUILTIN_SLACK_TOKEN),
        profiles={
            "plain": Profile(name="plain"),
            "work": Profile(name="work", custom={"NPM_TOKEN": "ref://npm"}),
        },
    )
    written = json.loads(serialize_config(cfg))["profiles"]
    assert "custom" not in written["plain"]
    assert written["work"]["custom"] == {"NPM_TOKEN": "ref://npm"}
    # Absent is empty on the way back in, so the current reader is unaffected:
    # the omission says exactly what `{}` said.
    assert deserialize_config(serialize_config(cfg)) == cfg


@pytest.mark.parametrize("value, expect", [
    # A name that is not a shell identifier breaks the loader script `mien use`
    # writes: sourcing it fails at that line and abandons the rest of the file,
    # so every later export — the whole rest of the identity — silently vanishes.
    ({"2FA": "ref://x"},
     r"profile 'work': custom: '2FA' is not a usable environment variable name"),
    ({"MY VAR": "ref://x"},
     r"profile 'work': custom: 'MY VAR' is not a usable environment variable name"),
    ({"": "ref://x"},
     r"profile 'work': custom: '' is not a usable environment variable name"),
    # A collision with a built-in must not be resolved by whichever `build_env`
    # branch happens to run last.
    ({"GH_TOKEN": "ref://x"},
     r"profile 'work': custom: 'GH_TOKEN' is the environment variable mien "
     r"already uses for github"),
    ({"AWS_SECRET_ACCESS_KEY": "ref://x"}, r"already uses for aws"),
    ({"NOTION_TOKEN": "ref://x"}, r"already uses for notion"),
    ({"MIEN_PROFILE": "ref://x"}, r"already uses for mien itself"),
    # A name the shell itself reads is worse than a collision: the scrub is the
    # union over every profile, so this one name makes `mien use <anything>` and
    # `mien-unset` strip PATH in shells that never touch this profile.
    ({"PATH": "ref://x"},
     r"profile 'work': custom: 'PATH' is how the shell — and mien's own loader — "
     r"finds every program it runs"),
    ({"PS1": "ref://x"}, r"'PS1' is the prompt the shell renders"),
    ({"IFS": "ref://x"}, r"'IFS' is how the shell splits every word it parses"),
    ({"MIEN_CONFIG": "ref://x"}, r"'MIEN_CONFIG' is where mien reads and writes the config"),
    # A capture marker is worse still, and quieter: the same union-scrub emits
    # `unset CLAUDECODE` in every shell, which disarms the refusals that keep a
    # secret out of a recorded transcript — and nothing looks broken afterwards.
    ({"CLAUDECODE": "ref://x"},
     r"profile 'work': custom: 'CLAUDECODE' is the marker Claude Code sets to say "
     r"an agent, not a person, is driving this shell"),
    ({"MIEN_CAPTURED": "ref://x"},
     r"'MIEN_CAPTURED' is the marker you set yourself to tell mien this harness "
     r"records what mien prints"),
    ({"CLAUDE_CODE_ENTRYPOINT": "ref://x"}, r"agent entrypoint that is driving this shell"),
    # The value is a REFERENCE, and a non-string one dies in `backend.get` deep
    # inside `mien use`, long after the config that named it.
    ({"ANTHROPIC_API_KEY": 5},
     r"profile 'work': custom: 'ANTHROPIC_API_KEY' must be a backend secret "
     r"reference string \(not the secret itself\), got int: 5"),
    ({"ANTHROPIC_API_KEY": None}, r"must be a backend secret reference string"),
    ({"ANTHROPIC_API_KEY": ["ref://x"]}, r"must be a backend secret reference string"),
    # Shape, like every other block: checked, never coerced.
    ([{"ANTHROPIC_API_KEY": "ref://x"}],
     r"profile 'work': custom must be a JSON object, got list"),
    ("ANTHROPIC_API_KEY", r"profile 'work': custom must be a JSON object, got str"),
])
def test_a_bad_custom_block_is_refused_at_parse_time(value, expect):
    """A hand-edited config fails exactly as `mien login` would have.

    Both gates exist because either alone leaves the other open: the CLI cannot
    stop an editor, and the parser cannot stop a name being written in the first
    place. `ConfigError` specifically, so the fail-open surfaces announce rather
    than exiting in silence.
    """
    with pytest.raises(ConfigError, match=expect):
        deserialize_config(_raw_with_custom(value))


def test_a_custom_collision_message_points_at_the_builtin_login():
    """The remedy names the command that stores the built-in it would fight."""
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw_with_custom({"ATLASSIAN_API_TOKEN": "ref://x"}))
    assert "--service atlassian" in str(exc.value)


def test_every_builtin_variable_is_refused_as_a_custom_name():
    """Derived from the same map the scrub is, so a new built-in is covered too."""
    from mien.shell import BUILTIN_VARS
    for var in BUILTIN_VARS:
        with pytest.raises(ConfigError, match="already uses for"):
            deserialize_config(_raw_with_custom({var: "ref://x"}))


def test_every_shell_critical_variable_is_refused_as_a_custom_name():
    """Derived from the same map, so an entry added later is covered too.

    Each refusal quotes that map's description, which is what keeps a name from
    being listed without a reason a reader can weigh.
    """
    from mien.shell import SHELL_CRITICAL_VARS
    for var, description in SHELL_CRITICAL_VARS.items():
        with pytest.raises(ConfigError) as exc:
            deserialize_config(_raw_with_custom({var: "ref://x"}))
        assert description in str(exc.value)
        assert "Pick another name." in str(exc.value)


def test_every_capture_marker_is_refused_as_a_custom_name():
    """Derived from the map the DETECTOR reads, so a marker added later is covered.

    Each refusal quotes that map's description of what sets the marker, which is
    what keeps a name from being listed without a reason a reader can weigh.
    """
    from mien.shell import CAPTURE_MARKER_VARS
    for var, description in CAPTURE_MARKER_VARS.items():
        with pytest.raises(ConfigError) as exc:
            deserialize_config(_raw_with_custom({var: "ref://x"}))
        assert description in str(exc.value)
        assert "Pick another name." in str(exc.value)


def test_the_capture_marker_refusal_explains_what_clearing_it_would_disarm():
    """The reason is not "the shell needs it" — no shell does. It is polarity.

    `unset CLAUDECODE` breaks nothing and exporting a credential over it leaves it
    non-empty, i.e. still detected. The harm is the other direction: mien reads the
    marker's ABSENCE as "no agent is watching", so the scrub's `unset` turns two
    refusals off while everything still looks fine. A message that only said "pick
    another name" would leave a reader with no way to weigh that.
    """
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw_with_custom({"CLAUDECODE": "ref://x"}))
    message = str(exc.value)
    assert "mien reads it to decide whether an agent is driving" in message
    assert "ABSENCE as 'no agent'" in message
    assert "unset CLAUDECODE" in message
    assert "`mien token`" in message and "`mien exec`" in message
    assert "silently" in message


def test_the_shell_critical_list_does_not_overlap_the_builtins():
    """Two rules, two disjoint sets — a name in both would get the wrong message."""
    from mien.shell import BUILTIN_VARS, SHELL_CRITICAL_VARS
    assert not set(BUILTIN_VARS) & set(SHELL_CRITICAL_VARS)


def test_the_three_refused_sets_are_pairwise_disjoint():
    """Three reasons, three disjoint maps: a name in two would get one message.

    The capture markers are refused for a reason the shell-critical rule does not
    state (their absence is the permissive state, not a broken shell), so they are
    held apart rather than folded in — and a marker that drifted into either other
    map would be explained by the wrong sentence.
    """
    from mien.shell import BUILTIN_VARS, CAPTURE_MARKER_VARS, SHELL_CRITICAL_VARS
    assert not set(CAPTURE_MARKER_VARS) & set(BUILTIN_VARS)
    assert not set(CAPTURE_MARKER_VARS) & set(SHELL_CRITICAL_VARS)


@pytest.mark.parametrize(
    "name", ["MY_CLAUDECODE", "CLAUDECODE_EXTRA", "MIEN_CAPTURED_AT",
             "CLAUDE_CODE_ENTRYPOINT_URL"])
def test_a_name_merely_containing_a_capture_marker_is_accepted(name):
    """Exact match, not substring — `capture_context` reads exact names too.

    A substring rule would refuse usable credential names while protecting
    nothing: `unset MY_CLAUDECODE` clears no marker mien ever looks at.
    """
    cfg = deserialize_config(_raw_with_custom({name: "ref://x"}))
    assert cfg.profiles["work"].custom == {name: "ref://x"}


@pytest.mark.parametrize("name", ["MY_PATH", "PATH_TO_KEY", "HOMEBREW_TOKEN", "TMPDIR_KEY"])
def test_a_name_merely_containing_a_shell_critical_one_is_accepted(name):
    """Exact match, not substring: `PATH_TO_KEY` is an ordinary credential name.

    The loader emitting `unset PATH_TO_KEY` touches nothing the shell reads, so a
    substring rule would refuse usable names and protect nothing.
    """
    cfg = deserialize_config(_raw_with_custom({name: "ref://x"}))
    assert cfg.profiles["work"].custom == {name: "ref://x"}


def test_a_custom_name_is_a_known_profile_key_not_an_unknown_one():
    """`custom` is accepted at profile level; a typo of it is still fatal."""
    cfg = deserialize_config(_raw_with_custom({"ANTHROPIC_API_KEY": "ref://x"}))
    assert cfg.profiles["work"].custom == {"ANTHROPIC_API_KEY": "ref://x"}
    with pytest.raises(ConfigError, match="unknown key 'custm'"):
        deserialize_config({"$schema_version": 1,
                            "secrets_backend": {"type": "keyring"},
                            "profiles": {"work": {"custm": {"X": "ref://x"}}}})


def test_a_collision_with_miens_own_variable_suggests_no_login_command():
    """There is no `--service mien itself`, so the remedy must not invent one."""
    with pytest.raises(ConfigError) as exc:
        deserialize_config(_raw_with_custom({"MIEN_EPHEMERAL_DIR": "ref://x"}))
    assert "already uses for mien itself" in str(exc.value)
    assert "Pick another name." in str(exc.value)
    assert "--service" not in str(exc.value)
