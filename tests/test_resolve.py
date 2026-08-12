import pytest

from mien.config import AtlassianService, GitHubService, GoogleService, Profile
from mien.resolve import (AmbiguousScope, claimed_profile, expand_scope,
                          match_base, normalize_remote, profile_for_email,
                          remote_embeds_credential, resolve_profile,
                          resolve_remote_profile)


def _gp(name, *, google=None, atlassian=None, github=None):
    g = GoogleService(email=google, oauth_client_id="c", oauth_client_secret_ref=None,
                      refresh_token_ref=None, adc_ref=None, gcloud_config_name=name,
                      default_project=None) if google else None
    a = AtlassianService(email=atlassian, api_token_ref="r",
                         base_url="https://x.atlassian.net") if atlassian else None
    h = GitHubService(username=github, host="github.com", token_ref="r") if github else None
    return Profile(name=name, google=g, atlassian=a, github=h)


class TestProfileForEmail:
    def test_matches_google_and_atlassian_addresses(self):
        ps = {"work": _gp("work", google="me@acme.example"),
              "client": _gp("client", atlassian="me@client.example")}
        assert profile_for_email(ps, "me@acme.example") == "work"
        assert profile_for_email(ps, "me@client.example") == "client"

    def test_matches_the_github_noreply_form_case_insensitively(self):
        ps = {"me": _gp("me", github="Octo")}
        assert profile_for_email(ps, "octo@users.noreply.github.com") == "me"
        assert profile_for_email(ps, "OCTO@users.noreply.github.com") == "me"

    def test_an_unknown_email_returns_none(self):
        ps = {"work": _gp("work", google="me@acme.example")}
        assert profile_for_email(ps, "stranger@nowhere.example") is None
        assert profile_for_email(ps, "") is None

    def test_an_email_shared_by_two_profiles_is_inconclusive(self):
        ps = {"a": _gp("a", google="shared@x.example"),
              "b": _gp("b", atlassian="shared@x.example")}
        assert profile_for_email(ps, "shared@x.example") is None

    def test_matches_an_explicit_git_email_none_of_the_accounts_carry(self):
        ps = {"work": Profile(name="work", git_email="Commits@Acme.example",
                              google=GoogleService(
                                  email="me@acme.example", oauth_client_id="c",
                                  oauth_client_secret_ref=None, refresh_token_ref=None,
                                  adc_ref=None, gcloud_config_name="work",
                                  default_project=None))}
        # the commit address differs from the Google login, yet still attributes
        assert profile_for_email(ps, "commits@acme.example") == "work"
        assert profile_for_email(ps, "me@acme.example") == "work"


def prof(name: str, *globs: str) -> Profile:
    return Profile(name=name, default_for=list(globs))


def profiles(*ps: Profile) -> dict[str, Profile]:
    return {p.name: p for p in ps}


def rprof(name: str, *remotes: str) -> Profile:
    return Profile(name=name, owns_remotes=list(remotes))


