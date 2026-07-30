import subprocess

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

    found = discover_remotes([home], origin=urls.get)
    assert [(f.provider, f.identifier) for f in found] == [
        ("remote", "github.com/acme-inc"), ("remote", "github.com/me")]
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


def test_owner_glob_claims_the_owner():
    assert owner_glob("GitHub.com/Acme/") == "github.com/acme/*"
