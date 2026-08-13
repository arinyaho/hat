from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import MISSING, asdict, dataclass, field
from dataclasses import fields as dc_fields
from functools import lru_cache
from pathlib import Path
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

from mien.secret_naming import (
    BUILTIN_DEFAULT,
    BUILTIN_SLACK_TOKEN,
    REQUIRED_TOKENS,
    TOKEN_SEPARATES,
    render_name,
    template_fields,
)

SCHEMA_VERSION = 1


@dataclass
class BackendConfig:
    type: str
    options: dict = field(default_factory=dict)


@dataclass
class SecretNaming:
    default: str
    slack_token: str


@dataclass
class GoogleService:
    email: str
    oauth_client_id: str
    oauth_client_secret_ref: str | None
    # Null is a real, working state, not an unset field: a gcloud-login-only
    # profile has no stored refresh token, every reader guards on it
    # (`mien env`, `mien whoami`, `mien logout`), and mien's own configs carry
    # null here. Annotated accordingly — the leaf check reads these annotations,
    # so `str` would reject a config that loads today. The key is still required
    # to be *present*: that comes from having no default, not from the type.
    refresh_token_ref: str | None
    adc_ref: str | None
    gcloud_config_name: str
    default_project: str | None


@dataclass
class GitHubService:
    username: str
    host: str
    token_ref: str | None = None
    ssh_key_path: str | None = None
    ssh_key_ref: str | None = None


@dataclass
class SlackWorkspace:
    workspace: str
    user_token_ref: str


@dataclass
class AWSService:
    region: str | None = None
    profile: str | None = None
    access_key_id_ref: str | None = None
    secret_access_key_ref: str | None = None


@dataclass
class OCIService:
    profile: str | None = None
    config_file: str | None = None


@dataclass
class AtlassianService:
    email: str
    base_url: str
    api_token_ref: str


@dataclass
class NotionService:
    api_token_ref: str


@dataclass
class ProjectEnvScope:
    match: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Profile:
    name: str
    google: GoogleService | None = None
    github: GitHubService | None = None
    slack: list[SlackWorkspace] = field(default_factory=list)
    aws: AWSService | None = None
    oci: OCIService | None = None
    atlassian: AtlassianService | None = None
    notion: NotionService | None = None
    # The user's own credentials, delivered as environment variables: each key is
    # the variable name, each value a backend REFERENCE to the secret — never the
    # secret. That is what keeps this block safe to store in config.json and to
    # upload as the shared manifest, and it is exactly where `project_env` differs
    # (its values are stored and uploaded verbatim, so it is not secret-safe).
    # A default_factory, so a config written before this field existed loads
    # unchanged with no custom variables.
    custom: dict[str, str] = field(default_factory=dict)
    project_env: list[ProjectEnvScope] = field(default_factory=list)
    # Directory globs this profile claims as its default identity. Kept separate
    # from project_env: that maps directories to environment values, this maps
    # directories to *who you are*, and the two are set independently.
    default_for: list[str] = field(default_factory=list)
    # Git remote globs this profile owns, matched against a repo's `origin` in a
    # canonical `host/path` form (scheme, `user@`, and a trailing `.git` stripped;
    # an ssh `:` normalized to `/`) — e.g. ["github.com/me/*",
    # "github.com/me-labs/*"]. This claims identity by *what the repo is* rather
    # than where it sits, so it fits repositories kept side by side with no
    # per-employer directory convention.
    owns_remotes: list[str] = field(default_factory=list)
    # The git author address a commit under this identity carries. Setting git's
    # own `user.email` is git's job (native `includeIf`); mien reads git_email
    # only for the author cross-check, so guard/statusline can warn when a
    # commit's `user.email` disagrees with the identity acting here. Set it when
    # you commit under an address none of the profile's accounts carry.
    git_email: str | None = None


@dataclass
class Config:
    schema_version: int
    secrets_backend: BackendConfig
    bootstrap: dict
    secret_naming: SecretNaming
    profiles: dict[str, Profile]


def config_path() -> Path:
    override = os.environ.get("MIEN_CONFIG")
    if override:
        return Path(override)
    home = Path(os.environ.get("HOME", str(Path.home())))
    return home / ".config" / "mien" / "config.json"


def serialize_config(cfg: Config) -> str:
    return json.dumps(_config_to_dict(cfg), indent=2, sort_keys=False)


def deserialize_config(raw: str | dict) -> Config:
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid config JSON: {exc}") from exc
    else:
        data = raw
    data = _mapping_from_raw("config", data)
    version = data.get("$schema_version", data.get("schema_version"))
    if version != SCHEMA_VERSION:
        raise ConfigError(
            f"Unsupported schema_version {version!r}; expected {SCHEMA_VERSION}")
    # After the version check, not before: a config from a future schema should
    # be told its version is unsupported, rather than blamed for a key that
    # version legitimately added.
    _reject_unknown_keys(
        "config", data, _TOP_LEVEL_KEYS, advertised=_TOP_LEVEL_KEYS_ADVERTISED)
    return _config_from_dict(data)


def load_config() -> Config | None:
    """Load the config, or None when mien is simply not set up here.

    "Not set up" means nothing is at the path — and only that. A config that is
    *there* but cannot be read (mode 0600 owned by another user, a directory in
    `MIEN_CONFIG`, a dangling symlink, an I/O error, bytes that are not text) is
    a ConfigError, not a None: the file is present and mien still cannot tell
    which identity is which, which is exactly what ConfigError means.

    Translated here rather than in each caller so every surface gets it at once
    — the fail-open ones recognize ConfigError as "I have stopped working" and
    would otherwise swallow an OSError in their catch-all and go quiet, and
    `_friendly_backend_message` renders it as an actionable error instead of an
    OSError traceback.

    Which surfaces those are is not a fixed list: `guard`, the status line and the
    prompt fail open because they annotate someone else's work rather than doing
    it, and `mien unset` and `mien status` joined them once they had to read the
    config for the names of a profile's `custom` variables (see
    `cli._profiles_for_vars` — `unset` is eval'd through a shell wrapper, so
    exiting non-zero would clear nothing at all). Every one of them announces the
    failure on stderr; none of them may go quiet.
    """
    path = config_path()
    try:
        raw = path.read_text()
    except FileNotFoundError as exc:
        # A dangling symlink reports ENOENT too, but there the operator did put
        # a config in place and its target has since gone — present-and-broken,
        # not absent, so it is announced rather than passed off as unconfigured.
        if not path.is_symlink():
            return None
        raise ConfigError(
            f"config at {path} is a symlink to a file that does not exist"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        raise ConfigError(f"cannot read config at {path}: {reason}") from exc
    return deserialize_config(raw)


def save_config(cfg: Config) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_config(cfg))
    path.chmod(0o600)


