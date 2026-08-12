import os
import subprocess
from pathlib import Path

import pytest

from mien.config import AWSService, GitHubService, GoogleService, OCIService, Profile
from mien.discover import (Found, discover_aws, discover_gcloud, discover_github,
                           discover_oci, discover_remotes, owner_glob,
                           render_report)


def test_discover_aws_reads_config_and_credentials(tmp_path):
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "config").write_text(
        "[default]\nregion = us-east-1\n[profile work]\nregion = us-west-1\n")
    (aws / "credentials").write_text("[personal]\naws_access_key_id = AKIA\n")
    names = {f.identifier for f in discover_aws(tmp_path)}
    assert names == {"default", "work", "personal"}


def test_discover_oci_reads_sections(tmp_path):
    oci = tmp_path / ".oci"
    oci.mkdir()
    (oci / "config").write_text("[DEFAULT]\nuser = ocid1\n[work]\nuser = ocid2\n")
    assert {f.identifier for f in discover_oci(tmp_path)} == {"DEFAULT", "work"}


def test_discover_gcloud_reads_configurations(tmp_path):
    conf = tmp_path / ".config" / "gcloud" / "configurations"
    conf.mkdir(parents=True)
    (conf / "config_default").write_text("[core]\naccount = me@acme.example\n")
    (conf / "config_side").write_text("[core]\naccount = me@side.example\n")
    found = sorted(discover_gcloud(tmp_path), key=lambda f: f.identifier)
    assert [(f.identifier, f.detail) for f in found] == [
        ("default", "me@acme.example"), ("side", "me@side.example")]


def test_discover_github_parses_gh_auth_status():
    out = ("github.com\n"
           "  ✓ Logged in to github.com account octocat (keyring)\n"
           "  ✓ Logged in to github.com account octo-work (keyring)\n")
    fake = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr="")
    names = {f.identifier for f in discover_github(fake)}
    assert names == {"octocat", "octo-work"}


def test_discover_github_absent_gh_is_silent():
    def missing(*a, **k):
        raise FileNotFoundError
    assert discover_github(missing) == []


def test_render_report_marks_bound_and_unbound():
    found = [
        Found("github", "octocat", "github.com"),
        Found("github", "octo-work", "github.com"),
        Found("aws", "work"),
    ]
    profiles = {
        "personal": Profile(name="personal",
                            github=GitHubService(username="octocat",
                                                 host="github.com", token_ref="r")),
    }
    report = render_report(found, profiles)
    assert "✓ octocat (github.com) — in a mien profile" in report
    assert "· octo-work (github.com) — not imported" in report
    assert "mien login <profile> --service github --username octo-work" in report
    assert "· work — not imported" in report
    assert "--service aws --aws-profile work" in report


def test_render_report_empty():
    assert "No local" in render_report([], {})


def _repo(root, rel, url):
    """A directory that looks like a git repository, with a remote to report."""
    path = root / rel
    (path / ".git").mkdir(parents=True)
    return str(path), url


def test_discover_remotes_groups_by_owner_and_stays_in_the_tree(tmp_path):
    home = tmp_path / "home"
    urls = dict([
        _repo(home, "Projects/api", "git@github.com:acme-inc/api.git"),
        _repo(home, "Projects/web", "https://github.com/acme-inc/web.git"),
        _repo(home, "Projects/blog", "https://github.com/me/blog"),
        # Too deep for the default depth, and hidden — neither is visited.
        _repo(home, "a/b/c/deep", "https://github.com/deep/deep"),
        _repo(home, ".cache/hidden", "https://github.com/hidden/hidden"),
        # No owner segment: claiming a whole host is not something to offer.
        _repo(home, "Projects/hostonly", "git@internal.example:standalone.git"),
    ])
    outside = tmp_path / "outside"
    (outside / "secret" / ".git").mkdir(parents=True)
    urls[str(outside / "secret")] = "https://github.com/outside/secret"
    (home / "Projects" / "link").symlink_to(outside)

    visited: list[str] = []

    def origin(path):
        visited.append(path)
        return urls.get(path)

    found = discover_remotes([home], origin=origin)
    # The symlink is not followed, so the walk visits only repositories inside
    # the tree it was pointed at — asserted on what it reached, not on what a
    # lookup returns for it.
    assert visited == [str(home / "Projects" / name)
                       for name in ("api", "blog", "hostonly", "web")]
    # Every repository is reported, grouped under its owner: coverage is a
    # question about all of an owner's repositories, not about one sample.
    assert [(f.provider, f.identifier, f.detail) for f in found] == [
        ("remote", "github.com/acme-inc", "github.com/acme-inc/api"),
        ("remote", "github.com/acme-inc", "github.com/acme-inc/web"),
        ("remote", "github.com/me", "github.com/me/blog")]
    # The detail is a real remote of that owner — what a claim is verified against.
    assert found[0].detail.startswith("github.com/acme-inc/")


def test_discover_remotes_does_not_descend_into_a_repo(tmp_path):
    urls = dict([_repo(tmp_path, "Projects/api", "https://github.com/acme/api")])
    nested, _ = _repo(tmp_path / "Projects" / "api", "vendor/dep",
                      "https://github.com/other/dep")
    urls[nested] = "https://github.com/other/dep"
    assert [f.identifier for f in discover_remotes([tmp_path], origin=urls.get)] == [
        "github.com/acme"]


