"""Render the mien identity segment for a Claude Code status line.

The segment answers one question, always in view: *who am I acting as here* —
and turns red the moment the answer is wrong. An agent session that inherited
one identity from the shell it was launched in, sitting in a directory that
belongs to a different identity, is exactly how a personal commit lands in a
work repository. The status line makes that visible before it happens.

The rendering is pure and secret-free by construction: it takes only two profile
*names* (never a token) and formats them, so it is safe to call at status-line
frequency.
"""

from __future__ import annotations

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"


def render_segment(
    env_profile: str | None,
    claimed_profile: str | None,
    *,
    source: str = "dir",
    author_profile: str | None = None,
    ambiguous: bool = False,
    env_unknown: bool = False,
    pending: str | None = None,
    remote_credential: bool = False,
) -> str:
    """Format the mien identity segment.

    Arguments are mien's signals for "who am I here":
    - ``env_profile``: the ``MIEN_PROFILE`` active in this session (inherited
      from the launching shell), or ``None``.
    - ``claimed_profile``: the profile this location claims — by the repository's
      remote owner or by a directory ``default_for`` scope — or ``None``.
    - ``source``: ``"repo"`` if the claim came from the git remote owner,
      ``"dir"`` if from a directory scope; only affects the mismatch wording.
    - ``author_profile``: the profile the repository's git ``user.email`` belongs
      to, or ``None`` when it matches no profile. Compared against the claim to
      catch a commit authored as the wrong self even with no profile activated.
    - ``ambiguous``: the location is claimed by two profiles with equal
      specificity (resolution would refuse to guess).
    - ``env_unknown``: ``MIEN_PROFILE`` names a profile that is not in the
      config (renamed or deleted, leaving a stale export in an open shell).
    - ``remote_credential``: the repository's `origin` embeds a token in its URL
      (`remote_embeds_credential`), so git acts as that token's owner whatever
      mien says — and every command that prints the remote leaks the secret.

    The alarm cases come first: a wrong or unknown active identity is the failure
    this segment exists to surface, so it must win over the calm cases.
    """
    def why(claimed: str) -> str:
        return f"repo is {claimed}'s" if source == "repo" else f"dir wants {claimed}"

    # A token sitting in the remote URL outranks every other alarm: it is a
    # certainty rather than a comparison, it overrides whatever mien routed, and
    # it is leaking on every command that prints the remote. Naming the profile
    # would only dilute it — the credential is the finding.
    if remote_credential:
        return f"{_RED}🔴 mien ✗ origin embeds a token — run 'mien doctor'{_RESET}"
    # A project-local `.mien` declaration is present but not yet approved — it
    # names an identity but does not act until `mien allow`.
    if pending and not env_profile:
        return f"{_YELLOW}🟡 mien:{pending}? ✗ run 'mien allow'{_RESET}"
    # An active profile that no longer exists — a stale export in this shell.
    if env_profile and env_unknown:
        return f"{_RED}🔴 mien:{env_profile} ✗ unknown profile{_RESET}"
    # Active identity disagrees with what this location claims: you are set up to
    # act as the wrong person here.
    if env_profile and claimed_profile and env_profile != claimed_profile:
        return f"{_RED}🔴 mien:{env_profile} ✗ {why(claimed_profile)}{_RESET}"
    # The git author would commit as a *different* profile than the one this
    # place belongs to — the mis-commit that lands even when nothing was
    # activated (or when the right profile is active but user.email is stale).
    if author_profile and claimed_profile and author_profile != claimed_profile:
        return f"{_RED}🔴 author:{author_profile} ✗ {why(claimed_profile)}{_RESET}"
    # The location is claimed ambiguously and nothing is set to break the tie —
    # mien would refuse to route; surface it rather than pick silently.
    if ambiguous and not env_profile:
        return f"{_RED}🔴 mien:? ✗ ambiguous{_RESET}"
    # A single definite identity: an explicit MIEN_PROFILE wins, else the claim.
    active = env_profile or claimed_profile
    if active:
        return f"{_GREEN}🟢 mien:{active}{_RESET}"
    # Nothing set, and nothing claims this location.
    return f"{_YELLOW}🟡 mien:— no profile here{_RESET}"


def guard_reason(
    env_profile: str | None,
    claimed_profile: str | None,
    *,
    source: str = "dir",
    author_profile: str | None = None,
    env_known: bool = True,
) -> str | None:
    """Why acting here would be as the wrong identity, or None if it is allowed.

    The gate refuses only on a *confident* mismatch — a known active profile, or a
    known git author, that positively disagrees with what this place belongs to.
    It stays silent (returns None) on every uncertainty: nothing claims the place,
    the active profile is unknown/stale, or the git author matches no profile. A
    gate that blocked on a guess would train people to bypass it, so it blocks
    only what it is sure about. Same signals as `render_segment`; this is the
    acting counterpart of that display.
    """
    if not claimed_profile:
        return None
    place = (f"this repository belongs to {claimed_profile}"
             if source == "repo" else f"this directory belongs to {claimed_profile}")
    if env_profile and env_known and env_profile != claimed_profile:
        return f"the active identity is {env_profile}, but {place}"
    if author_profile and author_profile != claimed_profile:
        return f"a commit here would be authored as {author_profile}, but {place}"
    return None
