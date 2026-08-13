import json
import os
import re

from conftest import make_config

import pytest
from click.testing import CliRunner

from mien.cli import main
from mien.config import (BackendConfig, Config, Profile, SecretNaming,
                         save_config)
from mien.secret_naming import BUILTIN_DEFAULT, BUILTIN_SLACK_TOKEN
from mien.statusline import guard_reason, render_segment


class TestGuardReason:
    def test_active_identity_mismatch_blocks(self):
        r = guard_reason("personal", "work", source="repo")
        assert r and "personal" in r and "work" in r and "belongs to work" in r

    def test_wrong_git_author_blocks_even_with_nothing_active(self):
        r = guard_reason(None, "work", source="repo", author_profile="personal")
        assert r and "authored as personal" in r and "work" in r

    def test_author_mismatch_blocks_even_when_active_profile_is_right(self):
        r = guard_reason("work", "work", source="repo", author_profile="personal")
        assert r and "authored as personal" in r

    def test_everything_agrees_allows(self):
        assert guard_reason("work", "work", source="repo", author_profile="work") is None

    def test_nothing_claims_the_place_allows(self):
        assert guard_reason("personal", None, author_profile="personal") is None

    def test_a_stale_unknown_active_profile_does_not_block(self):
        # env_known=False → we can't say who you are, so we don't refuse.
        assert guard_reason("ghost", "work", source="repo", env_known=False) is None

    def test_an_unrecognized_author_does_not_block(self):
        assert guard_reason(None, "work", source="repo", author_profile=None) is None


class TestRenderSegment:
    def test_env_and_dir_agree_is_calm(self):
        assert "🟢" in render_segment("work", "work")
        assert "mien:work" in render_segment("work", "work")

    def test_env_set_dir_unclaimed_is_calm(self):
        # An explicit MIEN_PROFILE with no directory objection is a known identity.
        out = render_segment("work", None)
        assert "🟢" in out and "mien:work" in out

    def test_dir_pins_with_no_env_is_calm(self):
        out = render_segment(None, "work")
        assert "🟢" in out and "mien:work" in out

    def test_env_disagrees_with_dir_is_alarm(self):
        # The core catch: set to personal, standing in a work directory.
        out = render_segment("personal", "work")
        assert "🔴" in out
        assert "personal" in out and "work" in out
        assert "✗" in out and "dir wants work" in out

    def test_env_disagrees_with_repo_is_alarm_with_repo_wording(self):
        # Same catch, but the claim came from the git remote owner.
        out = render_segment("personal", "work", source="repo")
        assert "🔴" in out and "repo is work's" in out

    def test_ambiguous_without_env_is_alarm(self):
        out = render_segment(None, None, ambiguous=True)
        assert "🔴" in out and "ambiguous" in out

    def test_ambiguous_dir_is_calm_when_env_breaks_the_tie(self):
        # An explicit profile resolves the ambiguity, exactly as `mien run` does.
        out = render_segment("work", None, ambiguous=True)
        assert "🟢" in out and "mien:work" in out

    def test_stale_unknown_env_profile_is_alarm(self):
        out = render_segment("ghost", None, env_unknown=True)
        assert "🔴" in out and "ghost" in out and "unknown" in out

    def test_wrong_git_author_is_alarm_even_with_nothing_active(self):
        # Nothing activated, but the repo is work's and you'd commit as personal.
        out = render_segment(None, "work", source="repo", author_profile="personal")
        assert "🔴" in out and "author:personal" in out and "repo is work's" in out

    def test_right_author_raises_no_alarm(self):
        out = render_segment(None, "work", source="repo", author_profile="work")
        assert "🟢" in out and "mien:work" in out

    def test_unknown_author_does_not_nag(self):
        # author_profile is None (email matched no profile) → no author alarm.
        out = render_segment(None, "work", source="repo", author_profile=None)
        assert "🟢" in out and "mien:work" in out

    def test_env_mismatch_takes_precedence_over_author_mismatch(self):
        out = render_segment("personal", "work", source="repo", author_profile="personal")
        assert "🔴" in out and "mien:personal" in out and "repo is work's" in out

    def test_author_alarm_fires_even_when_the_active_profile_is_right(self):
        # Correctly activated work, but user.email is still personal's — stale git config.
        out = render_segment("work", "work", source="repo", author_profile="personal")
        assert "🔴" in out and "author:personal" in out

    def test_nothing_set_and_nothing_claims_is_neutral(self):
        out = render_segment(None, None)
        assert "🟡" in out and "no profile here" in out