def _config_to_dict(cfg: Config) -> dict:
    profiles = {}
    for name, prof in cfg.profiles.items():
        profiles[name] = {
            "google": asdict(prof.google) if prof.google else None,
            "github": asdict(prof.github) if prof.github else None,
            # A write-side shim for OLDER readers, not a live field:
            # `SlackWorkspace` has not carried `team_id` since it was retired
            # and loading drops it (`_RETIRED_SERVICE_KEYS`).
            #
            # This JSON is also what `mien push` stores as the shared manifest,
            # and the mien reading it may be older. There `team_id` was a
            # required positional, so `SlackWorkspace(**w)` raises TypeError on
            # a block without the key: `mien sync` shows the traceback, and
            # `mien init` swallows it into "(manifest check skipped: ...)",
            # exits 0, and leaves the empty config it just wrote — every
            # identity gone, almost silently. The null keeps those readers
            # working. The two fields retired alongside it need no shim:
            # `gcloud_login_required` and `git_name` both had defaults.
            #
            # Drop this once every machine sharing a backend has upgraded past
            # the removal. Nothing in the file will tell you; it is a fact about
            # the fleet.
            "slack": [{**asdict(w), "team_id": None} for w in prof.slack],
            "aws": asdict(prof.aws) if prof.aws else None,
            "oci": asdict(prof.oci) if prof.oci else None,
            "atlassian": asdict(prof.atlassian) if prof.atlassian else None,
            "notion": asdict(prof.notion) if prof.notion else None,
            # Written only when non-empty — the same write-side shim as the
            # `team_id` null above. A mien predating this field rejects unknown
            # profile keys hard, and that ConfigError takes the WHOLE manifest
            # down, not the one profile carrying the key: `mien sync` fails,
            # `mien init` swallows it and leaves the empty config it just wrote,
            # and the remedy that failure prints is `mien push` — which uploads
            # that machine's custom-less config, emptying every profile's map
            # for everyone who syncs next.
            #
            # It cannot make a profile that USES `custom` readable by a mien
            # without the field. What it buys is that the break stops being
            # fleet-wide and is confined to the profiles using the feature.
            # Costless for current readers: `Profile.custom` has a
            # default_factory and the schema documents an absent key as `{}`.
            #
            # Drop this once no mien predating `custom` reads a manifest written
            # here — again, a fact about the fleet, not the file.
            **({"custom": dict(prof.custom)} if prof.custom else {}),
            "project_env": [asdict(s) for s in prof.project_env],
            "default_for": list(prof.default_for),
            "owns_remotes": list(prof.owns_remotes),
            "git_email": prof.git_email,
        }
    return {
        "$schema_version": cfg.schema_version,
        "secrets_backend": {"type": cfg.secrets_backend.type, **cfg.secrets_backend.options},
        "bootstrap": cfg.bootstrap,
        "secret_naming": asdict(cfg.secret_naming),
        "profiles": profiles,
    }


def _glob_string(must_be: str, value: object) -> str:
    """Validate one glob string -- the unit a list-of-globs field is made of and
    a ``project_env`` entry's ``match`` is.

    ``must_be`` is the whole "<where> must be ..." clause rather than its parts,
    so a caller checking every entry of a list can speak in the plural and one
    checking a single value in the singular, while the "got <type>: <value>"
    tail they share cannot drift apart. Every glob in the config is read by
    ``fnmatch``/``re`` as text (``mien.resolve``, ``mien.ambient``), so a
    wrong-typed one escapes as ``TypeError``/``AttributeError`` from whichever
    command touches it first -- which the fail-open surfaces do not recognize as
    a config failure at all. See ``_mapping_from_raw``.
    """
    if not isinstance(value, str):
        raise ConfigError(f"{must_be}, got {type(value).__name__}: {value!r}")
    return value


def _glob_list_from_raw(
    profile_name: str, field_name: str, kind: str, value: object, example: str
) -> list[str]:
    """Validate a profile's list-of-globs field instead of coercing it.

    A bare string would otherwise be exploded into one glob per character by
    ``list()``, and the resulting ``"*"`` element claims everything -- silently
    routing credentials to the wrong profile. Shared by ``default_for``
    (``kind="directory"``) and ``owns_remotes`` (``kind="remote"``).
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(
            f"profile {profile_name!r}: {field_name} must be a list of {kind} glob "
            f"strings (e.g. [{example!r}]), got {type(value).__name__}: {value!r}"
        )
    for item in value:
        _glob_string(
            f"profile {profile_name!r}: {field_name} entries must be {kind} glob "
            f"strings",
            item,
        )
    return list(value)


def _mapping_from_raw(where: str, value: object) -> dict:
    """Validate that a config block is a JSON object instead of assuming it is.

    Every block below is read with ``.items()``, ``in``, or ``dict()``, so a
    wrong-typed value would escape as ``TypeError``/``AttributeError`` rather
    than ``ConfigError`` -- and the fail-open surfaces (guard, status line) only
    recognize ``ConfigError`` as "I have stopped working", so anything else exits
    in silence. Same shape-check-don't-coerce rule as ``_glob_list_from_raw``:
    every value reaching here is checked, never coerced, so a block that may be
    absent goes through ``_optional_mapping_from_raw`` rather than being turned
    into ``{}`` by the caller before it gets here.
    """
    if not isinstance(value, dict):
        raise ConfigError(
            f"{where} must be a JSON object, got {type(value).__name__}: {value!r}"
        )
    return value


def _optional_mapping_from_raw(where: str, value: object) -> dict:
    """Read a block that defaults to empty when it is absent, shape-checked.

    Absence is the only thing that means "empty": ``value is None`` (the key is
    missing, or explicitly null) yields ``{}``, and everything else goes to
    ``_mapping_from_raw`` and keeps its shape error. The ``or {}`` this replaces
    coerced *before* the check, so a falsy wrong-typed block (``"profiles": []``,
    ``"secret_naming": []``) was silently read as empty — zero profiles, or the
    built-in secret-name templates back in force, deciding where secrets live
    with no error anywhere. Same absent-is-None rule as ``_glob_list_from_raw``,
    ``_object_list_from_raw``, and ``_service_from_raw``, so the answer no longer
    depends on how deeply the block is nested. Present-but-empty (``{}``) still
    parses exactly as absence does: an empty block asks for nothing, it is not a
    truncated one.
    """
    if value is None:
        return {}
    return _mapping_from_raw(where, value)


def _object_list_from_raw(where: str, value: object) -> list[dict]:
    """Validate a config block that is a list of JSON objects. See above."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(
            f"{where} must be a list of JSON objects, got "
            f"{type(value).__name__}: {value!r}"
        )
    return [_mapping_from_raw(f"{where}[{i}]", item) for i, item in enumerate(value)]