class TestNormalizeRemote:
    @pytest.mark.parametrize("url", [
        "https://github.com/octo/widget.git",
        "https://github.com/octo/widget",
        "git@github.com:octo/widget.git",
        "ssh://git@github.com/octo/widget",
        "https://user@github.com/octo/widget.git",
        "https://github.com/Octo/Widget",            # case-folded
    ])
    def test_every_form_of_the_same_repo_normalizes_alike(self, url):
        assert normalize_remote(url) == "github.com/octo/widget"

    @pytest.mark.parametrize("url", [
        "ssh://git@github.com:22/octo/widget",
        "ssh://git@ssh.github.com:443/octo/widget.git",   # GitHub's firewall form
        "https://github.com:443/octo/widget",
    ])
    def test_a_port_is_stripped_from_the_host(self, url):
        # A ported remote must still match an owns_remotes glob like github.com/octo/*.
        norm = normalize_remote(url)
        assert ":22" not in norm and ":443" not in norm
        assert norm.endswith("/octo/widget")

    def test_an_unencoded_slash_in_the_password_does_not_survive_as_a_host(self):
        """A mistyped remote git stores but curl rejects: stopping the userinfo
        at the first `/` would leave `user:ab/cd@github.com` as the owner."""
        norm = normalize_remote("https://user:ab/cd@github.com/acme/x.git")
        assert norm == "github.com/acme/x"
        assert "user" not in norm and "@" not in norm

    def test_an_at_sign_in_the_path_is_not_mistaken_for_userinfo(self):
        assert normalize_remote("https://github.com/acme/x@v2") == "github.com/acme/x@v2"
        assert normalize_remote("https://host:8080/acme/x@v2") == "host/acme/x@v2"

    def test_an_at_sign_in_the_password_does_not_survive_as_a_host(self):
        """Splitting the authority at the first `@` would print the owner as
        `rest@github.com/acme` — a credential fragment, and never the real owner."""
        norm = normalize_remote("https://user:paSS@rest@github.com/acme/x.git")
        assert norm == "github.com/acme/x"
        assert "@" not in norm and "pass" not in norm

    def test_a_bracketed_ipv6_host_is_a_host_and_not_a_malformed_authority(self):
        # Brackets are stripped and a port dropped, like any other host.
        assert normalize_remote("https://[::1]/acme/x@v2") == "::1/acme/x@v2"
        assert normalize_remote("https://[::1]/acme/x.git") == "::1/acme/x"
        assert normalize_remote("https://[::1]:8443/acme/x") == "::1/acme/x"

    def test_a_netloc_urlsplit_itself_rejects_is_handled_and_not_raised(self):
        """`urlsplit` raises *before* `.port` on two shapes, and the NFKC message
        quotes the netloc — i.e. the token. A raise would abort `mien discover`'s
        whole sweep and print the credential in the traceback."""
        for url, host in (
            # netloc not NFKC-stable
            ("https://user:ghp_faketoken0000@gith℀ub.com/acme/x.git", "gith℀ub.com"),
            # `]` with no `[` — "Invalid IPv6 URL"
            ("https://user:fake]pass@github.com/acme/x.git", "github.com"),
        ):
            norm = normalize_remote(url)
            assert norm == f"{host}/acme/x"
            assert "@" not in norm and "user" not in norm and "fake" not in norm
            assert remote_embeds_credential(url)

    def test_a_local_path_is_left_as_a_lowercased_string(self):
        # No host; simply must not crash and must not spuriously match a glob.
        assert normalize_remote("/srv/git/Repo") == "/srv/git/repo"


class TestResolveRemoteProfile:
    def test_matches_the_owning_profile(self):
        ps = profiles(rprof("work", "github.com/acme-*/*"),
                      rprof("personal", "github.com/me/*"))
        assert resolve_remote_profile(ps, "git@github.com:acme-core/api.git") == "work"
        assert resolve_remote_profile(ps, "https://github.com/me/dots") == "personal"

    def test_a_profile_can_own_several_orgs(self):
        # A personal account owns its user repos AND the orgs it manages.
        ps = profiles(rprof("me", "github.com/me/*", "github.com/me-labs-*/*"))
        assert resolve_remote_profile(ps, "https://github.com/me/blog") == "me"
        assert resolve_remote_profile(ps, "git@github.com:me-labs-x/site.git") == "me"

    def test_no_owner_returns_none(self):
        ps = profiles(rprof("work", "github.com/acme-*/*"))
        assert resolve_remote_profile(ps, "https://github.com/someone-else/x") is None

    def test_a_bare_owner_glob_claims_its_repos(self):
        ps = profiles(rprof("work", "github.com/acme"))
        assert resolve_remote_profile(ps, "https://github.com/acme/api.git") == "work"

    def test_an_equal_specificity_tie_raises(self):
        ps = profiles(rprof("a", "github.com/x-*/*"), rprof("b", "github.com/*/*"))
        # both patterns match; different lengths → longer ('x-*') wins, no raise
        assert resolve_remote_profile(ps, "https://github.com/x-1/r") == "a"
        # genuinely equal-length rival patterns tie
        ps2 = profiles(rprof("a", "github.com/x/*"), rprof("b", "github.com/x/*"))
        with pytest.raises(AmbiguousScope):
            resolve_remote_profile(ps2, "https://github.com/x/r")