def _write_cfg(tmp_path, monkeypatch, **profiles):
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "config.json"))
    save_config(make_config(
        profiles={name: Profile(name=name, default_for=scopes)
                  for name, scopes in profiles.items()},
    ))


def _write_cfg_remotes(tmp_path, monkeypatch, **owns):
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "config.json"))
    save_config(make_config(
        profiles={name: Profile(name=name, owns_remotes=pats)
                  for name, pats in owns.items()},
    ))


def _run(cwd, monkeypatch, mien_profile=None, remote=None, author_email=None):
    if mien_profile is None:
        monkeypatch.delenv("MIEN_PROFILE", raising=False)
    else:
        monkeypatch.setenv("MIEN_PROFILE", mien_profile)
    # Mock the git I/O so directory-based tests never shell out; remote/author
    # tests supply a fixed origin and commit email.
    monkeypatch.setattr("mien.cli.git_origin_remote", lambda _cwd: remote)
    monkeypatch.setattr("mien.cli.git_author_email", lambda _cwd: author_email)
    payload = json.dumps({"workspace": {"current_dir": cwd}})
    return CliRunner().invoke(main, ["statusline"], input=payload)


def test_statusline_shows_the_directory_profile(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, work=["*/acme/*"], personal=["*/me/*"])
    result = _run("/w/acme/repo", monkeypatch)
    assert result.exit_code == 0
    assert "🟢" in result.output and "mien:work" in result.output


def test_statusline_flags_the_mismatch(tmp_path, monkeypatch):
    """The heart-racing case: personal is active, but this directory is work's."""
    _write_cfg(tmp_path, monkeypatch, work=["*/acme/*"], personal=["*/me/*"])
    result = _run("/w/acme/repo", monkeypatch, mien_profile="personal")
    assert result.exit_code == 0
    assert "🔴" in result.output
    assert "personal" in result.output and "work" in result.output


def test_statusline_neutral_when_no_scope_claims_the_dir(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, work=["*/acme/*"])
    result = _run("/tmp/somewhere-else", monkeypatch)
    assert result.exit_code == 0
    assert "🟡" in result.output and "no profile here" in result.output


def test_statusline_shows_pending_for_an_unapproved_mien(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, work=[])
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".mien").write_text("work\n")
    result = _run(str(ws), monkeypatch)
    assert result.exit_code == 0
    assert "🟡" in result.output and "work?" in result.output
    assert "allow" in result.output


def test_statusline_shows_an_approved_mien_as_the_identity(tmp_path, monkeypatch):
    from mien.project import record_allow
    _write_cfg(tmp_path, monkeypatch, work=[])
    ws = tmp_path / "ws"
    ws.mkdir()
    decl = ws / ".mien"
    decl.write_text("work\n")
    record_allow(str(decl.resolve()), "work")
    result = _run(str(ws), monkeypatch)
    assert result.exit_code == 0
    assert "🟢" in result.output and "mien:work" in result.output


def test_statusline_is_silent_without_a_config(tmp_path, monkeypatch):
    # No config file at all — mien is not set up here, so print nothing.
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "does-not-exist.json"))
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    result = CliRunner().invoke(
        main, ["statusline"],
        input=json.dumps({"workspace": {"current_dir": "/w/acme/repo"}}),
    )
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_statusline_says_so_on_an_unreadable_config(tmp_path, monkeypatch):
    # A config that exists but does not parse is not "nothing to report" — mien
    # can no longer tell who you are here, and the status line must say so. It
    # has to land on stdout: that is the stream Claude Code renders.
    cfg = tmp_path / "config.json"
    cfg.write_text("{ not json")
    monkeypatch.setenv("MIEN_CONFIG", str(cfg))
    monkeypatch.delenv("MIEN_PROFILE", raising=False)
    result = CliRunner().invoke(
        main, ["statusline"],
        input=json.dumps({"workspace": {"current_dir": "/w/acme/repo"}}),
    )
    assert result.exit_code == 0
    assert "mien:config" in result.stdout
    assert result.stderr.strip() == ""
    assert result.stdout.count("\n") == 1  # one status-line row, not a report