# What may stand on the left of `export NAME=...`. Deliberately ASCII-only and
# stricter than `str.isidentifier()` (which accepts `café` and other non-ASCII
# names): this describes what a shell will take, not what Python will.
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _check_env_name(where: str, key: object, example: str) -> None:
    """One rule for what may stand on the left of ``export NAME=...``.

    Shared by every block whose keys become environment variables -- a
    ``project_env`` entry's ``env`` and a profile's ``custom`` -- so a name one
    accepts is a name the other accepts, and the wording cannot drift. ``example``
    is each caller's own, because a good example for one is a *refused* name for
    the other: ``AWS_PROFILE`` is the canonical ``project_env`` key and a
    collision with mien's built-in aws variable as a ``custom`` name.

    A non-string key cannot come from JSON, whose keys are always strings, but
    ``deserialize_config`` also accepts an already-parsed ``dict``; the
    ``isinstance`` here is what keeps the check itself from dying on one.
    """
    if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key):
        raise ConfigError(
            f"{where}: {key!r} is not a usable environment variable name. "
            f"Expected letters, digits and underscores, not starting with a "
            f"digit (e.g. {example!r})."
        )


def check_custom_var_name(where: str, name: object) -> None:
    """Validate one ``custom`` variable name, for both readers of the rule.

    Called from the config parser AND from ``mien login --service custom --name``,
    so a name the CLI accepts can never be one the parser then refuses -- which
    would be a config mien wrote and mien cannot load. ``where`` is the caller's
    own label (``profile 'work': custom``, ``--name``) so the message reads right
    in a config error and in a flag error alike.

    Four ways a name is refused:

    - **Not a shell identifier.** ``mien use`` writes a script that gets sourced,
      so a malformed name breaks the loader itself -- worse here than in
      ``project_env``, since the same script carries every variable the profile
      exports and sourcing abandons the file at the failing line.
    - **Already a built-in's variable.** ``build_env`` fills one dict, built-ins
      first and customs last, so a collision would let the custom value quietly
      overwrite the profile's real ``GH_TOKEN`` -- and would do the opposite if
      the order were ever reversed. Which of the two credentials a shell ends up
      carrying must not be settled by statement order, so it is refused and the
      message names the service it would fight.
    - **Shell-critical.** The loader ``unset``s every managed variable and
      re-``export``s the active profile's, and the scrub list is the union over
      every profile (``mien.shell.scrub_vars``). A name the shell or mien itself
      reads as an instruction -- ``PATH``, ``HOME``, ``TMPDIR`` and the rest of
      ``SHELL_CRITICAL_VARS`` -- therefore wrecks shells that have nothing to do
      with the profile that named it: one such name in one profile makes every
      ``mien use`` and every ``mien-unset`` strip it, everywhere. The exact-match
      here is the whole rule; ``MY_PATH`` and ``PATH_TO_KEY`` are ordinary names.
    - **An agent-harness capture marker.** Same blast radius, different reason,
      which is why ``mien.shell.CAPTURE_MARKER_VARS`` is its own map and gets its
      own message: neither half of the shell-critical rule holds (``unset
      CLAUDECODE`` breaks nothing, and a credential exported over it still reads
      as "detected"). What disqualifies it is polarity -- mien reads the marker as
      a safety signal whose ABSENCE is the permissive state, so the scrub's
      ``unset`` is what moves it to the unsafe side, silently disarming the
      refusals in ``mien token`` and ``mien exec``.

    Matching is exact and case-sensitive throughout, like the environment itself.
    """
    _check_env_name(where, name, "ANTHROPIC_API_KEY")
    # Imported here, not at module scope: `mien.shell` imports `mien.env`, which
    # imports this module, so a top-level import would be a cycle. Same shape as
    # `ensure_known_backend_options` below, and reached only for a config that
    # actually declares a custom variable.
    from mien.shell import (
        BUILTIN_VARS,
        CAPTURE_MARKER_VARS,
        MIEN_INTERNAL_OWNER,
        SHELL_CRITICAL_VARS,
    )
    owner = BUILTIN_VARS.get(name)
    if owner:
        # No `--service` to point at for mien's own bookkeeping variables, so the
        # remedy stops at "pick another name" rather than naming a command that
        # does not exist.
        remedy = "Pick another name." if owner == MIEN_INTERNAL_OWNER else (
            f"Pick another name, or use `mien login <profile> --service {owner} "
            f"...` if that built-in is what you meant."
        )
        raise ConfigError(
            f"{where}: {name!r} is the environment variable mien already uses for "
            f"{owner}. A custom credential cannot share a name with a built-in "
            f"one — which of the two a shell ends up carrying must not be decided "
            f"silently. {remedy}"
        )
    critical = SHELL_CRITICAL_VARS.get(name)
    if critical:
        raise ConfigError(
            f"{where}: {name!r} is {critical}. A custom credential cannot take "
            f"that over: `mien use` unsets every variable mien manages and "
            f"re-exports the active profile's, and that list is the union over "
            f"ALL profiles — so this name would strip {name} in every shell that "
            f"runs `mien use` or `mien-unset`, whichever profile is active. Pick "
            f"another name."
        )
    marker = CAPTURE_MARKER_VARS.get(name)
    if marker:
        raise ConfigError(
            f"{where}: {name!r} is {marker}. mien reads it to decide whether an "
            f"agent is driving, and reads its ABSENCE as 'no agent' — so a custom "
            f"credential cannot take it over: `mien use` unsets every variable "
            f"mien manages, and that list is the union over ALL profiles, so this "
            f"name would emit `unset {name}` in every shell that runs `mien use` "
            f"or `mien-unset`, whichever profile is active. That disarms the "
            f"refusals that keep `mien token` from printing a secret into a "
            f"recorded transcript and `mien exec` from running under the wrong "
            f"identity, and it disarms them silently. Pick another name."
        )


