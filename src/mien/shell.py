from __future__ import annotations

import os
import secrets as _secrets
from collections.abc import Mapping
from pathlib import Path

from mien.config import Profile
from mien.env import MIEN_INTERNAL_OWNER, EnvBundle

# The shell wrappers, as one canonical source. `mien shell-init` prints this so a
# user can wire it up with `eval "$(mien shell-init)"` — no repo checkout needed,
# which is the whole point: the CLI installs from a git URL and this comes with
# it. zsh and bash share the body; only the header comment differs.
_SHELL_WRAPPERS = """\
mien-use() {
  if [ -z "$1" ]; then
    echo "usage: mien-use <profile>" >&2
    return 2
  fi
  local exports
  # $$ is this shell's pid (unchanged inside the command substitution), so the
  # ephemeral files live as long as this shell rather than the mien process.
  exports="$(command mien use --owner-pid $$ "$1")" || return $?
  eval "$exports"
}

mien-unset() {
  local clears
  clears="$(command mien unset)" || return $?
  eval "$clears"
}

__mien_atexit() {
  if [ -n "$MIEN_PROFILE" ]; then
    command mien doctor --gc >/dev/null 2>&1 || true
  fi
}

trap __mien_atexit EXIT
"""

_SUPPORTED_SHELLS = ("zsh", "bash")


def render_shell_init(shell: str) -> str:
    """The shell wrappers (`mien-use`, `mien-unset`, the exit-trap GC) for eval.

    zsh and bash take the same body — POSIX `[ ]` tests, `local`, and an EXIT
    trap all work in both. Kept as one string so the two cannot drift.
    """
    if shell not in _SUPPORTED_SHELLS:
        raise ValueError(
            f"unsupported shell {shell!r}; expected one of {', '.join(_SUPPORTED_SHELLS)}"
        )
    header = f"# mien shell integration — eval \"$(mien shell-init --shell {shell})\"\n"
    return header + _SHELL_WRAPPERS


# Re-exported from `mien.env`, which sets these variables and must name their
# owner the same way — see the note there. Kept importable from here because the
# collision message keys off it: for a real service it can suggest
# `--service <owner>`, and for these two there is no such command to suggest.

# Every environment variable a built-in service puts in the environment, and the
# service that owns it. A map rather than a bare list because two readers need
# the owner: the collision check that refuses a `custom` variable named after a
# built-in has to say which service it would fight (`mien.config`), and nothing
# else in the file can tell you that `GIT_SSH_COMMAND` is github's.
BUILTIN_VARS: dict[str, str] = {
    "MIEN_PROFILE": MIEN_INTERNAL_OWNER,
    "MIEN_EPHEMERAL_DIR": MIEN_INTERNAL_OWNER,
    "CLOUDSDK_ACTIVE_CONFIG_NAME": "google",
    "CLOUDSDK_CORE_PROJECT": "google",
    "GOOGLE_APPLICATION_CREDENTIALS": "google",
    "GH_TOKEN": "github",
    "MIEN_SLACK_TOKENS": "slack",
    "MIEN_SLACK_DEFAULT_TOKEN": "slack",
    "AWS_PROFILE": "aws",
    "AWS_DEFAULT_REGION": "aws",
    "AWS_ACCESS_KEY_ID": "aws",
    "AWS_SECRET_ACCESS_KEY": "aws",
    "OCI_CLI_PROFILE": "oci",
    "OCI_CLI_CONFIG_FILE": "oci",
    "ATLASSIAN_EMAIL": "atlassian",
    "ATLASSIAN_API_TOKEN": "atlassian",
    "ATLASSIAN_BASE_URL": "atlassian",
    "NOTION_TOKEN": "notion",
    "GIT_SSH_COMMAND": "github",
}

# Derived, never hand-listed: a variable added to `BUILTIN_VARS` is scrubbed from
# the moment it exists, and the two cannot drift.
KNOWN_VARS = list(BUILTIN_VARS)

# The built-ins whose value is a selector, a path or an address rather than a
# credential, so `mien status` may print it. An allowlist rather than a denylist
# of secrets on purpose: a variable added to `BUILTIN_VARS` later is masked until
# someone decides it is safe, which is the direction that fails closed.
NON_SECRET_VARS: frozenset[str] = frozenset({
    "CLOUDSDK_ACTIVE_CONFIG_NAME",
    "CLOUDSDK_CORE_PROJECT",
    "GOOGLE_APPLICATION_CREDENTIALS",   # a path to a 0600 file, not the token
    "GIT_SSH_COMMAND",                  # `ssh -i <path>`
    "MIEN_SLACK_TOKENS",                # a path to the 0600 workspace map
    "AWS_PROFILE",
    "AWS_DEFAULT_REGION",
    "OCI_CLI_PROFILE",
    "OCI_CLI_CONFIG_FILE",
    "ATLASSIAN_EMAIL",
    "ATLASSIAN_BASE_URL",
})