def test_statusline_flags_a_stale_active_profile(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, work=["*/acme/*"])
    result = _run("/w/acme/repo", monkeypatch, mien_profile="deleted-profile")
    assert result.exit_code == 0
    assert "🔴" in result.output and "deleted-profile" in result.output
    assert "unknown" in result.output


def test_statusline_resolves_by_git_remote_owner(tmp_path, monkeypatch):
    """Flat layout: no directory scope, but the repo's origin names its owner."""
    _write_cfg_remotes(tmp_path, monkeypatch,
                       work=["github.com/acme-*/*"], personal=["github.com/me/*"])
    result = _run("/anywhere/flat/api", monkeypatch,
                  remote="git@github.com:acme-core/api.git")
    assert result.exit_code == 0
    assert "🟢" in result.output and "mien:work" in result.output


def test_statusline_flags_wrong_identity_by_remote(tmp_path, monkeypatch):
    """personal is active, but this repo's origin belongs to work."""
    _write_cfg_remotes(tmp_path, monkeypatch,
                       work=["github.com/acme-*/*"], personal=["github.com/me/*"])
    result = _run("/anywhere/flat/api", monkeypatch, mien_profile="personal",
                  remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0
    assert "🔴" in result.output
    assert "repo is work's" in result.output and "personal" in result.output


def _write_cfg_full(tmp_path, monkeypatch, profiles):
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "config.json"))
    save_config(make_config(
        profiles=profiles,
    ))


def test_statusline_flags_a_wrong_git_author(tmp_path, monkeypatch):
    """The full mis-commit catch: no profile active, but the repo belongs to work
    while `git config user.email` is personal's known address."""
    from mien.config import GoogleService
    _write_cfg_full(tmp_path, monkeypatch, {
        "work": Profile(name="work", owns_remotes=["github.com/acme-*/*"],
                        google=GoogleService(
                            email="me@acme.example", oauth_client_id="c",
                            oauth_client_secret_ref=None, refresh_token_ref=None,
                            adc_ref=None, gcloud_config_name="work",
                            default_project=None)),
        "personal": Profile(name="personal", google=GoogleService(
                            email="me@personal.example", oauth_client_id="c",
                            oauth_client_secret_ref=None, refresh_token_ref=None,
                            adc_ref=None, gcloud_config_name="personal",
                            default_project=None)),
    })
    result = _run("/flat/api", monkeypatch,
                  remote="https://github.com/acme-core/api.git",
                  author_email="me@personal.example")
    assert result.exit_code == 0
    assert "🔴" in result.output
    assert "author:personal" in result.output and "repo is work's" in result.output


def test_statusline_is_calm_when_the_git_author_matches(tmp_path, monkeypatch):
    from mien.config import GoogleService
    _write_cfg_full(tmp_path, monkeypatch, {
        "work": Profile(name="work", owns_remotes=["github.com/acme-*/*"],
                        google=GoogleService(
                            email="me@acme.example", oauth_client_id="c",
                            oauth_client_secret_ref=None, refresh_token_ref=None,
                            adc_ref=None, gcloud_config_name="work",
                            default_project=None)),
    })
    result = _run("/flat/api", monkeypatch,
                  remote="https://github.com/acme-core/api.git",
                  author_email="me@acme.example")
    assert result.exit_code == 0
    assert "🟢" in result.output and "mien:work" in result.output


def test_statusline_does_not_nag_on_an_unknown_git_author(tmp_path, monkeypatch):
    _write_cfg_remotes(tmp_path, monkeypatch, work=["github.com/acme-*/*"])
    result = _run("/flat/api", monkeypatch,
                  remote="https://github.com/acme-core/api.git",
                  author_email="someone@nowhere.example")
    assert result.exit_code == 0
    assert "🟢" in result.output and "mien:work" in result.output


def _run_guard(cwd, monkeypatch, mien_profile=None, remote=None, author_email=None,
               guard_env=None, force=False):
    if mien_profile is None:
        monkeypatch.delenv("MIEN_PROFILE", raising=False)
    else:
        monkeypatch.setenv("MIEN_PROFILE", mien_profile)
    if guard_env is None:
        monkeypatch.delenv("MIEN_GUARD", raising=False)
    else:
        monkeypatch.setenv("MIEN_GUARD", guard_env)
    monkeypatch.setattr("mien.cli._logical_cwd", lambda: cwd)
    monkeypatch.setattr("mien.cli.git_origin_remote", lambda _cwd: remote)
    monkeypatch.setattr("mien.cli.git_author_email", lambda _cwd: author_email)
    args = ["guard", "--force"] if force else ["guard"]
    return CliRunner().invoke(main, args)