_BUILTIN_TEMPLATE = {"default": BUILTIN_DEFAULT, "slack_token": BUILTIN_SLACK_TOKEN}


def _check_secret_name_template(key: str, template: str) -> None:
    """Require a secret-name template to distinguish what it names.

    ``secret_naming`` is user-editable, and every ``mien login`` renders one of
    its two templates to decide WHERE a credential is stored. Nothing else keeps
    those names apart: the "one credential, one secret" property comes entirely
    from the template spending every token it is rendered with. Drop one --
    ``"mien-{profile}-{service}"`` is a plausible hand-edit, and a manifest pulled
    from a shared backend can carry it too -- and two credentials render the same
    name. `mien login` then stores the second over the first with nothing said,
    both config entries point at the survivor, and `mien logout` of either deletes
    the secret the other still references, leaving a profile that cannot be
    activated at all. That is the same silent credential destruction the
    verbatim-`kind` rule exists to prevent, reached through a config field instead
    of through case folding; which of two credentials a shell ends up carrying
    must not be decided silently either way.

    Two rules, checked in this order, and NEITHER SUBSUMES THE OTHER -- do not
    delete one as redundant:

    1. STRUCTURE (this template maps two credentials onto one name). Every token
       the template is rendered with must appear as a *plain* replacement field:
       no conversion, no format spec, no attribute access, no indexing. This is
       injectivity, and it is why a probe render is not the check: injectivity is
       a property of every possible input, so probing ``kind="a"`` against
       ``kind="b"`` waves through ``"{profile}-{service}-{kind:.1s}"``, and
       ``{kind:.0s}`` renders perfectly while producing one name for everything --
       byte-identical to dropping ``{kind}``. A plain field, by contrast, is
       injective in that token by construction. See
       ``secret_naming.template_tokens`` for why every decorated form is either
       useless or a collision.

    2. RENDERABILITY (this template cannot produce a name at all). Every field the
       template asks for must be one mien actually substitutes into it, and here a
       probe render IS the check -- a set difference on the field names is not. A
       nested spec hides an unknown token where the parser's top-level walk never
       sees it (``{kind:{w}}``), and ``{}`` / ``{0}`` pass any name test and then
       raise from ``format_map``. Rule 1 already refuses every spec, so what the
       probe is left holding is the positional forms and any token that belongs to
       the other template -- ``"...-{kind}-{workspace}"`` in ``default``. Without
       it those escape ``deserialize_config`` as a bare ``KeyError``/``ValueError``
       that no fail-open surface recognizes as a config failure, and, because
       ``cli._reject_reserved_secret_name`` renders BOTH templates on every login,
       a bad ``slack_token`` alone takes down ``mien login --service github``.

    So: a probe cannot establish injectivity (rule 1's job), and a structural walk
    cannot establish renderability (rule 2's job). The messages are separate too,
    because dropping a token and naming a token that does not exist are different
    mistakes with different fixes.

    Checked at parse time, so it covers every way a template arrives: a
    hand-edited config file, and `mien sync`/`mien init`'s import of the shared
    backend manifest, which goes through this same parser. `mien init` needs no
    check of its own -- it writes the built-in templates and offers no way to type
    one.
    """
    required = REQUIRED_TOKENS[key]
    expected = ", ".join("{%s}" % t for t in required)
    try:
        fields = template_fields(template)
    except ValueError as exc:
        # An unbalanced brace. Reported here rather than left to `render_name`,
        # where it surfaces as a bare ValueError out of whichever command first
        # stores or reads a secret -- and, since this check now runs on load,
        # would otherwise escape `deserialize_config` as a non-ConfigError that
        # the fail-open surfaces do not recognize as a config failure.
        raise ConfigError(
            f"secret_naming: {key!r} template {template!r} is not a usable "
            f"template ({exc}) — mien cannot render a secret name from it. "
            f"Expected {expected} in some arrangement, e.g. "
            f"{_BUILTIN_TEMPLATE[key]!r}."
        ) from exc

    # Rule 1: every required token spent as a bare `{name}`, and no decorated
    # field anywhere. Refused outright rather than merely not counted, so that an
    # accepted template is literal text plus plain fields and nothing else.
    plain = {f.name for f in fields if f.plain}
    decorated = ", ".join(f.text for f in fields if not f.plain)
    missing = [t for t in required if t not in plain]
    if missing:
        raise ConfigError(
            f"secret_naming: {key!r} template {template!r} does not use "
            f"{', '.join('{%s}' % t for t in missing)} as a plain replacement "
            f"field, so {'; '.join(TOKEN_SEPARATES[t] for t in missing)}. Two "
            f"credentials then land on one backend secret: the second `mien "
            f"login` overwrites the first with nothing said, and a later `mien "
            f"logout` of either deletes the secret the other one still points at."
            + (
                f" {decorated} does not count as spending the token: a format "
                f"spec, a conversion, attribute access or indexing renders "
                f"something shorter or different, and a shortened token is "
                f"exactly how two credentials collapse — `{{kind:.1s}}` keeps one "
                f"character of the name and `{{kind:.0s}}` keeps none."
                if decorated
                else ""
            )
            + f" Every token mien renders this template with has to appear in it "
            f"as a bare `{{name}}` — {expected} — in whatever arrangement you "
            f"like. The built-in template is {_BUILTIN_TEMPLATE[key]!r}."
        )
    if decorated:
        raise ConfigError(
            f"secret_naming: {key!r} template {template!r} spends {decorated} "
            f"through a format spec, a conversion, attribute access or indexing. "
            f"Only a plain replacement field — a bare `{{name}}` — reproduces the "
            f"token mien renders it with, and mien refuses the decorated forms "
            f"rather than quietly ignoring them: the tokens are already strings, "
            f"so `:s` is a no-op, `!r` wraps the name in quotes, a width or fill "
            f"pads it with spaces, and a precision truncates it (`{{kind:.1s}}` "
            f"keeps one character of the name, `{{kind:.0s}}` keeps none) — a "
            f"truncated token renders one secret name for two credentials. Write "
            f"{expected} as bare fields, plus whatever literal text you like. The "
            f"built-in template is {_BUILTIN_TEMPLATE[key]!r}."
        )

    # Rule 2: nothing in the template that mien has no value for. The probe render
    # uses the token names as their own values -- only renderability is at stake
    # here, never the shape of the result.
    try:
        render_name(template, **{t: t for t in required})
    except Exception as exc:  # KeyError, ValueError, and whatever a field invents
        detail = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        raise ConfigError(
            f"secret_naming: {key!r} template {template!r} asks for a field mien "
            f"does not substitute into it, so it cannot render a secret name at "
            f"all ({detail}). mien renders this template with exactly {expected} "
            f"— nothing else — so a positional field (`{{}}`, `{{0}}`), a token "
            f"that belongs to the other `secret_naming` template, or one hidden "
            f"in a nested format spec has no value to stand for. Left in, it "
            f"takes down every `mien login`, this template's credentials and the "
            f"other's alike, with a bare exception instead of this message. Use "
            f"only {expected}. The built-in template is "
            f"{_BUILTIN_TEMPLATE[key]!r}."
        ) from exc


