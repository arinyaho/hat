import re
from pathlib import Path

import pytest

from mien.env import EnvBundle
from mien.shell import emit_unset, emit_use


def _parse_script_path(out: str) -> Path:
    """`emit_use` returns `. '<path>' && rm -f '<path>'`. Extract path."""
    m = re.match(r"\. '([^']+)' && rm -f '\1'", out.strip())
    assert m, f"unexpected emit_use output: {out!r}"
    return Path(m.group(1))


def test_emit_use_does_not_leak_secrets_to_stdout(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    bundle = EnvBundle(
        profile_name="personal",
        env={
            "MIEN_PROFILE": "personal",
            "GH_TOKEN": "ghp_xx'yy",
            "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/mien/x y.json",
        },
        ephemeral_files=[Path("/tmp/mien/x y.json")],
    )
    out = emit_use(bundle, {})

    # Critical: the secret value must never appear on stdout. The whole point
    # of routing exports through a 0600 file is that a caller who forgets to
    # `eval` doesn't leak the token to their transcript / history / ps output.
    assert "ghp_xx" not in out
    assert "ghp_xx'yy" not in out

    # The emitted snippet sources a path inside our TMPDIR.
    path = _parse_script_path(out)
    assert path.is_file()
    assert str(path).startswith(str(tmp_path))


def test_emit_use_writes_exports_to_a_0600_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    bundle = EnvBundle(
        profile_name="personal",
        env={
            "MIEN_PROFILE": "personal",
            "GH_TOKEN": "ghp_xx'yy",
            "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/mien/x y.json",
        },
    )
    out = emit_use(bundle, {})
    path = _parse_script_path(out)

    # mode bits must be owner-only (0o600) so other users on the box can't read.
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)

    body = path.read_text()
    assert "export MIEN_PROFILE='personal'" in body
    assert "GH_TOKEN='ghp_xx'\"'\"'yy'" in body
    assert "GOOGLE_APPLICATION_CREDENTIALS='/tmp/mien/x y.json'" in body


def test_emit_use_scrubs_stale_vars_before_exporting(tmp_path, monkeypatch):
    """Switching profiles must not leave the previous profile's variables set.
    The sourced script unsets every KNOWN_VARS name before exporting only what
    the new profile defines, so a var the new profile omits (here AWS_* and
    ATLASSIAN_*) is cleared rather than left dangling from an earlier `mien use`.
    The unset must precede the exports, or it would wipe the values it just set."""
    from mien.shell import KNOWN_VARS
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    bundle = EnvBundle(
        profile_name="personal",
        env={"MIEN_PROFILE": "personal", "GH_TOKEN": "ghp_new"},
    )
    body = _parse_script_path(emit_use(bundle, {})).read_text()

    scrub_line = next(ln for ln in body.splitlines() if ln.startswith("unset "))
    scrubbed = set(scrub_line.removeprefix("unset ").split())
    # Every known var is unset up front...
    assert set(KNOWN_VARS) == scrubbed
    # ...including ones this profile does not define (would be stale otherwise).
    assert {"AWS_ACCESS_KEY_ID", "ATLASSIAN_API_TOKEN"} <= scrubbed
    # ...and the unset happens before any export, so set values survive.
    assert body.index("unset ") < body.index("export "), body
    assert "export GH_TOKEN='ghp_new'" in body


def _profile(name, **kwargs):
    from mien.config import Profile
    return Profile(name=name, **kwargs)


def test_scrub_vars_is_the_builtins_plus_every_profiles_custom_names():
    """EVERY profile's, not the activated one's.

    What a scrub has to clear is whatever the shell was carrying before, and that
    is some *other* profile's set — the one being activated re-exports its own
    two lines later.
    """
    from mien.shell import KNOWN_VARS, custom_vars, scrub_vars
    profiles = {
        "work": _profile("work", custom={"ANTHROPIC_API_KEY": "ref://a"}),
        "personal": _profile("personal", custom={"NPM_TOKEN": "ref://n"}),
        "bare": _profile("bare"),
    }
    assert custom_vars(profiles) == ["ANTHROPIC_API_KEY", "NPM_TOKEN"]
    assert scrub_vars(profiles) == KNOWN_VARS + ["ANTHROPIC_API_KEY", "NPM_TOKEN"]
    # No config, or no custom variables anywhere: exactly the built-ins.
    assert scrub_vars({}) == KNOWN_VARS
    assert custom_vars({}) == []