def _write_broken_cfg(tmp_path, monkeypatch):
    """A config that exists but does not parse — the fail-open surfaces' hard case.

    Not "mien is unconfigured" (that is a missing file, and silence is right):
    the file is there and mien can no longer read who you are from it.
    """
    cfg = tmp_path / "config.json"
    cfg.write_text("{ not json")
    monkeypatch.setenv("MIEN_CONFIG", str(cfg))


def _write_cfg_with_a_non_string_backend_type(tmp_path, monkeypatch):
    """Valid JSON, well-formed everywhere except `secrets_backend.type`.

    The quietest shape of "unreadable": the file parses as JSON and every
    identity in it is spelled correctly, so the operator has no reason to
    suspect anything. `type` alone is the wrong type, and it used to be the one
    leaf handed to `BackendConfig` unchecked — the resulting `TypeError` came
    out of `deserialize_config`, which is not `ConfigError`, so guard neither
    enforced nor announced.

    The profiles are the same ones as `test_guard_blocks_a_wrong_active_identity`
    so the two tests differ in exactly one key: with a string `type` this config
    makes guard refuse, which is what the announcement is standing in for.
    """
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "$schema_version": 1,
        "secrets_backend": {"type": ["macos_keychain"]},
        "bootstrap": {},
        "secret_naming": {},
        "profiles": {
            "work": {"owns_remotes": ["github.com/acme-*/*"]},
            "personal": {"owns_remotes": ["github.com/me/*"]},
        },
    }))
    monkeypatch.setenv("MIEN_CONFIG", str(cfg))


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _run_prompt(cwd, monkeypatch, mien_profile=None, remote=None, author_email=None):
    if mien_profile is None:
        monkeypatch.delenv("MIEN_PROFILE", raising=False)
    else:
        monkeypatch.setenv("MIEN_PROFILE", mien_profile)
    monkeypatch.setattr("mien.cli._logical_cwd", lambda: cwd)
    monkeypatch.setattr("mien.cli.git_origin_remote", lambda _cwd: remote)
    monkeypatch.setattr("mien.cli.git_author_email", lambda _cwd: author_email)
    return CliRunner().invoke(main, ["prompt"])


