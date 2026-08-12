"""Resolve a profile from the working directory.

A profile may claim directories via `default_for` globs. Resolution is a function
of the config and the path it is handed — it inspects no working directory of its
own and caches nothing, so it returns the same answer in a long-lived interactive
shell and in an agent harness that starts a fresh shell for every command.

The one thing it does read from the environment is what the shell would read from
it anyway: `~` and `$VAR` inside a scope, expanded by `expand_scope` at the point
where zsh expands them in the `case` pattern `mien env sync` generates.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import re
import subprocess

from mien.config import Profile

# `$VAR` / `${VAR}`, with the same name characters `os.path.expandvars` accepts.
# The braced form is tried first so `${VAR}` is not read as `$VAR` plus braces.
# Anything else that starts with `$` (a bare `$`, `${VAR:-x}`, `$1`) matches
# nothing here and is left exactly as written.
_VAR_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}|\$([A-Za-z0-9_]+)")


class AmbiguousScope(Exception):
    """Two or more profiles claim a directory with equal specificity."""


def match_base(match: str) -> str:
    """Normalize a scope glob to its directory root.

    Strips a trailing '/*' or '/' so that a scope covers the directory itself and
    everything beneath it. Mirrors the `case "$PWD/" in <base>/*)` form emitted by
    `mien env sync`, so ambient env and identity agree on where a scope ends.

    Normalization only — deliberately no expansion. `mien env sync` writes the
    scope into the generated script as written and lets zsh expand `~` and `$VAR`
    when the `case` runs, which keeps the script valid after HOME changes and keeps
    the sync-time environment out of a file that is read in every shell. Identity
    resolution gets the same expansion by calling `expand_scope` itself; see
    `resolve_profile`.
    """
    base = match
    if base.endswith("/*"):
        base = base[:-2]
    return base.rstrip("/")


def _expand_vars(match: str) -> str:
    """Substitute `$VAR` / `${VAR}` from the environment, except where the value
    would be empty. `os.path.expandvars` cannot express that exception: it skips
    an unset name but expands a set-but-empty one away, which is the same
    widening by a different route (`WORK_ROOT=` in a dotfile, or
    `WORK_ROOT="$SOMETHING_UNSET"` in a wrapper).

    A glob metacharacter (`*`, `?`, `[`) that arrives *in a value* is escaped, so
    `fnmatch` treats it literally. zsh does not set GLOB_SUBST by default, so a
    value like `*` from `$STARVAR` is a literal `*` in the `case` pattern `mien
    env sync` generates — it matches a directory named `*`, i.e. nothing real.
    Left unescaped, `fnmatch` would honour it as a wildcard and match a live tree
    for identity resolution while the ambient block matched nothing — silently
    widening a scope, which is how credentials get misrouted. Glob characters the
    user writes *literally in the scope* are not touched; only ones that come in
    through a variable's value are escaped, which is exactly zsh's split."""

    def sub(m: re.Match[str]) -> str:
        value = os.environ.get(m.group(1) or m.group(2))
        return glob.escape(value) if value else m.group(0)

    return _VAR_RE.sub(sub, match)


def _expand_user(match: str) -> str:
    """Expand a leading `~`, except where HOME is set but empty — `os.path
    .expanduser` has the same hole as `expandvars` there, turning '~/Projects'
    into '/Projects'. An absent HOME is not the same case: expanduser falls back
    to the password database and gets a real home."""
    if (match == "~" or match.startswith("~/")) and os.environ.get("HOME") == "":
        return match
    return os.path.expanduser(match)


def expand_scope(match: str) -> str:
    """Expand `~` and `$VAR` in a scope, the way the shell expands the same text.

    zsh performs tilde and parameter expansion on a `case` pattern before matching,
    so `case "$PWD/" in ~/Projects/acme/*)` fires inside the home directory.
    `fnmatch` performs neither, so without this the identical scope string would
    cover a directory for ambient env and not for identity — a hard "no profile
    claims ..." at best, and the wrong identity if some broader scope catches it.

    A variable that is unset OR set to the empty string is left as written rather
    than expanded away, and so is `~` under an empty HOME. This is a deliberate
    divergence from zsh, which expands both to nothing: '$EMPTY/Projects' would
    become the far broader '/Projects', and a scope that is nothing but
    '$EMPTY' would normalize to '' and cover every absolute path. Silently
    widening a scope is how credentials get misrouted. Left literal, the
    reference simply matches nothing.

    Only `$VAR` and `${VAR}` are recognized; any other `$`-form is left untouched
    rather than rejected.

    A glob character (`*`, `?`, `[`) coming from a variable's *value* is treated
    literally, not as a wildcard, matching zsh's default (no GLOB_SUBST): the
    substitution is expansion, not pattern injection. Glob characters written
    literally in the scope itself keep their wildcard meaning. See `_expand_vars`.
    """
    return _expand_vars(_expand_user(match))


def _covers(base: str, path: str) -> bool:
    """True if `base` covers `path` — the directory itself or a descendant.

    `fnmatch`'s '*' spans '/', matching the shell `case` patterns these globs are
    also compiled into. Descendants are tested against '<base>/*' rather than by
    string prefix, so '*/Projects/acme' does not capture '.../acme-fork'.
    """
    return fnmatch.fnmatch(path, base) or fnmatch.fnmatch(path, f"{base}/*")


def _specificity(base: str) -> int:
    """Longer globs are treated as more specific, so a scope nested inside another
    wins. This is a lexical rule, not a semantic one: it cannot tell that
    '*/Projects/acme' is narrower than a longer but broader literal path. Equal
    scores are refused rather than broken arbitrarily, which keeps the rule honest
    about what it does not know."""
    return len(base)


def resolve_profile(profiles: dict[str, Profile], path: str) -> str | None:
    """Return the profile claiming `path`, or None if no scope covers it.

    `path` is supplied by the caller rather than read here, so the answer depends
    only on the arguments. Scopes are expanded (`expand_scope`) and then normalized
    (`match_base`) so that a scope means the same directory tree here as it does in
    the zsh `case` that `mien env sync` generates from it.

    Raises AmbiguousScope when two profiles claim it with equal specificity;
    guessing between them would misroute credentials silently.
    """
    best: dict[str, int] = {}
    for name in sorted(profiles):
        for raw in profiles[name].default_for:
            base = match_base(expand_scope(raw))
            if _covers(base, path):
                score = _specificity(base)
                if score > best.get(name, -1):
                    best[name] = score

    if not best:
        return None

    top = max(best.values())
    winners = sorted(n for n, s in best.items() if s == top)
    if len(winners) > 1:
        raise AmbiguousScope(
            f"{path} is claimed with equal specificity by: {', '.join(winners)}. "
            "Narrow one of their default_for scopes, or name a profile explicitly."
        )
    return winners[0]


def normalize_remote(url: str) -> str:
    """Reduce a git remote URL to a canonical, lower-cased ``host/path``.

    The same repository is reachable as `https://github.com/o/r.git`,
    `git@github.com:o/r.git`, or `ssh://git@github.com/o/r` — all of which name
    the same owner. Stripping the scheme, any `user@`, a trailing `.git`, and
    normalizing the scp-form `:` to `/` lets one `owns_remotes` glob match every
    form. Lower-casing keeps host and owner matching case-insensitively (a case
    mismatch would be a false *miss* — the status line failing to warn — which is
    the worse direction for a safety signal).
    """
    s = url.strip()
    if s.endswith(".git"):
        s = s[:-4]
    if "://" in s:
        s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", s)  # drop scheme
        s = re.sub(r"^[^/@]+@", "", s)                     # drop user@
        s = re.sub(r"^([^/:]+):\d+", r"\1", s)             # drop :port from the host
                                                           # (e.g. GitHub's ssh.github.com:443)
    elif re.match(r"^[^/]+@[^:/]+:", s):                   # scp-like git@host:path
        s = re.sub(r"^[^@]+@", "", s).replace(":", "/", 1)
    return s.rstrip("/").lower()


# Literal, case-sensitive prefixes of issued tokens. Add a provider here.
# GitHub tokens only: an underscore is illegal in a GitHub username, so none of
# these can be a real user. Other forges' tokens are deliberately absent — their
# prefixes (`glpat-`, and Slack/Atlassian tokens that cannot authenticate git at
# all) are legal usernames, and the real GitLab forms `gitlab-ci-token:<token>@`
# and `oauth2:<token>@` are caught by the password branch.
CREDENTIAL_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")


def remote_embeds_credential(url: str | None) -> bool:
    """True if ``url`` carries a secret in its userinfo — `https://user:token@host`.

    This is an identity mien does not route and cannot see: git authenticates as
    whoever that token belongs to, no matter which profile is active, and every
    command that prints a remote (`git remote -v`, `git config --list`, a push
    error) writes the secret into a terminal, a CI log, or an agent transcript.

    A *password* component always counts. A bare userinfo counts only when it
    looks like a token: `git clone https://$TOKEN@host/...` leaves the secret
    alone in the userinfo, and git sends it as the Basic username with an empty
    password — a working credential. A bare `https://username@host` names a user
    git prompts against, so it is flagged only on a GitHub token prefix, matched
    case-sensitively — each contains an underscore, which GitHub forbids in a
    username, so no real user can collide with one. A false positive here would
    train people to ignore the warning.
    """
    if not url:
        return False
    m = re.match(r"^https?://([^/]+)@", url.strip(), re.IGNORECASE)
    if not m:
        return False
    userinfo = m.group(1)
    return ":" in userinfo or userinfo.startswith(CREDENTIAL_PREFIXES)


def resolve_remote_profile(profiles: dict[str, Profile], remote: str) -> str | None:
    """Return the profile whose ``owns_remotes`` claims ``remote``, or None.

    ``remote`` is normalized (`normalize_remote`) and matched against each
    profile's globs — both the pattern itself and ``<pattern>/*``, so a bare
    owner glob (`github.com/arinyaho`) claims the owner and everything under it.
    As with directory scopes, the longest matching pattern wins and an exact tie
    raises AmbiguousScope rather than guessing.
    """
    norm = normalize_remote(remote)
    best: dict[str, int] = {}
    for name in sorted(profiles):
        for raw in profiles[name].owns_remotes:
            pat = raw.strip().rstrip("/").lower()
            if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(norm, f"{pat}/*"):
                score = len(pat)
                if score > best.get(name, -1):
                    best[name] = score
    if not best:
        return None
    top = max(best.values())
    winners = sorted(n for n, s in best.items() if s == top)
    if len(winners) > 1:
        raise AmbiguousScope(
            f"remote {norm!r} is claimed with equal specificity by: "
            f"{', '.join(winners)}. Narrow one of their owns_remotes globs."
        )
    return winners[0]


def git_origin_remote(cwd: str) -> str | None:
    """The `origin` remote URL of the repository at ``cwd``, or None.

    Thin git I/O, kept separate so the matching logic stays pure and testable and
    callers can mock it. Never raises: no repo, no `origin`, or no `git` on PATH
    all return None, so a status line built on it stays silent rather than failing.

    Fetch URL deliberately: this answers "who owns this repository" for identity
    routing, and the fetch URL is that answer.

    ponytail: so a credential that lives only in `remote.origin.pushurl` does not
    raise the status-line warning — `mien doctor` reports it. Add a separate
    pushurl query here if the status line needs to catch it too; do not widen this
    function, whose result also feeds owner matching.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def claimed_profile(
    profiles: dict[str, Profile], path: str, *, remote: str | None = None
) -> tuple[str | None, str | None]:
    """The profile a location claims, and how — by the repository's remote owner
    first, then by directory ``default_for``.

    Returns ``(name, source)`` where source is ``"repo"`` or ``"dir"`` (or
    ``(None, None)`` when nothing claims it), so a caller can phrase the two
    differently. Remote is the stronger signal — a repository's owner is
    objective, a path glob is a heuristic — so it wins when both would match.
    Raises AmbiguousScope from whichever layer is itself ambiguous.

    Note: this is for *display* (the status line). Choosing an identity that acts
    must not consider the remote, since a checked-out repository controls it; see
    the resolution design.
    """
    if remote:
        by_remote = resolve_remote_profile(profiles, remote)
        if by_remote:
            return by_remote, "repo"
    by_dir = resolve_profile(profiles, path)
    return (by_dir, "dir" if by_dir else None)


