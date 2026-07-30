"""Inventory the identities and the places already on this machine, so onboarding
is "here's what you have, claim what you want" rather than a blank config.

Two halves, both read-only. Credentials: the local config of each provider
(AWS/OCI profiles, gcloud configurations, GitHub accounts), reported as already
bound to a mien profile or not, with the `mien login` to import each. Places: the
git remote owners of the repositories on this machine, reported as already
claimed by some profile's `owns_remotes` or not — which is what the status line,
`mien guard` and `mien exec` read to answer "whose place is this", and what is
empty on a fresh machine because nothing ever wrote it.

Nothing here reads a secret, touches a backend, or writes anything. Importing a
credential stays an explicit `mien login`; claiming an owner stays an explicit
`mien discover --own`.
"""

from __future__ import annotations

import configparser
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mien.config import Profile
from mien.resolve import (AmbiguousScope, git_origin_remote, normalize_remote,
                          resolve_remote_profile)


@dataclass(frozen=True)
class Found:
    provider: str      # "aws" | "oci" | "gcloud" | "github" | "remote"
    identifier: str    # profile / config / account name, or a `host/owner` remote owner
    detail: str = ""   # e.g. the account email behind a gcloud config, or a
                       # sample repository remote behind an owner


def _ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        pass
    return parser


def discover_aws(home: Path) -> list[Found]:
    """AWS profiles from `~/.aws/config` (`[profile x]`, `[default]`) and
    `~/.aws/credentials` (`[x]`)."""
    names: set[str] = set()
    for section in _ini(home / ".aws" / "config").sections():
        names.add(section[len("profile "):] if section.startswith("profile ") else section)
    names.update(_ini(home / ".aws" / "credentials").sections())
    return [Found("aws", n) for n in sorted(names)]


def discover_oci(home: Path) -> list[Found]:
    """OCI profiles are the section names in `~/.oci/config` (incl. DEFAULT)."""
    parser = _ini(home / ".oci" / "config")
    names = set(parser.sections())
    if parser.defaults():
        names.add("DEFAULT")
    return [Found("oci", n) for n in sorted(names)]


def discover_gcloud(home: Path) -> list[Found]:
    """gcloud configurations from `~/.config/gcloud/configurations/config_<name>`,
    each a `[core] account = …` ini."""
    base = home / ".config" / "gcloud" / "configurations"
    found: list[Found] = []
    if not base.is_dir():
        return found
    for path in sorted(base.glob("config_*")):
        name = path.name[len("config_"):]
        account = _ini(path).get("core", "account", fallback="")
        found.append(Found("gcloud", name, account))
    return found