def test_prompt_shows_the_segment_from_the_shell_cwd(tmp_path, monkeypatch):
    _write_cfg_remotes(tmp_path, monkeypatch, work=["github.com/acme-*/*"])
    result = _run_prompt("/flat/api", monkeypatch, mien_profile="work",
                         remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0
    assert "🟢" in result.output and "mien:work" in result.output
    # No trailing newline — it embeds into a prompt.
    assert not result.output.endswith("\n")


def test_prompt_flags_a_mismatch(tmp_path, monkeypatch):
    _write_cfg_remotes(tmp_path, monkeypatch,
                       work=["github.com/acme-*/*"], personal=["github.com/me/*"])
    result = _run_prompt("/flat/api", monkeypatch, mien_profile="personal",
                         remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0
    assert "🔴" in result.output and "repo is work's" in result.output


def test_prompt_is_silent_without_a_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "nope.json"))
    result = _run_prompt("/flat/api", monkeypatch, mien_profile="work",
                         remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0 and result.output == ""


def test_prompt_says_so_on_an_unreadable_config(tmp_path, monkeypatch):
    # A blank prompt segment reads as "nothing to report", when in fact mien can
    # no longer tell who you are here — so it shows a marker instead of nothing.
    _write_broken_cfg(tmp_path, monkeypatch)
    result = _run_prompt("/flat/api", monkeypatch, mien_profile="work",
                         remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0  # a prompt command must never fail the shell
    assert "mien:config" in result.stdout


def test_prompt_puts_the_config_marker_on_stdout_not_stderr(tmp_path, monkeypatch):
    # Deliberate, and easy for a later refactor to flip: a prompt redraws
    # constantly and its stderr goes straight to the terminal, so a message
    # there would spam every redraw. The marker rides in the segment instead.
    _write_broken_cfg(tmp_path, monkeypatch)
    result = _run_prompt("/flat/api", monkeypatch, mien_profile="work",
                         remote="https://github.com/acme-core/api.git")
    assert "mien:config" in result.stdout
    assert result.stderr == ""


def test_prompt_config_marker_stays_compact(tmp_path, monkeypatch):
    # It has to sit inside a prompt line: no newline (same as a healthy
    # segment), and no parse detail — `mien doctor` is where that belongs.
    _write_broken_cfg(tmp_path, monkeypatch)
    result = _run_prompt("/flat/api", monkeypatch, mien_profile="work",
                         remote="https://github.com/acme-core/api.git")
    assert "\n" not in result.stdout
    assert "invalid config JSON" not in result.stdout
    assert str(tmp_path) not in result.stdout
    assert len(_ANSI.sub("", result.stdout)) <= 32


def test_guard_fails_open_on_an_unreadable_config_and_says_it_is_not_enforcing(
        tmp_path, monkeypatch):
    # These are the exact inputs that make guard refuse when the config parses
    # (see test_guard_blocks_a_wrong_active_identity), so this pins both halves
    # at once: it no longer blocks — a broken config must not wedge a commit —
    # and it says why, because a guard that silently stopped guarding is worse
    # than one that admits it. Neither half may regress alone.
    _write_broken_cfg(tmp_path, monkeypatch)
    result = _run_guard("/flat/api", monkeypatch, mien_profile="personal",
                        remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0
    assert "refusing" not in result.output
    assert "NOT enforcing" in result.stderr
    assert "config unreadable" in result.stderr
    # A pre-commit hook's stdout is not a place to narrate; the warning is a
    # diagnostic, so it goes to stderr and nowhere else.
    assert result.stdout == ""


def test_guard_announces_rather_than_going_quiet_on_a_non_string_backend_type(
        tmp_path, monkeypatch):
    """The consequence, not the exception type: guard may not fall silent here.

    These are the inputs of `test_guard_blocks_a_wrong_active_identity` — the
    config even names the same two profiles — with `secrets_backend.type` alone
    changed from a string to a list. That made `deserialize_config` raise a bare
    `TypeError`, which the fail-open surfaces do not recognize as a config
    failure, so guard exited 0 with both streams empty: a pre-commit hook waving
    through the exact mis-identity commit it is installed to block, saying
    nothing. Either outcome is acceptable — refuse, or fail open and admit it —
    but not silence.
    """
    _write_cfg_with_a_non_string_backend_type(tmp_path, monkeypatch)
    result = _run_guard("/flat/api", monkeypatch, mien_profile="personal",
                        remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0
    assert "NOT enforcing" in result.stderr
    assert "config unreadable" in result.stderr
    assert result.stdout == ""


def test_statusline_says_so_on_a_non_string_backend_type(tmp_path, monkeypatch):
    # Same config, the other always-on surface: a blank segment would read as
    # "nothing to report" when mien can no longer tell who you are here.
    _write_cfg_with_a_non_string_backend_type(tmp_path, monkeypatch)
    result = _run("/flat/api", monkeypatch,
                  remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0
    assert "mien:config" in result.stdout
    assert "🟢" not in result.stdout


def test_guard_blocks_a_wrong_active_identity(tmp_path, monkeypatch):
    _write_cfg_remotes(tmp_path, monkeypatch,
                       work=["github.com/acme-*/*"], personal=["github.com/me/*"])
    result = _run_guard("/flat/api", monkeypatch, mien_profile="personal",
                        remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 1
    assert "refusing" in result.output and "personal" in result.output


def test_guard_blocks_a_wrong_commit_author_with_nothing_active(tmp_path, monkeypatch):
    from mien.config import GoogleService
    _write_cfg_full(tmp_path, monkeypatch, {
        "work": Profile(name="work", owns_remotes=["github.com/acme-*/*"],
                        google=GoogleService(
                            email="me@acme.example", oauth_client_id="c",
                            oauth_client_secret_ref=None, refresh_token_ref=None,
                            adc_ref=None, gcloud_config_name="work",
                            default_project=None)),
        "personal": Profile(name="personal", google=GoogleService(
                            email="me@personal.example", oauth_client_id="c",
                            oauth_client_secret_ref=None, refresh_token_ref=None,
                            adc_ref=None, gcloud_config_name="personal",
                            default_project=None)),
    })
    result = _run_guard("/flat/api", monkeypatch,
                        remote="https://github.com/acme-core/api.git",
                        author_email="me@personal.example")
    assert result.exit_code == 1
    assert "authored as personal" in result.output


def test_guard_allows_when_consistent(tmp_path, monkeypatch):
    _write_cfg_remotes(tmp_path, monkeypatch, work=["github.com/acme-*/*"])
    result = _run_guard("/flat/api", monkeypatch, mien_profile="work",
                        remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_guard_allows_on_an_unknown_owner(tmp_path, monkeypatch):
    _write_cfg_remotes(tmp_path, monkeypatch, work=["github.com/acme-*/*"])
    result = _run_guard("/flat/api", monkeypatch, mien_profile="personal",
                        remote="https://github.com/nobody/x.git")
    assert result.exit_code == 0


def test_guard_override_via_env_lets_it_through(tmp_path, monkeypatch):
    _write_cfg_remotes(tmp_path, monkeypatch,
                       work=["github.com/acme-*/*"], personal=["github.com/me/*"])
    result = _run_guard("/flat/api", monkeypatch, mien_profile="personal",
                        remote="https://github.com/acme-core/api.git", guard_env="off")
    assert result.exit_code == 0


def test_guard_force_flag_lets_it_through(tmp_path, monkeypatch):
    _write_cfg_remotes(tmp_path, monkeypatch,
                       work=["github.com/acme-*/*"], personal=["github.com/me/*"])
    result = _run_guard("/flat/api", monkeypatch, mien_profile="personal",
                        remote="https://github.com/acme-core/api.git", force=True)
    assert result.exit_code == 0


def test_guard_allows_when_mien_is_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "nope.json"))
    result = _run_guard("/flat/api", monkeypatch, mien_profile="personal",
                        remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0


def _write_unopenable_cfg(tmp_path, monkeypatch):
    """A config that is present and perfectly valid, but cannot be OPENED.

    The other half of "unreadable": `_write_broken_cfg` is a file mien can read
    and not parse; this is one mien cannot read at all — mode 0600 owned by
    another user on a shared machine, or a config written under `sudo`. Both
    leave mien unable to say who you are, so both have to be announced instead
    of being swallowed by a catch-all.

    The config underneath is deliberately a *working* one that renders a healthy
    green segment, so if the file ever becomes readable again these tests fail
    rather than passing for the wrong reason.
    """
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file regardless")
    _write_cfg_remotes(tmp_path, monkeypatch, work=["github.com/acme-*/*"])
    (tmp_path / "config.json").chmod(0o000)


def _point_cfg_at_a_directory(tmp_path, monkeypatch):
    """MIEN_CONFIG pointing at a directory — an OSError that is not permissions.

    Pins that "present but unreadable" is the I/O *class* being announced, not
    PermissionError specifically.
    """
    d = tmp_path / "config.json"
    d.mkdir()
    monkeypatch.setenv("MIEN_CONFIG", str(d))


def test_statusline_says_so_on_a_config_it_cannot_open(tmp_path, monkeypatch):
    _write_unopenable_cfg(tmp_path, monkeypatch)
    result = _run("/flat/api", monkeypatch,
                  remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0
    assert "mien:config" in result.stdout      # not a blank, silent segment
    assert "🟢" not in result.stdout           # and not a healthy one either
    assert result.stderr.strip() == ""
    assert result.stdout.count("\n") == 1


def test_statusline_says_so_when_the_config_path_is_a_directory(tmp_path, monkeypatch):
    _point_cfg_at_a_directory(tmp_path, monkeypatch)
    result = _run("/flat/api", monkeypatch,
                  remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0
    assert "mien:config" in result.stdout


def test_statusline_says_so_on_a_dangling_config_symlink(tmp_path, monkeypatch):
    # ENOENT, like an absent config — but a symlink is something the operator
    # put there on purpose, so this is a config whose target went missing, not
    # an unconfigured machine. It must be announced, not passed off as silence.
    link = tmp_path / "config.json"
    link.symlink_to(tmp_path / "gone.json")
    monkeypatch.setenv("MIEN_CONFIG", str(link))
    result = _run("/flat/api", monkeypatch,
                  remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0
    assert "mien:config" in result.stdout


def test_prompt_says_so_on_a_config_it_cannot_open(tmp_path, monkeypatch):
    _write_unopenable_cfg(tmp_path, monkeypatch)
    result = _run_prompt("/flat/api", monkeypatch, mien_profile="work",
                         remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0  # a prompt command must never fail the shell
    assert "mien:config" in result.stdout
    assert "🟢" not in result.stdout
    assert result.stderr == ""    # a prompt redraws constantly; stderr would spam
    assert "\n" not in result.stdout


def test_guard_fails_open_on_a_config_it_cannot_open_and_says_it_is_not_enforcing(
        tmp_path, monkeypatch):
    # The same pair of guarantees as the unparseable-config case, for a config
    # mien cannot open: it must not wedge the commit, and it must not stop
    # guarding in silence.
    _write_unopenable_cfg(tmp_path, monkeypatch)
    result = _run_guard("/flat/api", monkeypatch, mien_profile="personal",
                        remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0
    assert "refusing" not in result.output
    assert "NOT enforcing" in result.stderr
    assert "config unreadable" in result.stderr
    assert result.stdout == ""


def test_guard_fails_open_when_the_config_path_is_a_directory(tmp_path, monkeypatch):
    _point_cfg_at_a_directory(tmp_path, monkeypatch)
    result = _run_guard("/flat/api", monkeypatch, mien_profile="personal",
                        remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0
    assert "NOT enforcing" in result.stderr


def test_an_absent_config_stays_silent_everywhere(tmp_path, monkeypatch):
    # The regression the announcement must not cost: no config at all means mien
    # is simply not set up here, which is not a failure. All three surfaces stay
    # quiet — none of them may start reporting "unreadable" for a machine that
    # never configured mien.
    missing = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("MIEN_CONFIG", str(missing))

    line = _run("/flat/api", monkeypatch,
                remote="https://github.com/acme-core/api.git")
    assert line.exit_code == 0
    assert line.output.strip() == ""

    prompt = _run_prompt("/flat/api", monkeypatch, mien_profile="work",
                         remote="https://github.com/acme-core/api.git")
    assert prompt.exit_code == 0 and prompt.output == ""

    guard = _run_guard("/flat/api", monkeypatch, mien_profile="personal",
                       remote="https://github.com/acme-core/api.git")
    assert guard.exit_code == 0
    assert guard.output == "" and guard.stderr == ""


def test_statusline_remote_owner_beats_a_directory_scope(tmp_path, monkeypatch):
    """When both signals resolve and disagree, the repo's remote wins."""
    monkeypatch.setenv("MIEN_CONFIG", str(tmp_path / "config.json"))
    save_config(make_config(
        profiles={
            "work": Profile(name="work", owns_remotes=["github.com/acme-*/*"]),
            "personal": Profile(name="personal", default_for=["*/flat/*"]),
        },
    ))
    result = _run("/anywhere/flat/api", monkeypatch,
                  remote="https://github.com/acme-core/api.git")
    assert result.exit_code == 0
    assert "🟢" in result.output and "mien:work" in result.output


def test_statusline_flags_a_token_in_the_remote(tmp_path, monkeypatch):
    """A credential in the remote URL outranks every other signal: git acts as
    that token's owner whatever mien routed, and the segment must not echo it."""
    _write_cfg(tmp_path, monkeypatch, work=["*/acme/*"], personal=["*/me/*"])
    result = _run("/w/acme/repo", monkeypatch, mien_profile="work",
                  remote="https://x-access-token:SECRETVALUE@github.com/acme/repo.git")
    assert result.exit_code == 0
    assert "🔴" in result.output
    assert "origin embeds a token" in result.output
    assert "SECRETVALUE" not in result.output
    # the profile agreeing with the directory must not turn this green
    assert "🟢" not in result.output


def test_statusline_stays_green_on_an_ordinary_remote(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, work=["*/acme/*"], personal=["*/me/*"])
    result = _run("/w/acme/repo", monkeypatch, mien_profile="work",
                  remote="https://github.com/acme/repo.git")
    assert result.exit_code == 0
    assert "🟢" in result.output and "mien:work" in result.output