def _custom_map_from_raw(profile_name: str, value: object) -> dict[str, str]:
    """Validate a profile's ``custom`` map -- shape, names AND values.

    ``custom`` is a plain dict rather than a dataclass, so none of the
    block-level machinery above reaches inside it: without this, a hand-edited
    ``"custom": {"2FA": 5}`` would parse clean and then break the loader script
    ``mien use`` writes. Values are backend references, checked only for being
    strings -- a non-string one dies in ``backend.get`` deep inside ``mien use``,
    long after the config that named it.
    """
    where = f"profile {profile_name!r}: custom"
    env = _optional_mapping_from_raw(where, value)
    for key, item in env.items():
        check_custom_var_name(where, key)
        if not isinstance(item, str):
            raise ConfigError(
                f"{where}: {key!r} must be a backend secret reference string "
                f"(not the secret itself), got {type(item).__name__}: {item!r}"
            )
    return dict(env)


def _env_map_from_raw(where: str, value: object) -> dict[str, str]:
    """Validate a ``project_env`` entry's ``env`` map -- shape, keys AND values.

    ``_optional_mapping_from_raw`` establishes only that the block is a JSON
    object. Every pair inside it is then written into ``ambient.zsh`` verbatim as
    ``export <key>=<value>`` (``mien.ambient._scope_block``), so both halves have
    to be checked here, for the same reason the sibling ``match`` goes through
    ``_glob_string`` rather than being trusted:

    - A non-string VALUE dies in ``ambient._emit_value`` as a bare
      ``AttributeError`` (``.replace`` on an int), which ``env_sync_cmd`` does
      not catch and ``MienGroup`` does not translate: ``mien env sync`` exits 1
      with a raw traceback on stderr rather than the actionable ``Error:`` line
      every other config fault gets, and neither ``ambient.zsh`` nor
      ``~/.zshenv`` is touched. An unquoted number (``{"PORT": 8080}``)
      is a plausible hand-edit, and the packaged schema declares this field
      ``dict[str, str]``.
    - A KEY that is not a shell identifier survives every gate downstream.
      ``zsh -n`` PARSES ``export 2FA="x"`` (it is a run-time error, not a syntax
      one), so ``assert_parses`` passes and the file is written -- and then
      sourcing it fails at that line with ``not an identifier`` and ABANDONS THE
      REST OF THE FILE: every later export, including every other profile's
      scopes, silently never happens. A key with a space is quieter still --
      ``export MY VAR="x"`` is valid syntax that exports ``MY`` empty and
      ``VAR="x"``, so the variable the operator wrote is never set. Neither says
      a word anywhere, which is the wrong-account-in-silence ending this parser
      exists to prevent.

    The name half is ``_check_env_name``, shared with ``custom``.
    """
    env = _optional_mapping_from_raw(where, value)
    for key, item in env.items():
        _check_env_name(where, key, "AWS_PROFILE")
        if not isinstance(item, str):
            raise ConfigError(
                f"{where}: {key!r} must be a string, got "
                f"{type(item).__name__}: {item!r}"
            )
    return dict(env)


# The JSON types a leaf config value can be annotated with, and how a message
# names each one. A field annotated with anything else -- a nested dataclass, a
# service block -- is not a leaf and is built and validated by its own pass.
_LEAF_TYPE_NAMES: dict[type, str] = {
    str: "a string",
    bool: "a boolean",
    int: "an integer",
    float: "a number",
    list: "a list",
    dict: "a JSON object",
}


def _leaf_spec(annotation: object) -> tuple[tuple[type, ...], str] | None:
    """Read one field annotation as (accepted types, how to say them), or None.

    None means "not a leaf": ``GoogleService | None`` names a block that
    ``_service_from_raw`` already validates, not a value to type-check here.
    ``str | None`` keeps accepting ``None`` because ``NoneType`` is one of its
    arguments; a bare ``str`` does not, which is the whole distinction the
    annotations already record.
    """
    origin = get_origin(annotation)
    args = get_args(annotation) if origin in (Union, UnionType) else (annotation,)
    types: list[type] = []
    names: list[str] = []
    nullable = False
    for arg in args:
        if arg is type(None):
            nullable = True
            continue
        base = get_origin(arg) or arg  # dict[str, str] -> dict, list[str] -> list
        if base not in _LEAF_TYPE_NAMES:
            return None
        types.append(base)
        if _LEAF_TYPE_NAMES[base] not in names:
            names.append(_LEAF_TYPE_NAMES[base])
    if not types:
        return None
    if nullable:
        types.append(type(None))
        names.append("null")
    return tuple(types), " or ".join(names)


@lru_cache(maxsize=None)
def _leaf_specs(cls: type, scalars_only: bool = False) -> dict[str, tuple[tuple[type, ...], str]]:
    """The leaf fields of a dataclass and the type each one accepts.

    Derived from the annotations rather than hand-listed, so a field added to a
    dataclass below is checked from the moment it exists and the check cannot
    drift from the declaration. ``from __future__ import annotations`` makes
    every annotation a string, hence ``get_type_hints`` rather than
    ``f.type``. Cached because the answer is a property of the class.

    ``scalars_only`` drops the ``list``/``dict`` fields, for callers whose
    container fields each have a dedicated validator with a more useful message
    (``default_for`` says "a list of directory glob strings", not "a list").
    """
    hints = get_type_hints(cls)
    specs = {}
    for f in dc_fields(cls):
        spec = _leaf_spec(hints[f.name])
        if spec is None:
            continue
        if scalars_only and any(t in (list, dict) for t in spec[0]):
            continue
        specs[f.name] = spec
    return specs