# Variables a `custom` credential may not be named after, each mapped to what it
# is — the message quotes the phrase, so a name cannot be listed here without
# saying why (`mien.config.check_custom_var_name`).
#
# One rule, in two halves, and an entry has to satisfy at least one: the
# machinery that carries out `mien use` / `mien unset` READS this variable as an
# instruction rather than carrying it as payload, so either (a) `unset`ting it
# stops the shell or the loader from working, or (b) `export`ing a credential
# over it puts that credential somewhere nobody asked for. The blast radius is
# what makes the refusal worth having: `scrub_vars` is the union over EVERY
# profile, so one such name in one profile makes every `mien use` and every
# `mien-unset`, in every shell, strip it.
#
# Deliberately NOT a denylist of POSIX variables. A long list is unmaintainable
# and would refuse names that are only ever payload; candidates weighed and left
# off, each for a reason that can be checked:
#
# - `PWD`/`OLDPWD`: zsh and bash re-set `PWD` on the next `cd`, so the `unset` is
#   self-healing, and mien already distrusts the value it finds (`_trusted_cwd`
#   validates it with `samefile` and falls back to `os.getcwd()`).
# - `MIEN_GUARD`/`MIEN_EXEC`/`MIEN_TOKEN`: opt-OUTs, matched against a fixed set
#   of off-values. Unset or overwritten with a credential, both land on "guard
#   on" — the safe side — so neither half of the rule is met. Note the contrast
#   with `CAPTURE_MARKER_VARS` below, which are also `MIEN_*`-adjacent signals and
#   ARE refused: what separates them is polarity, not naming. These three are
#   read as "is the value one of a fixed set of off-words?", so absence means
#   enforcing; a capture marker is read as "is anything set?", so absence means
#   permissive — which is why the scrub's `unset` is harmless here and unsafe
#   there.
# - `XDG_CONFIG_HOME`: mien reads it for one non-credential path (the global
#   gitignore `.mien` goes in), and that write already fails soft on `OSError`.
# - `LD_PRELOAD`/`DYLD_*`: neither half holds. `unset` does not break the shell,
#   and exporting a credential leaves the dynamic loader failing to open a
#   library by that name — it stores no credential anywhere. The remaining
#   argument, that mien should not be a channel for library injection, is a
#   security claim this check cannot back: the value comes from the user's own
#   backend into the user's own shell, granting nothing they could not do with
#   `export` themselves, and listing them would advertise a sandbox mien does
#   not implement.
SHELL_CRITICAL_VARS: dict[str, str] = {
    # (a) The shell finds every program through it — and so does mien's own
    # loader: the `mien-use` wrapper runs `command mien`, and the script it evals
    # runs `rm -f`. Unset, nothing in the shell runs at all.
    "PATH": "how the shell — and mien's own loader — finds every program it runs",
    # (a) Tilde expansion, and every tool's idea of where its config lives,
    # including mien's: `config_path` reads `$HOME/.config/mien/config.json`, so
    # a credential exported over it hides mien's config from mien.
    "HOME": "where the shell, mien and every other tool look for files, mien's own config included",
    # (a) The shell splits every unquoted expansion by it, including inside the
    # `eval` that loads a profile, so a credential here changes how the shell
    # parses everything that comes after.
    "IFS": "how the shell splits every word it parses",
    # (b) The shell renders it at every prompt, so exporting a credential over it
    # PRINTS that credential to the terminal on every line — the exact leak
    # `emit_use` exists to prevent — and it is exported, so child shells too.
    "PS1": "the prompt the shell renders, and prints, on every line",
    # (b) mien resolves its ephemeral store from it (`_env_script_dir`,
    # `EphemeralStore`). Exported over with a credential, the value is not a
    # directory: mien creates one NAMED for the credential under whatever the
    # current directory happens to be — a git worktree, say — and drops the
    # plaintext files there, where `mien doctor --gc` run from anywhere else will
    # never sweep them.
    "TMPDIR": "where mien writes the ephemeral files that carry your credentials",
    # (b) `config_path` honours it, so a credential exported over it points every
    # later mien command at a config that does not exist — `mien status` reports
    # nothing, and `mien login` writes a fresh config to a path named for the
    # credential. The `unset` half misdirects too: a shell deliberately pointed
    # at another config silently falls back to the default one.
    "MIEN_CONFIG": "where mien reads and writes the config that holds every profile",
}