def test_the_same_custom_name_in_two_profiles_is_listed_once():
    from mien.shell import custom_vars
    profiles = {
        "work": _profile("work", custom={"ANTHROPIC_API_KEY": "ref://w"}),
        "personal": _profile("personal", custom={"ANTHROPIC_API_KEY": "ref://p"}),
    }
    assert custom_vars(profiles) == ["ANTHROPIC_API_KEY"]


def test_known_vars_is_derived_from_the_builtin_owner_map():
    """One source of truth, so a variable added to one is scrubbed by the other."""
    from mien.shell import BUILTIN_VARS, KNOWN_VARS
    assert KNOWN_VARS == list(BUILTIN_VARS)
    assert BUILTIN_VARS["GH_TOKEN"] == "github"
    assert BUILTIN_VARS["GIT_SSH_COMMAND"] == "github"


def test_the_capture_markers_the_detector_reads_are_exactly_the_refused_ones(monkeypatch):
    """One list, two readers — pinned by injecting a marker neither hard-codes.

    The bug this pins: `capture_context` detected three markers that
    `check_custom_var_name` did not refuse, so `mien login --name CLAUDECODE` was
    accepted and the union-scrub then emitted `unset CLAUDECODE` in every shell,
    silently disarming the `mien token` and `mien exec` refusals. A test that
    listed the three names would go stale the moment a fourth is added — the
    reopening would look exactly like today's green suite. So instead: add a
    marker at runtime and assert BOTH sides move. A hand-copied list on either
    side fails this.
    """
    from mien.cli import capture_context
    from mien.config import ConfigError, check_custom_var_name
    from mien.shell import CAPTURE_MARKER_VARS

    monkeypatch.setitem(CAPTURE_MARKER_VARS, "HERMES_SESSION",
                        "the marker a made-up harness sets")
    monkeypatch.setenv("HERMES_SESSION", "1")
    # The detector picked it up...
    assert capture_context() == "HERMES_SESSION"
    # ...and so did the refusal, from the same map, quoting the same phrase.
    with pytest.raises(ConfigError) as exc:
        check_custom_var_name("--name", "HERMES_SESSION")
    assert "the marker a made-up harness sets" in str(exc.value)


def test_capture_markers_are_not_in_the_scrub_list():
    """They are refused as custom names, so nothing can put them in the scrub.

    Belt and braces on the other side of the same coupling: were a marker ever
    listed as a built-in instead, `KNOWN_VARS` would carry it and every `mien use`
    would clear it — the same disarming, by a different route.
    """
    from mien.shell import CAPTURE_MARKER_VARS, scrub_vars
    profiles = {"work": _profile("work", custom={"ANTHROPIC_API_KEY": "ref://a"})}
    assert not set(CAPTURE_MARKER_VARS) & set(scrub_vars(profiles))


def test_switching_to_a_profile_without_a_custom_var_still_clears_it():
    """The cross-profile leak, at the layer that writes the loader.

    `personal` defines no ANTHROPIC_API_KEY, so nothing re-exports it — the only
    thing standing between one identity's API key and the next identity's shell
    is that the scrub line names it.
    """
    from mien.shell import emit_use
    bundle = EnvBundle(profile_name="personal", env={"MIEN_PROFILE": "personal"})
    profiles = {
        "work": _profile("work", custom={"ANTHROPIC_API_KEY": "ref://a"}),
        "personal": _profile("personal"),
    }
    body = _parse_script_path(emit_use(bundle, profiles)).read_text()
    scrub_line = next(ln for ln in body.splitlines() if ln.startswith("unset "))
    assert "ANTHROPIC_API_KEY" in scrub_line.removeprefix("unset ").split()
    # And it is cleared before anything is exported, or the scrub would wipe the
    # new profile's own values.
    assert body.index("unset ") < body.index("export ")