def _check_leaf_values(
    label: str, data: dict, cls: type, *, scalars_only: bool = False
) -> None:
    """Validate the field VALUES of one config block, not just its shape.

    The shape checks above stop at the block: they establish that ``github`` is
    an object and ``slack`` a list of objects, and then hand the leaves straight
    to the dataclass, which takes whatever it is given. ``"git_email": 123`` or
    ``"username": 42`` therefore parsed clean and died much later, wherever the
    value was first used as text -- as ``AttributeError``/``TypeError``, which
    the fail-open surfaces do not recognize as a config failure. The status line
    rendered blank and ``mien guard`` exited 0 with nothing on either stream: a
    commit under the wrong identity, waved through in silence. Same rule as
    everywhere else here, applied one level deeper -- check, never coerce, and
    fail as ``ConfigError`` so the surfaces can say they have stopped working.

    Only keys that are present are checked; an absent one is the caller's
    missing-key check or a dataclass default, both of which have their own say.
    """
    for name, (types, expected) in _leaf_specs(cls, scalars_only).items():
        if name not in data:
            continue
        value = data[name]
        # `isinstance(True, int)` is True, so a bool is only accepted where the
        # annotation actually says bool.
        if isinstance(value, types) and not (isinstance(value, bool) and bool not in types):
            continue
        raise ConfigError(
            f"{label}: {name!r} must be {expected}, got "
            f"{type(value).__name__}: {value!r}"
        )


class ConfigError(ValueError):
    """The config file cannot be understood.

    Distinct from a plain ValueError so the CLI can report it as an actionable
    error instead of a traceback, and so the fail-open surfaces (status line,
    guard) can say that they have stopped working rather than going quiet.
    """


def _reject_unknown_keys(
    label: str,
    data: dict,
    known: Iterable[str],
    *,
    advertised: Iterable[str] | None = None,
    tolerated: Iterable[str] = (),
) -> None:
    """Reject a key in one config block that mien does not recognize.

    Every block gets the same treatment through this one function so the four
    messages cannot drift apart: `label` names the block precisely (`config`,
    `secret_naming`, `profile 'work'`, `profile 'work': project_env entry
    '*/work'`, `profile 'work': aws`), and the message always names the offending
    key and lists what is valid. `advertised` is the set the message suggests
    when it differs from what is accepted -- an accepted alias should not be
    recommended. `tolerated` is accepted silently and never advertised: keys an
    older mien wrote that nothing reads any more.

    Unrecognized means *rejected*, at every level, because every level has a way
    to lose an identity in silence:

    - top level: `"profles"` yields a config with zero profiles and no error, so
      every identity vanishes and `mien which` resolves to nothing; `"bootstrp"`
      empties the bootstrap account the GCP backend reports in its "you are
      logged in as someone else" diagnostics.
    - `secret_naming`: a typo'd template key falls back to the built-in
      template, which changes *where secrets live* -- `mien login` writes under
      the built-in name while the operator believes the config's name is in
      force, and secrets already stored under the intended name become
      unreachable.
    - profile: `defualt_for` simply drops the directory claim, and the directory
      then falls to some other profile's catch-all glob.
    - `project_env` entry: `{"match": "*/work", "envs": {...}}` builds a scope
      with an empty `env`, so the scope matches and exports nothing -- no
      `AWS_PROFILE`, the tool falls back to its own default account, and the
      command succeeds as somebody else.
    - service block: a dropped `profile`/`ssh_key_path` leaves the service
      unconfigured, with the same wrong-account ending.

    Nothing warns in any of those cases, and the fail-open surfaces (`guard`,
    `statusline`, `prompt`) stay silent too, so all of them fail loudly instead.
    """
    known = set(known)
    tolerated = set(tolerated)
    valid = ", ".join(sorted(known if advertised is None else set(advertised)))
    for k in data:
        if k in known or k in tolerated:
            continue
        raise ConfigError(f"{label}: unknown key {k!r}. Valid keys are: {valid}.")


# Profile-level keys an older config may still carry. Tolerated by name alone,
# whatever value they hold: `git_name` was never read by any code path, so a
# config that set it to a real name was already getting nothing from it, and
# rejecting such a config now would break configs that worked. Still an explicit
# list rather than "drop anything unrecognized" — see `_reject_unknown_keys` for
# why an unknown profile key must fail loudly.
_RETIRED_PROFILE_KEYS: frozenset[str] = frozenset({"git_name"})


# Top-level keys mien reads. `$schema_version` is the spelling `_config_to_dict`
# writes; `schema_version` is the older, unprefixed one `deserialize_config`
# still accepts, so it has to be accepted here too — rejecting it would make a
# config that loads today fail to load.
_TOP_LEVEL_KEYS: frozenset[str] = frozenset({
    "$schema_version",
    "schema_version",
    "secrets_backend",
    "bootstrap",
    "secret_naming",
    "profiles",
})

# What the error message advertises: the alias is accepted but not suggested.
_TOP_LEVEL_KEYS_ADVERTISED: frozenset[str] = _TOP_LEVEL_KEYS - {"schema_version"}


@dataclass(frozen=True)
class _RetiredKey:
    """A service key an older mien wrote, and what deleting it costs today.

    `no_op_value` is the value the key had to hold for its removal to change
    nothing. `remedy` is the part the caller cannot guess: a retirement that
    removed a *capability* makes "just delete the key" a behaviour change, so
    each key has to spell out what deleting it does and how to get the old
    behaviour back. "Remove it from your config" is only honest advice when the
    key really was inert.
    """

    no_op_value: object
    remedy: str