class TestClaimedProfile:
    def test_remote_owner_wins_over_directory_scope(self, monkeypatch):
        ps = {"work": Profile(name="work", owns_remotes=["github.com/acme-*/*"]),
              "personal": Profile(name="personal", default_for=["*/flat/*"])}
        name, source = claimed_profile(
            ps, "/x/flat/api", remote="https://github.com/acme-core/api.git")
        assert (name, source) == ("work", "repo")

    def test_falls_back_to_directory_when_no_remote_match(self):
        ps = {"work": Profile(name="work", owns_remotes=["github.com/acme-*/*"]),
              "personal": Profile(name="personal", default_for=["*/flat/*"])}
        name, source = claimed_profile(
            ps, "/x/flat/api", remote="https://github.com/nobody/api.git")
        assert (name, source) == ("personal", "dir")

    def test_nothing_claims_it(self):
        ps = {"work": Profile(name="work", owns_remotes=["github.com/acme-*/*"])}
        assert claimed_profile(ps, "/x/y", remote=None) == (None, None)


class TestMatchBase:
    """Where a scope ends must agree with the zsh `case "$PWD/" in base/*)` form
    that `mien env sync` generates, or ambient env and identity would disagree
    about which directories a scope covers. This half is normalization only: the
    generated script keeps the scope as written and lets zsh expand it, so the
    matching side gets that expansion from `expand_scope` instead."""

    @pytest.mark.parametrize("raw,expected", [
        ("*/Projects/mien", "*/Projects/mien"),
        ("*/Projects/mien/", "*/Projects/mien"),
        ("*/Projects/mien/*", "*/Projects/mien"),
        ("/", ""),
    ])
    def test_strips_trailing_slash_and_star(self, raw, expected):
        assert match_base(raw) == expected

    @pytest.mark.parametrize("raw", ["~/Projects/mien", "$HOME/Projects/mien"])
    def test_does_not_expand_so_env_sync_output_is_unchanged(self, raw, monkeypatch):
        """`mien env sync` emits this text straight into a zsh `case` pattern, where
        the shell expands it at match time. Expanding here would bake the sync-time
        HOME into a file every shell sources."""
        monkeypatch.setenv("HOME", "/Users/me")
        assert match_base(raw + "/*") == raw
        assert match_base(raw) == raw