def test_a_real_shell_sourcing_the_loader_loses_the_previous_key(tmp_path, monkeypatch):
    """The same claim, proved by running it rather than by reading it.

    A shell that activated `work` really is carrying ANTHROPIC_API_KEY; after
    sourcing the loader `mien use personal` writes, it must not be.
    """
    import subprocess

    from mien.shell import write_env_script
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    profiles = {
        "work": _profile("work", custom={"ANTHROPIC_API_KEY": "ref://a"}),
        "personal": _profile("personal", custom={"NPM_TOKEN": "ref://n"}),
    }
    script = write_env_script(
        EnvBundle(profile_name="personal",
                  env={"MIEN_PROFILE": "personal", "NPM_TOKEN": "npm-secret"}),
        profiles,
    )
    out = subprocess.run(
        ["bash", "-c",
         f'export ANTHROPIC_API_KEY=work-secret; . {script}; '
         'echo "anthropic=${ANTHROPIC_API_KEY-<unset>}"; '
         'echo "npm=${NPM_TOKEN-<unset>}"; echo "profile=$MIEN_PROFILE"'],
        capture_output=True, text=True, check=True,
    )
    assert "anthropic=<unset>" in out.stdout, out.stdout
    # ...while the profile being activated still gets its own.
    assert "npm=npm-secret" in out.stdout
    assert "profile=personal" in out.stdout


def test_emit_unset_is_exactly_the_scrub_list():
    """Not a list of names — a pin on the whole list, and it has to stay one.

    `mien unset` is the ONLY path that takes an interactive shell to *no*
    identity: `emit_use`'s scrub runs only when some other profile is being
    activated. So a name missing here is not cleared by anything else — it
    survives into the next command with the previous identity's value (a stale
    `GOOGLE_APPLICATION_CREDENTIALS` still pointing at that key file) while
    `mien status` reports nothing active. Derived from `scrub_vars`, with no
    hand-written names, so a built-in added later cannot be silently dropped
    from this path while the assertions still read as green.
    """
    from mien.shell import emit_unset, scrub_vars
    profiles = {"work": _profile("work", custom={"ANTHROPIC_API_KEY": "ref://a"})}
    out = emit_unset(profiles)
    assert out.split() == [w for v in scrub_vars(profiles) for w in ("unset", v)]


def test_the_packaged_schema_reference_lists_exactly_the_taken_names():
    """The one doc claim about this list that goes stale silently.

    `references/schema.md` is what an agent hand-edits a config from, and it
    enumerates the built-in variables a `custom` name may not collide with. A
    built-in added to `BUILTIN_VARS` and not to that list would leave the
    reference telling readers a taken name is free.
    """
    import re

    from mien.shell import BUILTIN_VARS
    doc = (Path(__file__).resolve().parent.parent
           / "plugins/mien/skills/mien/references/schema.md").read_text()
    para = next(line for line in doc.splitlines() if "nineteen taken names" in line)
    # The sentence names every taken variable, and nothing that is not one.
    assert set(re.findall(r"`([A-Z_][A-Z0-9_]*)`", para)) == set(BUILTIN_VARS)
    # ...the count it claims is the real one...
    assert len(BUILTIN_VARS) == 19
    # ...and each name sits in the group labelled with the service the collision
    # error attributes it to, so the doc attributes rather than merely lists.
    attributed = {}
    for group in re.finditer(r"([^()]*)\(([a-z ]+)\)", para):
        names, owner = group.group(1), group.group(2)
        attributed.update({v: owner for v in re.findall(r"`([A-Z_][A-Z0-9_]*)`", names)})
    assert attributed == BUILTIN_VARS