# Keys a service block may still carry from an older mien. Deliberately an
# explicit map rather than "drop anything unrecognized": an unknown key is far
# more likely a typo (`profil`, `ssh_keypath`) than a retired field, and silently
# dropping it would leave the service unconfigured — no
# `AWS_PROFILE`/`OCI_CLI_PROFILE` exported, the tool falling back to its own
# default account, and the command succeeding as somebody else. A misconfigured
# identity has to fail loudly.
_RETIRED_SERVICE_KEYS: dict[type, dict[str, _RetiredKey]] = {
    GoogleService: {
        "gcloud_login_required": _RetiredKey(
            no_op_value=False,
            remedy=(
                "The ADC suppression it controlled has been removed from mien: "
                "nothing reads\n"
                "this key any more, and there is no other setting you might have "
                "meant.\n\n"
                "Deleting the key is therefore not a no-op. mien would start "
                "writing an\n"
                "ephemeral ADC file for this profile and exporting "
                "GOOGLE_APPLICATION_CREDENTIALS\n"
                "— the export this profile asked it not to make.\n\n"
                "To move forward:\n"
                "  - If that export is fine, delete 'gcloud_login_required' and "
                "nothing else\n"
                "    changes.\n"
                "  - To keep this profile ADC-free, delete 'gcloud_login_required' "
                "AND set this\n"
                "    google block's 'oauth_client_secret_ref' to null. mien writes "
                "an ADC only\n"
                "    when both 'oauth_client_secret_ref' and 'refresh_token_ref' "
                "are set, so\n"
                "    clearing it leaves the gcloud-login-only google that "
                "'gcloud_login_required:\n"
                "    true' described. `gcloud` still runs under this profile's "
                "config; what\n"
                "    stops working is `mien token google` and `mien whoami "
                "--live`'s google\n"
                "    probe, neither of which works for a gcloud-login-only profile "
                "anyway.\n\n"
                "    Nulling the ref also strands the secret it named. `mien logout "
                "--service\n"
                "    google` deletes that secret only when "
                "'oauth_client_secret_ref' is set, and\n"
                "    it drops the whole google block as it goes — so once the ref "
                "is null the\n"
                "    stored OAuth client secret stays in the backend with nothing "
                "left pointing\n"
                "    at it. Write the ref's current value down and delete that "
                "secret from your\n"
                "    backend yourself before you null the key, or accept the orphan "
                "and clean it\n"
                "    up later."
            ),
        ),
    },
    SlackWorkspace: {
        "team_id": _RetiredKey(
            no_op_value=None,
            remedy=(
                "Nothing has ever read it — mien addresses a workspace by its "
                "'workspace' label —\n"
                "so deleting the key changes nothing. mien only ever wrote it as "
                "null, so a real\n"
                "team id here was typed by hand: check it was not meant to be a "
                "different key\n"
                "(valid keys: workspace, user_token_ref) before you delete it."
            ),
        ),
    },
}


def _service_from_dict(cls, data: dict, label: str):
    """Build a service dataclass, tolerating only known-retired keys.

    A retired key is dropped when it holds the value that made it a no-op. Any
    other value meant something once, so it is reported rather than ignored —
    silently discarding it would change behaviour without saying so. The report
    carries that key's own remedy, because for a key whose capability was removed
    the obvious fix (delete it) *is* the silent behaviour change this raise
    exists to prevent.

    `label` is the caller's own name for this block (`profile 'work': aws`,
    `profile 'work': slack[1]`) and prefixes every message here, so a bad key
    inside a service block reads like every other config error and names the
    profile to go and edit. The dataclass name alone does not: with several
    profiles carrying an `aws` block, `AWSService: ...` leaves the reader to
    guess which one.
    """
    retired = _RETIRED_SERVICE_KEYS.get(cls, {})
    cleaned = {}
    for k, v in data.items():
        if k in retired:
            if v != retired[k].no_op_value:
                raise ConfigError(
                    f"{label}: {k!r}={v!r} is no longer supported "
                    f"(only {retired[k].no_op_value!r} was ever written).\n\n"
                    f"{retired[k].remedy}"
                )
            continue
        cleaned[k] = v
    fields = dc_fields(cls)
    _reject_unknown_keys(label, cleaned, {f.name for f in fields})
    # A missing required field is the same class of error as an unknown one: the
    # block is present, so it was meant to configure something, and half a block
    # configures the wrong identity just as quietly as a typo'd key does. Named
    # here rather than left to `cls(**cleaned)`, whose TypeError talks about
    # positional arguments the config file does not have.
    missing = [
        f.name for f in fields
        if f.name not in cleaned
        and f.default is MISSING and f.default_factory is MISSING
    ]
    if missing:
        raise ConfigError(
            f"{label}: missing required key{'s' if len(missing) > 1 else ''} "
            f"{', '.join(repr(m) for m in missing)}. Valid keys are: "
            f"{', '.join(sorted(f.name for f in fields))}."
        )
    _check_leaf_values(label, cleaned, cls)
    try:
        return cls(**cleaned)
    except TypeError as exc:
        # Unreachable while the checks above cover every way `cls(**cleaned)` can
        # reject its arguments; kept as the backstop that holds the module's one
        # invariant if a future dataclass grows a new one. Nothing may leave here
        # as a bare TypeError: the fail-open surfaces recognize only ConfigError
        # as "I have stopped working" and would exit in silence on anything else.
        raise ConfigError(
            f"{label}: {exc}. Valid keys are: "
            f"{', '.join(sorted(f.name for f in fields))}."
        ) from exc


def _service_from_raw(cls, profile_name: str, key: str, p: dict):
    """Build an optional service block, checking its shape first.

    Absent-or-null means "this profile has no such service"; anything else is a
    block to validate. Present-but-empty is *not* absent: `"google": {}` is a
    truncated block, and short-circuiting on falsiness dropped the service
    without a word — the profile then acts with no google at all, which is the
    silent identity loss this parser exists to prevent. `{}` now goes through the
    normal checks, so a service whose fields are all optional (`aws`, `oci`)
    still parses and one with required fields reports what is missing. A
    present-but-wrong-typed block (`"github": false`, `"aws": []`) reaches
    `_mapping_from_raw` and keeps its shape error, which falsiness also used to
    swallow.
    """
    value = p.get(key)
    if value is None:
        return None
    label = f"profile {profile_name!r}: {key}"
    return _service_from_dict(cls, _mapping_from_raw(label, value), label)