def git_author_email(cwd: str) -> str | None:
    """The email a commit in the repository at ``cwd`` would be authored as, or
    None. Reads the effective `user.email` (repo-local then global), the same
    value `git commit` would stamp. Thin, mockable git I/O; never raises.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "config", "user.email"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _profile_emails(profile: Profile) -> set[str]:
    """The email addresses known to identify ``profile``, lower-cased.

    Drawn from what the profile already declares — an explicit `git_email`, its
    Google and Atlassian account emails, and the GitHub no-reply address derived
    from its username — so a commit's `user.email` can be attributed to a profile.
    The explicit `git_email` is the one to set when you commit under an address
    none of the accounts carry.
    """
    emails: set[str] = set()
    if profile.git_email:
        emails.add(profile.git_email.lower())
    if profile.google and profile.google.email:
        emails.add(profile.google.email.lower())
    if profile.atlassian and profile.atlassian.email:
        emails.add(profile.atlassian.email.lower())
    if profile.github and profile.github.username:
        emails.add(f"{profile.github.username.lower()}@users.noreply.github.com")
    return emails


def profile_for_email(profiles: dict[str, Profile], email: str) -> str | None:
    """Which profile a git-author ``email`` identifies, or None when it matches
    no profile or (defensively) more than one.

    Returning None for an unknown email is deliberate: the author cross-check
    warns only on a *confident* mismatch — an email that positively belongs to a
    different profile — and never on a merely unrecognized one, so a legitimate
    alternate address raises no false alarm.
    """
    target = email.strip().lower()
    if not target:
        return None
    owners = [name for name in sorted(profiles)
              if target in _profile_emails(profiles[name])]
    return owners[0] if len(owners) == 1 else None