class TestExpandScope:
    """zsh expands `~` and `$VAR` in a `case` pattern before matching; `fnmatch`
    expands neither. Without an equivalent step the identical scope string would
    cover a directory for ambient env and not for identity."""

    def test_expands_tilde_and_variables(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/me")
        monkeypatch.setenv("MIEN_TEST_ROOT", "/srv/clients")
        assert expand_scope("~/Projects/acme") == "/Users/me/Projects/acme"
        assert expand_scope("$HOME/Projects/acme") == "/Users/me/Projects/acme"
        assert expand_scope("$MIEN_TEST_ROOT/acme") == "/srv/clients/acme"

    def test_leaves_ordinary_globs_alone(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/me")
        assert expand_scope("*/Projects/acme") == "*/Projects/acme"

    def test_literalizes_a_glob_from_a_variable_value(self, monkeypatch):
        """A glob character arriving through a variable's *value* is escaped so
        fnmatch treats it literally — zsh has no GLOB_SUBST, so the value is a
        literal in the `case` pattern, not a wildcard. `[*]` is fnmatch for a
        literal `*`. The glob the user wrote literally in the scope is untouched
        (covered by test_leaves_ordinary_globs_alone)."""
        monkeypatch.setenv("STARVAR", "*")
        monkeypatch.setenv("QVAR", "a?b")
        assert expand_scope("$STARVAR/Projects/acme") == "[*]/Projects/acme"
        assert expand_scope("$QVAR/x") == "a[?]b/x"

    def test_leaves_an_undefined_variable_literal(self, monkeypatch):
        """zsh would expand it away and leave the far broader '/Projects/acme'.
        Silently widening a scope is how credentials get misrouted, so an unset
        variable stays literal and matches nothing."""
        monkeypatch.delenv("MIEN_NO_SUCH_VAR", raising=False)
        assert expand_scope("$MIEN_NO_SUCH_VAR/Projects/acme") == (
            "$MIEN_NO_SUCH_VAR/Projects/acme"
        )

    @pytest.mark.parametrize("raw", [
        "$MIEN_TEST_ROOT", "${MIEN_TEST_ROOT}",
        "$MIEN_TEST_ROOT/Projects/acme", "${MIEN_TEST_ROOT}/Projects/acme",
    ])
    def test_leaves_a_set_but_empty_variable_literal(self, raw, monkeypatch):
        """`export WORK_ROOT=` in a dotfile is the ordinary accident. zsh would
        expand it away exactly like an unset one, so it fails closed the same
        way: alone it would normalize to '' and cover every absolute path, and
        as a prefix it would leave the far broader '/Projects/acme'."""
        monkeypatch.setenv("MIEN_TEST_ROOT", "")
        assert expand_scope(raw) == raw

    @pytest.mark.parametrize("raw", ["${MIEN_TEST_ROOT}/acme", "${HOME}/Projects/acme"])
    def test_expands_the_braced_form_when_the_value_is_non_empty(self, raw, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/me")
        monkeypatch.setenv("MIEN_TEST_ROOT", "/srv/clients")
        assert "$" not in expand_scope(raw)

    def test_leaves_tilde_literal_when_home_is_empty(self, monkeypatch):
        """`os.path.expanduser` has the same hole: an empty HOME turns
        '~/Projects/acme' into '/Projects/acme'."""
        monkeypatch.setenv("HOME", "")
        assert expand_scope("~/Projects/acme") == "~/Projects/acme"
        assert expand_scope("~") == "~"

    def test_leaves_unrecognized_dollar_forms_untouched(self, monkeypatch):
        """Only `$VAR` and `${VAR}` are substituted; nothing else becomes an
        error, so scopes that work today keep working."""
        monkeypatch.delenv("MIEN_NO_SUCH_VAR", raising=False)
        assert expand_scope("*/Projects/$") == "*/Projects/$"
        assert expand_scope("${MIEN_NO_SUCH_VAR:-/tmp}/acme") == (
            "${MIEN_NO_SUCH_VAR:-/tmp}/acme"
        )


class TestResolveExpandedScopes:
    def test_tilde_scope_covers_the_home_directory(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/me")
        p = profiles(prof("work", "~/Projects/acme"))
        assert resolve_profile(p, "/Users/me/Projects/acme") == "work"
        assert resolve_profile(p, "/Users/me/Projects/acme/src") == "work"
        # expanded to THIS home — '~' is not a wildcard standing for any home
        assert resolve_profile(p, "/Users/other/Projects/acme") is None
        # and it is not the literal text '~/...' that got matched
        assert resolve_profile(p, "~/Projects/acme") is None

    def test_home_variable_scope_covers_the_home_directory(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/me")
        p = profiles(prof("work", "$HOME/Projects/acme"))
        assert resolve_profile(p, "/Users/me/Projects/acme/src") == "work"
        assert resolve_profile(p, "/Users/other/Projects/acme") is None

    def test_glob_in_a_variable_value_does_not_become_a_wildcard(self, monkeypatch):
        """A scope of `$STARVAR/Projects/acme` with STARVAR='*' must match a
        directory literally named '*', i.e. nothing real — not every client tree.
        Before the fix, fnmatch honoured the injected '*' as a wildcard and the
        scope claimed a live directory that the ambient `case` block (literal,
        per zsh's no-GLOB_SUBST default) matched nothing for."""
        monkeypatch.setenv("STARVAR", "*")
        p = profiles(prof("work", "$STARVAR/Projects/acme"))
        assert resolve_profile(p, "/srv/clients/Projects/acme") is None

    def test_arbitrary_variable_scope(self, monkeypatch):
        monkeypatch.setenv("MIEN_TEST_ROOT", "/srv/clients")
        p = profiles(prof("work", "$MIEN_TEST_ROOT/acme"))
        assert resolve_profile(p, "/srv/clients/acme/src") == "work"
        assert resolve_profile(p, "/srv/clients/acme-fork") is None

    @pytest.mark.parametrize("raw", [
        "~/Projects/acme", "~/Projects/acme/", "~/Projects/acme/*",
    ])
    def test_trailing_forms_are_normalized_after_expansion(self, raw, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/me")
        p = profiles(prof("work", raw))
        assert resolve_profile(p, "/Users/me/Projects/acme") == "work"
        assert resolve_profile(p, "/Users/me/Projects/acme/deep") == "work"
        assert resolve_profile(p, "/Users/me/Projects/acme-fork") is None

    def test_undefined_variable_does_not_widen_the_scope(self, monkeypatch):
        monkeypatch.delenv("MIEN_NO_SUCH_VAR", raising=False)
        p = profiles(prof("work", "$MIEN_NO_SUCH_VAR/Projects/acme"))
        assert resolve_profile(p, "/Projects/acme") is None
        assert resolve_profile(p, "/Users/me/Projects/acme") is None

    @pytest.mark.parametrize("raw", ["$MIEN_TEST_ROOT", "${MIEN_TEST_ROOT}"])
    def test_empty_variable_alone_claims_nothing(self, raw, monkeypatch):
        """Expanded away, this scope would normalize to '' and `_covers` would
        match every absolute path — every directory on the machine would run as
        `work`."""
        monkeypatch.setenv("MIEN_TEST_ROOT", "")
        p = profiles(prof("work", raw))
        assert resolve_profile(p, "/Users/me/Projects/mien") is None
        assert resolve_profile(p, "/tmp/scratch") is None
        assert resolve_profile(p, "/") is None

    @pytest.mark.parametrize("raw", [
        "$MIEN_TEST_ROOT/Projects/acme", "${MIEN_TEST_ROOT}/Projects/acme",
    ])
    def test_empty_variable_prefix_does_not_widen_the_scope(self, raw, monkeypatch):
        monkeypatch.setenv("MIEN_TEST_ROOT", "")
        p = profiles(prof("work", raw))
        assert resolve_profile(p, "/Projects/acme") is None
        assert resolve_profile(p, "/Projects/acme/src") is None
        assert resolve_profile(p, "/Users/me/Projects/acme") is None

    def test_an_empty_variable_scope_does_not_steal_another_profiles_directory(
        self, monkeypatch
    ):
        """The reported misroute: `work: ["$WORK_ROOT"]` with WORK_ROOT empty
        answered `work` in a directory `personal` owns, and elsewhere."""
        monkeypatch.setenv("MIEN_TEST_ROOT", "")
        p = profiles(
            prof("work", "$MIEN_TEST_ROOT"),
            prof("personal", "*/Projects/mien"),
        )
        assert resolve_profile(p, "/Users/me/Projects/mien") == "personal"
        assert resolve_profile(p, "/Users/me/elsewhere") is None

    def test_empty_home_does_not_widen_a_tilde_scope(self, monkeypatch):
        monkeypatch.setenv("HOME", "")
        p = profiles(prof("work", "~/Projects/acme"))
        assert resolve_profile(p, "/Projects/acme") is None
        assert resolve_profile(p, "/Projects/acme/src") is None

    def test_specificity_is_scored_on_the_expanded_scope(self, monkeypatch):
        """Unexpanded, the shorter '~/Projects/acme' would lose to the longer but
        broader literal parent; expanded, the nested scope is longer and wins."""
        monkeypatch.setenv("HOME", "/Users/me")
        p = profiles(
            prof("personal", "/Users/me/Projects"),
            prof("work", "~/Projects/acme"),
        )
        assert resolve_profile(p, "/Users/me/Projects/acme/src") == "work"
        assert resolve_profile(p, "/Users/me/Projects/other") == "personal"

    def test_two_spellings_of_one_directory_are_ambiguous(self, monkeypatch):
        """'~/Projects/shared' and '$HOME/Projects/shared' name the same directory,
        so two profiles spelling it differently is the same clash as spelling it
        identically — not a silent miss."""
        monkeypatch.setenv("HOME", "/Users/me")
        p = profiles(
            prof("delta", "~/Projects/shared"),
            prof("echo", "$HOME/Projects/shared"),
        )
        with pytest.raises(AmbiguousScope) as exc:
            resolve_profile(p, "/Users/me/Projects/shared")
        # naming both clashing profiles is the whole point of refusing
        assert "claimed with equal specificity by: delta, echo" in str(exc.value)


class TestResolveProfile:
    def test_no_scopes_resolves_to_nothing(self):
        assert resolve_profile(profiles(prof("work")), "/Users/me/Projects/x") is None

    def test_matches_the_directory_itself(self):
        p = profiles(prof("work", "*/Projects/acme"))
        assert resolve_profile(p, "/Users/me/Projects/acme") == "work"

    def test_matches_a_descendant(self):
        p = profiles(prof("work", "*/Projects/acme"))
        assert resolve_profile(p, "/Users/me/Projects/acme/deep/nested") == "work"

    def test_does_not_match_a_sibling_with_a_shared_prefix(self):
        """`acme` must not capture `acme-fork` — a prefix match without the
        separator would route a different project's commits to the wrong account."""
        p = profiles(prof("work", "*/Projects/acme"))
        assert resolve_profile(p, "/Users/me/Projects/acme-fork") is None

    def test_unrelated_directory_resolves_to_nothing(self):
        p = profiles(prof("work", "*/Projects/acme"))
        assert resolve_profile(p, "/tmp/scratch") is None

    def test_longest_glob_wins(self):
        p = profiles(
            prof("personal", "*/Projects"),
            prof("work", "*/Projects/acme"),
        )
        assert resolve_profile(p, "/Users/me/Projects/acme/src") == "work"
        assert resolve_profile(p, "/Users/me/Projects/other") == "personal"

    def test_equally_specific_scopes_raise(self):
        """Two scopes of identical specificity have no principled winner. Picking
        one silently would misroute credentials, so refuse and make the user say."""
        p = profiles(
            prof("foxtrot", "*/Projects/shared"),
            prof("golf", "*/Projects/shared"),
        )
        with pytest.raises(AmbiguousScope) as exc:
            resolve_profile(p, "/Users/me/Projects/shared")
        # the user can only pick a winner if the refusal names both candidates
        assert "claimed with equal specificity by: foxtrot, golf" in str(exc.value)

    def test_a_profile_may_claim_several_scopes(self):
        p = profiles(prof("work", "*/Projects/acme", "*/work/*"))
        assert resolve_profile(p, "/Users/me/work/thing") == "work"
        assert resolve_profile(p, "/Users/me/Projects/acme") == "work"

    def test_same_profile_matching_twice_is_not_ambiguous(self):
        """Overlapping scopes on one profile agree on the answer, so there is
        nothing to disambiguate."""
        p = profiles(prof("work", "*/Projects", "*/Projects/acme"))
        assert resolve_profile(p, "/Users/me/Projects/acme") == "work"


class TestRemoteEmbedsCredential:
    """A token in a remote URL is an identity mien cannot route: git acts as its
    owner whatever profile is active, and printing the remote leaks the secret."""

    def test_flags_a_token_in_the_userinfo(self):
        assert remote_embeds_credential(
            "https://x-access-token:TOKEN@github.com/acme/repo.git"
        )
        assert remote_embeds_credential("https://user:TOKEN@github.com/acme/repo")
        assert remote_embeds_credential("HTTP://user:TOKEN@example.com/r")

    def test_flags_a_bare_token_userinfo(self):
        """`git clone https://$TOKEN@host/...` leaves the token alone in the
        userinfo; git sends it as the Basic username and it authenticates."""
        assert remote_embeds_credential(
            "https://ghp_0123456789abcdef@github.com/acme/repo.git"
        )
        assert remote_embeds_credential(
            "https://github_pat_0123456789@github.com/acme/repo"
        )

    def test_flags_the_gitlab_forms_through_the_password_branch(self):
        """GitLab does not accept a bare PAT as the whole userinfo; its real
        forms carry a `:` and are caught without any prefix of their own."""
        assert remote_embeds_credential(
            "https://gitlab-ci-token:0123456789@gitlab.com/acme/repo"
        )
        assert remote_embeds_credential("https://oauth2:0123456789@gitlab.com/acme/repo")

    def test_ignores_forms_that_carry_no_secret(self):
        """A false positive would train people to ignore the warning, so a bare
        userinfo flags only on a known token prefix — otherwise it is a username
        git prompts against, and ssh userinfo is just `git`."""
        assert not remote_embeds_credential("https://github.com/acme/repo.git")
        assert not remote_embeds_credential("https://arinyaho@github.com/acme/repo")
        assert not remote_embeds_credential("https://Ghp_notatoken@github.com/acme/r")

    def test_ignores_usernames_that_merely_start_like_a_token(self):
        """`xoxo`, `glpat-user` and `ATATuser` are legal forge usernames; only
        prefixes containing an underscore (illegal in a GitHub username) flag."""
        assert not remote_embeds_credential("https://xoxo@github.com/acme/repo")
        assert not remote_embeds_credential("https://glpat-user@gitlab.com/a/b")
        assert not remote_embeds_credential("https://ATATuser@example.com/a/b")
        assert not remote_embeds_credential("git@github.com:acme/repo.git")
        assert not remote_embeds_credential("ssh://git@github.com/acme/repo.git")
        assert not remote_embeds_credential(None)
        assert not remote_embeds_credential("")

    def test_flags_a_password_containing_an_unencoded_slash(self):
        """The `/` pushes the `@` past the first slash; reading only as far as
        that slash would call it credential-free and print part of it."""
        assert remote_embeds_credential("https://user:ab/cd@github.com/acme/x.git")

    def test_flags_a_password_containing_an_unencoded_at_sign(self):
        """Userinfo runs to the *last* `@`; splitting at the first would read
        `rest@github.com` as the host and call the remote credential-free."""
        assert remote_embeds_credential("https://user:paSS@rest@github.com/acme/x.git")

    def test_a_bracketed_ipv6_host_is_not_mistaken_for_a_credential(self):
        assert not remote_embeds_credential("https://[::1]/acme/x@v2")
        assert not remote_embeds_credential("https://[::1]:8443/acme/x")

    def test_a_flagged_remote_still_normalizes_without_the_secret(self):
        """The matching path must never carry the token into a message: an
        AmbiguousScope error or a log line prints the normalized form."""
        norm = normalize_remote("https://x-access-token:TOKEN@github.com/acme/repo.git")
        assert norm == "github.com/acme/repo"
        assert "TOKEN" not in norm