def discover_github(run=subprocess.run) -> list[Found]:
    """GitHub accounts from `gh auth status`. Skipped silently if gh is absent."""
    try:
        result = run(["gh", "auth", "status"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[Found] = []
    for line in (result.stdout + result.stderr).splitlines():
        # "✓ Logged in to github.com account <name> (…)"
        if "Logged in to" in line and " account " in line:
            after = line.split(" account ", 1)[1].strip()
            name = after.split()[0] if after else ""
            host = line.split("Logged in to", 1)[1].strip().split()[0]
            if name:
                found.append(Found("github", name, host))
    return found


# How far below a scan root a repository is still found. Deep enough for the two
# shapes people actually use — `~/Projects/<repo>` and `~/<employer>/<client>/
# <repo>` — and shallow enough to stay cheap: on the author's machine it visits
# ~2000 directories in 0.2s, versus a full `$HOME` walk. Descent also stops at
# every repository, so no repo's own tree is ever walked.
# ponytail: fixed depth, not a knob — `--scan-root` already points the walk at
# anything deeper, and a second number to tune is a worse answer than a path.
_SCAN_DEPTH = 3


def _git_repos(root: Path, depth: int) -> list[Path]:
    """Git repositories at or under ``root``, at most ``depth`` levels down.

    Hidden directories are skipped, which is also what keeps the walk out of
    `.git` and its `worktrees/` bookkeeping. Symlinked directories are not
    followed, so a link cannot walk the scan out of the tree it was pointed at.
    Descent stops at a repository: everything below one is that same repository.
    """
    if (root / ".git").exists():
        return [root]
    if depth <= 0:
        return []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    repos: list[Path] = []
    for entry in entries:
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
            continue
        repos.extend(_git_repos(entry, depth - 1))
    return repos


def discover_remotes(
    roots: list[Path] | None = None, *, depth: int = _SCAN_DEPTH, origin=git_origin_remote
) -> list[Found]:
    """The git remote owners of the repositories on this machine.

    One `Found` per `host/owner`, with a sample repository remote as its detail —
    the sample is what a claim is verified against, so a glob offered here is
    known to match a real repository rather than assumed to.

    Owners come from the same `normalize_remote` form `owns_remotes` is matched
    in, so what is reported and what is written mean one thing. A remote with no
    owner segment (`host/repo`, some self-hosted setups) is skipped rather than
    reported as owning a whole host.
    """
    owners: dict[str, str] = {}
    for root in roots or [Path(os.environ.get("HOME", str(Path.home())))]:
        for repo in _git_repos(Path(root), depth):
            url = origin(str(repo))
            if not url:
                continue
            norm = normalize_remote(url)
            parts = norm.split("/")
            if len(parts) < 3:
                continue
            owners.setdefault("/".join(parts[:2]), norm)
    return [Found("remote", owner, sample) for owner, sample in sorted(owners.items())]


def owner_glob(owner: str) -> str:
    """The `owns_remotes` glob that claims ``owner`` and its repositories."""
    return f"{owner.strip().rstrip('/').lower()}/*"


def _remote_claimed_by(profiles: dict[str, Profile], remote: str) -> str | None:
    """Which profile already claims ``remote`` — by the exact rule the acting code
    uses, so "covered" here means covered there. An ambiguous claim is still a
    claim; it is reported as one rather than offered again."""
    try:
        return resolve_remote_profile(profiles, remote)
    except AmbiguousScope:
        return "several profiles"


def discover_all(home: Path | None = None, *, github_run=subprocess.run) -> list[Found]:
    home = home or Path(os.environ.get("HOME", str(Path.home())))
    return (discover_aws(home) + discover_oci(home) + discover_gcloud(home)
            + discover_github(github_run))


def _bound_identifiers(profiles: dict[str, Profile], provider: str) -> set[str]:
    """The identifiers a provider is already bound to across mien profiles."""
    bound: set[str] = set()
    for prof in profiles.values():
        if provider == "aws" and prof.aws and prof.aws.profile:
            bound.add(prof.aws.profile)
        elif provider == "oci" and prof.oci and prof.oci.profile:
            bound.add(prof.oci.profile)
        elif provider == "gcloud" and prof.google and prof.google.gcloud_config_name:
            bound.add(prof.google.gcloud_config_name)
        elif provider == "github" and prof.github and prof.github.username:
            bound.add(prof.github.username)
    return bound


def _import_hint(item: Found) -> str:
    p = "<profile>"
    if item.provider == "aws":
        return f"mien login {p} --service aws --aws-profile {item.identifier}"
    if item.provider == "oci":
        return f"mien login {p} --service oci --oci-profile {item.identifier}"
    if item.provider == "github":
        return f"mien login {p} --service github --username {item.identifier}"
    if item.provider == "gcloud":
        email = f" --email {item.detail}" if item.detail else ""
        return f"mien login {p} --service google{email} --client-id <id>"
    if item.provider == "remote":
        return f"mien discover --own {item.identifier} --profile {p}"
    return ""


def render_report(found: list[Found], profiles: dict[str, Profile]) -> str:
    """A human report: per provider, each discovered identity marked as already in
    a mien profile or not imported (with the command to import it)."""
    if not found:
        return ("No local AWS / OCI / gcloud / GitHub identities or git repositories "
                "found. Set an identity up with `mien login`.")
    labels = {"aws": "AWS profiles", "oci": "OCI profiles",
              "gcloud": "gcloud configurations", "github": "GitHub accounts",
              "remote": "Git remote owners"}
    lines: list[str] = []
    for provider in ("remote", "github", "gcloud", "aws", "oci"):
        items = [f for f in found if f.provider == provider]
        if not items:
            continue
        bound = _bound_identifiers(profiles, provider)
        lines.append(f"{labels[provider]}:")
        for item in items:
            detail = f" ({item.detail})" if item.detail else ""
            if provider == "remote":
                # Coverage is decided by resolving the sample remote, not by
                # comparing strings: whatever `resolve_remote_profile` answers is
                # what the status line, guard and exec will answer here.
                claimed = _remote_claimed_by(profiles, item.detail)
                if claimed:
                    lines.append(f"  ✓ {item.identifier} — owned by {claimed}")
                else:
                    lines.append(f"  · {item.identifier}{detail} — no profile owns it")
                    lines.append(f"      {_import_hint(item)}")
                continue
            if item.identifier in bound:
                lines.append(f"  ✓ {item.identifier}{detail} — in a mien profile")
            else:
                lines.append(f"  · {item.identifier}{detail} — not imported")
                lines.append(f"      {_import_hint(item)}")
    return "\n".join(lines)