def test_render_report_marks_owned_remotes_and_offers_the_rest():
    found = [Found("remote", "github.com/acme-inc", "github.com/acme-inc/api"),
             Found("remote", "github.com/me", "github.com/me/blog")]
    profiles = {"work": Profile(name="work", owns_remotes=["github.com/acme-*/*"])}
    report = render_report(found, profiles)
    assert "✓ github.com/acme-inc — owned by work" in report
    assert "· github.com/me (github.com/me/blog) — no profile owns it" in report
    assert "mien discover --own github.com/me --profile <profile>" in report


def test_render_report_distinguishes_full_from_partial_coverage():
    """An owner one of whose repositories no profile resolves is not "owned":
    saying so would promise coverage `mien exec`/`guard` do not give, and would
    leave the uncovered repositories with no command to claim them."""
    found = [Found("remote", "github.com/me", "github.com/me/blog"),
             Found("remote", "github.com/me", "github.com/me/other"),
             Found("remote", "github.com/acme", "github.com/acme/api"),
             Found("remote", "github.com/acme", "github.com/acme/web")]
    profiles = {"work": Profile(name="work",
                                owns_remotes=["github.com/me/blog",
                                              "github.com/acme/*"])}
    report = render_report(found, profiles)
    # Every repository resolves → owned, and no claim is offered.
    assert "✓ github.com/acme — owned by work" in report
    # One of two does not → partly owned, with the claim that would finish it.
    assert ("~ github.com/me — partly owned by work; 1 of 2 repositories "
            "(github.com/me/other) owned by no profile") in report
    assert "mien discover --own github.com/me --profile <profile>" in report
    assert "✓ github.com/me" not in report


def test_git_repos_skips_an_unreadable_directory(tmp_path):
    """One directory with no permissions must not abort the whole inventory."""
    import stat

    if os.geteuid() == 0:
        pytest.skip("root reads everything, so nothing is denied")
    home = tmp_path / "home"
    urls = dict([_repo(home, "Projects/api", "https://github.com/acme/api")])
    # Sorts before the readable repository, so a crash here would hide it too.
    denied = home / "Projects" / "aaa-denied"
    (denied / ".git").mkdir(parents=True)
    os.chmod(denied, 0o000)
    try:
        assert [f.identifier for f in discover_remotes([home], origin=urls.get)] == [
            "github.com/acme"]
    finally:
        os.chmod(denied, stat.S_IRWXU)


def test_owner_glob_claims_the_owner():
    assert owner_glob("GitHub.com/Acme/") == "github.com/acme/*"


def test_owner_glob_treats_a_metacharacter_in_the_owner_as_data():
    """An owner is read out of a repository's remote URL, so a `*` in it is data,
    not a pattern — escaped, exactly as `resolve._expand_vars` escapes one
    arriving in a variable's value. Unescaped it would claim the whole host."""
    from mien.resolve import resolve_remote_profile

    found = discover_remotes([], origin=lambda p: None) + [
        Found("remote", "github.com/*", "github.com/*/x")]
    # Reported owner, and a hint that survives a paste into a shell.
    report = render_report(found, {})
    assert "· github.com/* (github.com/*/x) — no profile owns it" in report
    assert "mien discover --own 'github.com/*' --profile <profile>" in report

    written = owner_glob("github.com/*")
    assert written == "github.com/[*]/*"
    profiles = {"work": Profile(name="work", owns_remotes=[written])}
    # It still claims the repository it came from …
    assert resolve_remote_profile(profiles, "https://github.com/*/x.git") == "work"
    # … and nothing else on the host.
    assert resolve_remote_profile(profiles, "git@github.com:unrelated-org/svc.git") is None
    assert resolve_remote_profile(profiles, "https://github.com/anyone/anything") is None


def test_discover_remotes_flags_a_repository_whose_remote_carries_a_token(tmp_path):
    """The walk already has every repository's remote in hand, so it can answer
    the one question no per-repository command can: which repositories on this
    machine are leaking. The path is reported; the URL never is."""
    home = tmp_path / "home"
    urls = dict([
        _repo(home, "Projects/clean", "https://github.com/acme/clean.git"),
        _repo(home, "Projects/leaky",
              "https://x-access-token:SECRETVALUE@github.com/acme/leaky.git"),
        _repo(home, "Projects/bare",
              "https://ghp_000000000000000000000000000000000000@github.com/acme/bare"),
    ])
    found = discover_remotes([home], origin=urls.get)

    leaks = [f for f in found if f.provider == "leak"]
    assert [Path(f.identifier).name for f in leaks] == ["bare", "leaky"]
    assert "SECRETVALUE" not in repr(found)
    assert "ghp_" not in repr(found)
    # A leaking repository is still an ordinary repository: its owner is reported
    # too, from the userinfo-stripped form, so the claim path is unaffected.
    assert ("remote", "github.com/acme") in [(f.provider, f.identifier) for f in found]


def test_render_report_leads_with_a_leaking_remote_and_offers_no_command():
    """It leads because it is already leaking, and offers no fix command because
    where the credential lives decides the fix — only `mien doctor` can say."""
    out = render_report(
        [Found("remote", "github.com/acme", "github.com/acme/api"),
         Found("leak", "/home/me/Projects/leaky")],
        {},
    )
    assert out.splitlines()[0] == "Remotes carrying a credential:"
    assert "/home/me/Projects/leaky" in out
    assert "mien doctor" in out
    # never a claim/import hint for a leak, and never a URL
    assert "mien discover --own" not in out.split("Git remote owners:")[0]