# Environment markers that say an agent harness is recording this command's
# output. Presence means: anything mien writes to stdout may be captured into a
# transcript that outlives the command — so printing a raw secret there is not a
# transient exposure but a durable one. `mien token` refuses on it, and
# `mien exec` refuses a wrong identity on it. The set is a heuristic and
# deliberately fails *open*: an unrecognized harness is not detected, so this is
# a backstop for the common case, never a guarantee.
#
# This is the ONE list of marker names: `mien.cli.capture_context` reads it to
# detect a harness and `mien.config.check_custom_var_name` reads it to refuse the
# same names as `custom` credentials. A second copy would let the detection and
# the refusal drift, and a marker mien detects but does not refuse is a marker
# the scrub can `unset`.
#
# Refused as a `custom` name for a reason NEXT TO the `SHELL_CRITICAL_VARS` rule
# rather than inside it, which is why the names live in their own map with their
# own message: neither half of that rule holds here. `unset CLAUDECODE` breaks no
# shell and no loader (a), and a credential exported over it leaves the value
# non-empty — still "detected", the safe side (b). What disqualifies these is a
# third property: mien reads the variable as a safety SIGNAL whose ABSENCE is the
# permissive state, so the scrub's `unset` is what moves it to the unsafe side.
# The blast radius is the same as the other rule's — `scrub_vars` is the union
# over EVERY profile, so one such name in one profile makes every `mien use` and
# every `mien-unset`, in every shell, emit `unset CLAUDECODE` — but the damage is
# quieter: a broken `PATH` is self-evident, while a disarmed refusal shows up only
# as a secret that gets printed when you expected it to be withheld.
#
# Each name maps to what it is, because the refusal quotes the phrase: a marker
# cannot be added here (or to the detection, which is the same thing) without
# saying what sets it.
CAPTURE_MARKER_VARS: dict[str, str] = {
    "CLAUDECODE": "the marker Claude Code sets to say an agent, not a person, is driving this shell",
    "CLAUDE_CODE_ENTRYPOINT": "the marker Claude Code sets to name the agent entrypoint that is driving this shell",
    "MIEN_CAPTURED": "the marker you set yourself to tell mien this harness records what mien prints",
}


def custom_vars(profiles: Mapping[str, Profile]) -> list[str]:
    """Every `custom` variable name any profile in the config defines, sorted.

    EVERY profile, not just the one being activated: what a scrub has to clear is
    whatever the shell was carrying before, which is some other profile's set.
    Sorted so the emitted script is a function of the config's content and not of
    its key order.
    """
    return sorted({var for prof in profiles.values() for var in prof.custom})


def scrub_vars(profiles: Mapping[str, Profile]) -> list[str]:
    """Every variable a scrub must clear: the built-ins plus every custom name.

    Computed from the config at scrub time rather than baked in, because the
    custom half is the user's to name. A `custom` name may not collide with a
    built-in (`mien.config.check_custom_var_name` refuses that at both login and
    parse time), so the two halves never overlap.
    """
    return KNOWN_VARS + custom_vars(profiles)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _env_script_dir() -> Path:
    tmpdir = Path(os.environ.get("TMPDIR", "/tmp"))
    root = tmpdir / "mien"
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_env_script(bundle: EnvBundle, profiles: Mapping[str, Profile]) -> Path:
    """Write the bundle's exports into a 0600 ephemeral file and return its path.

    The file is named env-<hex>.sh so EphemeralStore.gc() — which only sweeps
    PID-prefixed files — leaves it alone. The eval'd one-liner unlinks the
    file after sourcing; orphans are swept by `mien doctor --gc`.

    The script first `unset`s every managed name, then re-exports only what this
    profile defines. Without the scrub, switching profiles in a shell that
    already activated one would leave the previous profile's variables set — a
    stale `GH_TOKEN` still exported while `mien status` reports the new profile
    as active. Unset-then-export makes `mien use <p>` yield exactly `<p>`'s
    identity, independent of whatever was active before.

    `profiles` is the whole config's profile map, not just the one being
    activated, and that is the point: the scrub list is `KNOWN_VARS` plus every
    `custom` variable ANY profile defines (see `scrub_vars`), because switching
    from a profile that defines `ANTHROPIC_API_KEY` to one that does not must
    clear it — otherwise one identity's credential survives into another.
    """
    root = _env_script_dir()
    path = root / f"env-{_secrets.token_hex(8)}.sh"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        scrub = "unset " + " ".join(scrub_vars(profiles)) + "\n"
        exports = "".join(
            f"export {k}={_shell_quote(v)}\n" for k, v in bundle.env.items()
        )
        os.write(fd, (scrub + exports).encode("utf-8"))
    finally:
        os.close(fd)
    return path


def emit_use(bundle: EnvBundle, profiles: Mapping[str, Profile]) -> str:
    """Emit a shell snippet that loads `bundle`'s exports without printing the
    values to stdout. The exports live in a 0600 ephemeral file; stdout only
    carries the source-and-delete one-liner, so a caller that forgets `eval`
    cannot leak secrets through tool-call transcripts, history, or `ps`.
    """
    path = write_env_script(bundle, profiles)
    q = _shell_quote(str(path))
    return f". {q} && rm -f {q}\n"


def emit_unset(profiles: Mapping[str, Profile]) -> str:
    """The `unset` lines that clear every variable mien manages.

    Takes the profile map for the same reason `write_env_script` does — the
    custom half of the list lives in the config — and takes it as an argument
    rather than reading the config itself so the caller owns what happens when
    the config cannot be read (`mien unset` announces and scrubs the built-ins).
    """
    return "\n".join(f"unset {v}" for v in scrub_vars(profiles)) + "\n"