def _config_from_dict(raw: dict) -> Config:
    sb_raw = dict(
        _optional_mapping_from_raw("secrets_backend", raw.get("secrets_backend")))
    if "type" not in sb_raw:
        raise ConfigError(
            "secrets_backend.type is missing — mien cannot tell where your "
            "secrets live. Expected one of: gcp_secret_manager, macos_keychain, "
            "keyring.")
    # `type` was the one leaf that never reached `_check_leaf_values`: popped
    # out of the block and handed straight to `BackendConfig`, whatever it held.
    # A non-string one escaped `ensure_known_backend_options` as a bare
    # `TypeError: unhashable type: 'list'` out of `deserialize_config`, which
    # every surface calls — so `mien guard` exited 0 with both streams empty and
    # waved through the mis-identity commit it exists to block. Checked here
    # against the same annotation as every other leaf, so it cannot drift from
    # `BackendConfig.type: str`. `scalars_only` skips `options`.
    #
    # TYPE only. A string that is not a backend mien has ("keychain", the
    # retired "oci_vault") belongs to `ensure_known_backend`, which carries the
    # migration story and is deferred to the first command reaching for the
    # backend. `null` and `5` are rejected here instead, because "unknown
    # secrets backend 5" would send the reader looking for a backend called 5,
    # and splitting them off would make the user-visible contract depend on
    # whether a value happens to be hashable.
    _check_leaf_values("secrets_backend", sb_raw, BackendConfig, scalars_only=True)
    sb_type = sb_raw.pop("type")
    secrets_backend = BackendConfig(type=sb_type, options=sb_raw)
    # Imported here, not at module scope: `mien.backends` imports this module, so
    # a top-level import would be a cycle. By parse time it is already loadable,
    # and the module is cheap — every concrete backend inside it is lazy.
    from mien.backends import ensure_known_backend_options
    ensure_known_backend_options(secrets_backend)

    sn = _optional_mapping_from_raw("secret_naming", raw.get("secret_naming"))
    _reject_unknown_keys("secret_naming", sn, {f.name for f in dc_fields(SecretNaming)})
    # A template is `.format(...)`ed to decide where a secret lives, so a
    # non-string one dies as an AttributeError deep inside `mien login`/`token`
    # rather than here. `null` counts: `sn.get(key, default)` returns the null,
    # not the built-in template.
    _check_leaf_values("secret_naming", sn, SecretNaming)
    secret_naming = SecretNaming(
        default=sn.get("default", BUILTIN_DEFAULT),
        slack_token=sn.get("slack_token", BUILTIN_SLACK_TOKEN),
    )
    _check_secret_name_template("default", secret_naming.default)
    _check_secret_name_template("slack_token", secret_naming.slack_token)

    profiles: dict[str, Profile] = {}
    for name, p in _optional_mapping_from_raw("profiles", raw.get("profiles")).items():
        p = _mapping_from_raw(f"profile {name!r}", p)
        _reject_unknown_keys(
            f"profile {name!r}", p,
            {f.name for f in dc_fields(Profile)} - {"name"},
            tolerated=_RETIRED_PROFILE_KEYS,
        )
        # The profile's own scalar leaves, `git_email` today. Its list and dict
        # fields are skipped here because each already has a validator that says
        # more than "a list"/"a JSON object" (`_glob_list_from_raw`,
        # `_object_list_from_raw`, `_custom_map_from_raw`); a new scalar profile
        # field is covered the moment it is declared.
        _check_leaf_values(f"profile {name!r}", p, Profile, scalars_only=True)
        google = _service_from_raw(GoogleService, name, "google", p)
        github = _service_from_raw(GitHubService, name, "github", p)
        slack = [
            _service_from_dict(SlackWorkspace, w, f"profile {name!r}: slack[{i}]")
            for i, w in enumerate(
                _object_list_from_raw(f"profile {name!r}: slack", p.get("slack")))
        ]
        # `build_env` keys the token map by workspace name, so a repeated name
        # silently drops the earlier entry's token — the wrong-identity failure
        # this tool must never take quietly. Refused here rather than resolved.
        if dupes := sorted({w.workspace for w in slack
                            if [x.workspace for x in slack].count(w.workspace) > 1}):
            raise ConfigError(
                f"profile {name!r}: slack lists the same workspace twice: "
                f"{', '.join(dupes)}. Each workspace needs one entry; keep the "
                f"token you want and delete the other."
            )
        aws = _service_from_raw(AWSService, name, "aws", p)
        oci = _service_from_raw(OCIService, name, "oci", p)
        atlassian = _service_from_raw(AtlassianService, name, "atlassian", p)
        notion = _service_from_raw(NotionService, name, "notion", p)
        project_env = []
        for s_ in _object_list_from_raw(f"profile {name!r}: project_env", p.get("project_env")):
            if "match" not in s_:
                raise ConfigError(
                    f"profile {name!r}: a project_env entry has no 'match' glob: {s_!r}")
            # Checked before the label below is built from it, and before it
            # reaches `match_base`/`_VAR_RE` in `mien env sync` — a non-string
            # dies there as a bare AttributeError/TypeError that `env_sync_cmd`
            # does not catch. An empty glob is rejected for the opposite reason:
            # `match_base("")` is `""`, so the emitted `case "$PWD/" in /*)`
            # fires in every directory and the scope's env is exported
            # everywhere. Same silent widening as the `"*"` element that
            # `_glob_list_from_raw` exists to stop.
            match = _glob_string(
                f"profile {name!r}: project_env entry {s_!r}: 'match' must be a "
                f"directory glob string (e.g. '*/Projects/acme')",
                s_["match"],
            )
            if not match:
                raise ConfigError(
                    f"profile {name!r}: project_env entry {s_!r} has an empty "
                    f"'match' glob. An empty glob covers every directory, so this "
                    f"scope's env would be exported everywhere. Write the directory "
                    f"it applies to, e.g. '*/Projects/acme'."
                )
            _reject_unknown_keys(
                f"profile {name!r}: project_env entry {match!r}", s_,
                {f.name for f in dc_fields(ProjectEnvScope)},
            )
            project_env.append(ProjectEnvScope(
                match=match,
                env=_env_map_from_raw(
                    f"profile {name!r}: project_env {match!r} env",
                    s_.get("env")),
            ))
        profiles[name] = Profile(
            name=name,
            google=google,
            github=github,
            slack=slack,
            aws=aws,
            oci=oci,
            atlassian=atlassian,
            notion=notion,
            custom=_custom_map_from_raw(name, p.get("custom")),
            project_env=project_env,
            default_for=_glob_list_from_raw(
                name, "default_for", "directory", p.get("default_for"), "*/Projects/acme"),
            owns_remotes=_glob_list_from_raw(
                name, "owns_remotes", "remote", p.get("owns_remotes"), "github.com/acme/*"),
            git_email=p.get("git_email"),
        )

    return Config(
        schema_version=SCHEMA_VERSION,
        secrets_backend=secrets_backend,
        bootstrap=_optional_mapping_from_raw("bootstrap", raw.get("bootstrap")),
        secret_naming=secret_naming,
        profiles=profiles,
    )
