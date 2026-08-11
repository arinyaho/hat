from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import click

from mien.ambient import (
    AmbientParseError,
    ensure_zshenv_sources,
    unexpandable_scope_vars,
    write_ambient,
)
from mien.backends import (SecretNotFound, UnknownBackendType,
                           ensure_known_backend, load_backend)
from mien.ephemeral import EphemeralStore
from mien.config import (
    AWSService,
    ConfigError,
    AtlassianService,
    BackendConfig,
    Config,
    GitHubService,
    GoogleService,
    NotionService,
    OCIService,
    Profile,
    SecretNaming,
    SlackWorkspace,
    check_custom_var_name,
    config_path,
    load_config,
    save_config,
)
from mien.env import build_env
from mien.handover import refusal_reason
from mien.manifest import (
    MANIFEST_SECRET_NAME,
    ManifestError,
    is_cloud_backend,
    pull_manifest,
    push_manifest,
)
from mien.oauth import exchange_refresh_token, google_installed_app_flow
from mien.discover import discover_all, render_report
from mien.project import (ensure_gitignored, find_declaration, is_allowed,
                          record_allow, write_declaration)
from mien.resolve import (AmbiguousScope, claimed_profile, git_author_email,
                          git_origin_remote, profile_for_email,
                          remote_embeds_credential, resolve_profile)
from mien.verify import Status, probe_aws, probe_github, probe_google, run_probe_safely
from mien.secret_naming import BUILTIN_DEFAULT, BUILTIN_SLACK_TOKEN, render_name
from mien.shell import (CAPTURE_MARKER_VARS, custom_vars, emit_unset, emit_use,
                        render_shell_init)
from mien.statusline import guard_reason, render_segment


GOOGLE_DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/cloud-platform",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def _friendly_backend_message(exc: BaseException) -> str | None:
    """Translate noisy backend exceptions to actionable hints."""
    if isinstance(exc, UnknownBackendType):
        # Already written for a human, by the module that knows which backends
        # exist. Carrying it through here is what turns it into a clean
        # "Error: ..." instead of a traceback.
        return str(exc)

    # Before the plain-ConfigError branch: ManifestError subclasses it, and the
    # local-config advice below is all false for a manifest (wrong document,
    # wrong fix, and neither the status line nor guard is affected).
    if isinstance(exc, ManifestError):
        return (
            f"{exc}\n\n"
            f"That is the backend's config manifest (the {MANIFEST_SECRET_NAME!r} "
            "secret), not your local config — most likely written by a mien of a "
            "different version.\n"
            "Your local config parses fine, so the status line and `mien guard` are "
            "unaffected; only syncing from the backend is blocked.\n\n"
            "If your local config is the copy you want to keep, overwrite the "
            "manifest with it:\n"
            "  mien push"
        )

    if isinstance(exc, ConfigError):
        return (
            f"{exc}\n\n"
            f"The config is at {config_path()}.\n"
            # "read", not "parse": a ConfigError also covers a config that is
            # there but cannot be opened at all (permissions, a directory, a
            # dangling symlink), where "until it parses" would misdirect.
            "Until mien can read it, it cannot tell which identity is which — so "
            "the status line shows a warning in place of your identity, and `mien "
            "guard` stops enforcing (it says so instead of blocking)."
        )

    # A reference whose secret is gone. Reached from every surface that builds the
    # environment (`use`, `exec`, `run`, and `whoami --live`, which builds it to
    # probe -- each loads every ref the profile names) and from `logout` (deleting
    # one), and as a traceback it named neither the identity that is now unusable
    # nor a way out. The exception's own text carries whatever context the raiser
    # had — `build_env` adds the profile and the variable — and the advice below
    # is the part that holds wherever the dangling ref came from: a secret deleted
    # in the backend by hand, or one two references shared.
    if isinstance(exc, SecretNotFound):
        return (
            f"the secrets backend has no secret for a reference mien's config "
            f"still names: {exc}\n\n"
            "The secret was removed without the reference, so this profile cannot "
            "be activated until the two agree again.\n\n"
            "Store the secret again to make the reference live:\n"
            "  mien login <profile> --service <service>\n"
            "  mien login <profile> --service custom --name <VAR>   # a custom variable"
        )

    try:
        from google.api_core import exceptions as gerr
    except ImportError:
        gerr = None  # type: ignore[assignment]

    # Guarded: this runs inside an exception handler, and a config that itself
    # fails to parse would re-raise here and bury the original error under a
    # chained traceback.
    try:
        cfg = load_config()
    except Exception:
        cfg = None
    project = "<project>"
    account = "<bootstrap-email>"
    if cfg:
        project = cfg.secrets_backend.options.get("project", project)
        account = (cfg.bootstrap or {}).get("gcp_account", account)

    if gerr is not None and isinstance(exc, gerr.PermissionDenied):
        return (
            f"Permission denied accessing Secret Manager (project {project!r}).\n\n"
            "Most likely cause: your Application Default Credentials (ADC) is signed\n"
            "in as a different account than the mien bootstrap account.\n\n"
            "Check the current ADC account:\n"
            "  TOKEN=$(gcloud auth application-default print-access-token)\n"
            '  curl -s "https://oauth2.googleapis.com/tokeninfo?access_token=$TOKEN" | jq .email\n\n'
            f"If it isn't {account!r}, fix it:\n"
            f"  gcloud auth application-default login --account={account}\n\n"
            "Then verify with: mien doctor"
        )
    if gerr is not None and isinstance(exc, gerr.Unauthenticated):
        return (
            "No Application Default Credentials available.\n\n"
            f"  gcloud auth application-default login --account={account}\n\n"
            "Then verify with: mien doctor"
        )
    return None


class MienGroup(click.Group):
    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except click.ClickException:
            raise
        except Exception as exc:
            msg = _friendly_backend_message(exc)
            if msg:
                raise click.ClickException(msg) from exc
            raise


@click.group(cls=MienGroup)
@click.version_option(package_name="mien")
def main() -> None:
    """mien — multi-identity credential router."""
    # mien's own backend access always uses the bootstrap ADC.
    # An active profile's GOOGLE_APPLICATION_CREDENTIALS is meant for downstream
    # programs (gcloud, gh, etc.), not for mien itself — pop it so google-auth
    # falls back to ~/.config/gcloud/application_default_credentials.json.
    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)


_MARKDOWN_LINK = re.compile(r"^\[([^\]]+)\]\([^)]+\)$")


def _clean_email(s: str) -> str:
    s = s.strip()
    m = _MARKDOWN_LINK.match(s)
    return m.group(1) if m else s


def _read_secret(label: str, *, secret_cmd: str | None, from_stdin: bool) -> str:
    """Resolve a secret without it reaching argv, shell history, or ps.

    --secret-cmd: run the command, use its stdout (e.g. `op read op://...`).
                  Only the reference lands in history, never the secret.
    --token-stdin: read from a pipe.
    else: hidden interactive prompt (getpass — never echoed, never in argv).
    """
    if secret_cmd:
        try:
            out = subprocess.run(
                secret_cmd, shell=True, capture_output=True, text=True, check=True
            ).stdout
        except subprocess.CalledProcessError as exc:
            raise click.ClickException(
                f"--secret-cmd failed (exit {exc.returncode}): {(exc.stderr or '').strip()}"
            )
        secret = out.strip()
        if not secret:
            raise click.ClickException("--secret-cmd produced empty output")
        return secret
    if from_stdin:
        secret = sys.stdin.read().strip()
        if not secret:
            raise click.ClickException("--token-stdin set but stdin was empty")
        return secret
    return click.prompt(label, hide_input=True)


def _read_ssh_key(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise click.ClickException(
            f"SSH key not found at {path}.\n"
            f"Generate one with: ssh-keygen -t ed25519 -f {path}\n"
            f"Or pass an existing path with --ssh-key/--ssh-key-path."
        )


from contextlib import contextmanager


@contextmanager
def _readline_path_completion():
    """Enable tab completion for filesystem paths during a prompt."""
    try:
        import glob
        import readline
    except ImportError:
        yield
        return

    def completer(text: str, state: int):
        expanded = os.path.expanduser(text)
        matches = glob.glob(expanded + "*")
        matches = [m + "/" if os.path.isdir(m) else m for m in matches]
        if text.startswith("~"):
            home = os.path.expanduser("~")
            matches = [("~" + m[len(home):]) if m.startswith(home) else m for m in matches]
        return matches[state] if state < len(matches) else None

    prev_completer = readline.get_completer()
    prev_delims = readline.get_completer_delims()
    readline.set_completer(completer)
    readline.set_completer_delims(" \t\n")
    bind = "bind ^I rl_complete" if "libedit" in (readline.__doc__ or "") else "tab: complete"
    readline.parse_and_bind(bind)
    try:
        yield
    finally:
        readline.set_completer(prev_completer)
        readline.set_completer_delims(prev_delims)


def _validate_gcp_project_id(project: str) -> None:
    if " " in project or not project.islower():
        raise click.ClickException(
            f"{project!r} looks like a project NAME, not a PROJECT_ID.\n"
            "  Run `gcloud projects list` to find the PROJECT_ID column "
            "(lowercase, hyphens, e.g. 'my-first-project-12345')."
        )


def _verify_backend(backend, backend_type: str, bootstrap: dict) -> None:
    try:
        backend.health_check()
    except Exception as exc:
        msg = [f"backend health check failed: {exc}"]
        if backend_type == "gcp_secret_manager":
            account = bootstrap.get("gcp_account", "<bootstrap-email>")
            msg.append("")
            msg.append("Likely causes:")
            msg.append("  - Bootstrap ADC missing or for a different account.")
            msg.append("  - Bootstrap account lacks Secret Manager access on this project.")
            msg.append("")
            msg.append("Try:")
            msg.append(f"  gcloud auth application-default login --account={account}")
            msg.append(
                "  gcloud projects add-iam-policy-binding <project> \\\n"
                f"      --member=user:{account} --role=roles/secretmanager.admin"
            )
            msg.append("Then: mien doctor")
        raise click.ClickException("\n".join(msg))


@main.command("init")
@click.option("--backend", type=click.Choice(["gcp_secret_manager", "macos_keychain", "keyring"]),
              help="Skip the backend picker.")
@click.option("--project", help="(gcp) project ID")
@click.option("--bootstrap-email", help="(gcp) bootstrap account email")
@click.option("--service-prefix", default=None, help="(keychain) service prefix (default: 'mien-')")
@click.option("--yes", "-y", is_flag=True, help="Overwrite existing config and auto-import an existing backend manifest without prompting.")
@click.option("--no-import", "no_import", is_flag=True,
              help="Skip importing an existing config manifest from the backend.")
def init_cmd(
    backend: str | None,
    project: str | None,
    bootstrap_email: str | None,
    service_prefix: str | None,
    yes: bool,
    no_import: bool,
) -> None:
    """Bootstrap wizard. Supply flags for non-interactive setup; missing ones are prompted."""
    if config_path().exists():
        if yes:
            pass
        else:
            click.confirm(f"{config_path()} exists. Overwrite?", abort=True)

    if backend is None:
        click.echo("Pick a secrets backend:")
        click.echo("  1) gcp_secret_manager")
        click.echo("  2) macos_keychain")
        click.echo("  3) keyring (Linux Secret Service / Windows Credential Locker)")
        choice = click.prompt("Choice", type=click.Choice(["1", "2", "3"]))
        backend = {"1": "gcp_secret_manager", "2": "macos_keychain", "3": "keyring"}[choice]

    if backend == "gcp_secret_manager":
        if not project:
            project = click.prompt("GCP project ID").strip()
        project = project.strip()
        _validate_gcp_project_id(project)
        if not bootstrap_email:
            bootstrap_email = click.prompt("Bootstrap GCP account email")
        bootstrap_email = _clean_email(bootstrap_email)
        backend_cfg = BackendConfig(type="gcp_secret_manager", options={"project": project})
        bootstrap = {"gcp_account": bootstrap_email}
    elif backend == "keyring":
        if service_prefix is None:
            service_prefix = click.prompt("Service prefix", default="mien-")
        backend_cfg = BackendConfig(type="keyring", options={"service_prefix": service_prefix})
        bootstrap = {}
    else:  # macos_keychain
        if service_prefix is None:
            service_prefix = click.prompt("Service prefix", default="mien-")
        backend_cfg = BackendConfig(type="macos_keychain", options={"service_prefix": service_prefix})
        bootstrap = {}

    cfg = Config(
        schema_version=1,
        secrets_backend=backend_cfg,
        bootstrap=bootstrap,
        secret_naming=SecretNaming(
            default=BUILTIN_DEFAULT,
            slack_token=BUILTIN_SLACK_TOKEN,
        ),
        profiles={},
    )
    save_config(cfg)
    click.echo(f"Wrote {config_path()}.")

    backend = load_backend(cfg.secrets_backend)
    _verify_backend(backend, backend_cfg.type, bootstrap)
    click.echo(f"Backend ({backend_cfg.type}): OK")

    if backend_cfg.type == "gcp_secret_manager":
        _set_adc_quota_project(backend_cfg.options["project"])

    if not no_import and is_cloud_backend(backend_cfg):
        try:
            remote = pull_manifest(backend)
        except Exception as exc:
            remote = None
            click.echo(f"(manifest check skipped: {exc})", err=True)
        if remote and remote.profiles:
            names = ", ".join(remote.profiles)
            do_import = yes or click.confirm(
                f"Found an existing mien config in this backend "
                f"({len(remote.profiles)} profiles: {names}). Import it?",
                default=True,
            )
            if do_import:
                save_config(remote)
                first = next(iter(remote.profiles))
                click.echo(
                    f'Imported {len(remote.profiles)} profiles. '
                    f'Try: eval "$(mien use --owner-pid $$ {first})"  (or: mien-use {first})'
                )
                return

    click.echo("Next: `mien login <profile> --service google|github|slack`")



def _print_oauth_client_hint(cfg: Config) -> None:
    """Show how to create an OAuth Desktop client when none is supplied."""
    if cfg.secrets_backend.type == "gcp_secret_manager":
        project = cfg.secrets_backend.options.get("project")
        url = f"https://console.cloud.google.com/apis/credentials?project={project}"
    else:
        url = "https://console.cloud.google.com/apis/credentials"
    click.echo("Need an OAuth Desktop client. If you don't have one yet:")
    click.echo(f"  1) Open: {url}")
    click.echo("  2) Create Credentials → OAuth client ID → Application type: Desktop app")
    click.echo("  3) Copy the Client ID + Client secret, then paste below.")
    click.echo("(One Desktop client can be reused across all mien profiles.)")
    click.echo("")


def _check_adc_quota_project(expected: str | None) -> None:
    """Read ADC file and warn if quota_project_id is missing or mismatched."""
    adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if not adc_path.exists():
        click.echo("ADC: not found", err=True)
        return
    try:
        adc = json.loads(adc_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        click.echo(f"ADC: unreadable ({e})", err=True)
        return
    actual = adc.get("quota_project_id")
    if not actual:
        click.echo(
            f"ADC quota project: not set (expected {expected!r})\n"
            f"  Fix: gcloud auth application-default set-quota-project {expected}",
            err=True,
        )
    elif expected and actual != expected:
        click.echo(
            f"ADC quota project: {actual!r} (expected {expected!r})\n"
            f"  Fix: gcloud auth application-default set-quota-project {expected}",
            err=True,
        )
    else:
        click.echo(f"ADC quota project: {actual}")


def _set_adc_quota_project(project: str) -> None:
    """Pin the ADC's quota_project_id so end-user creds aren't quota-orphaned."""
    try:
        subprocess.run(
            ["gcloud", "auth", "application-default", "set-quota-project", project],
            check=True,
            capture_output=True,
        )
        click.echo(f"Set ADC quota project to {project}.")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        click.echo(
            f"warning: could not set ADC quota project ({exc}). "
            f"Run manually: gcloud auth application-default set-quota-project {project}",
            err=True,
        )


@main.command("shell-init")
@click.option(
    "--shell", "shell", default=None,
    help="Shell dialect (zsh or bash). Defaults to the one $SHELL names.",
)
def shell_init_cmd(shell: str | None) -> None:
    """Print the shell wrappers (`mien-use`, `mien-unset`) for eval.

    Add to your rc file so no repo checkout is needed:

        echo 'eval "$(mien shell-init)"' >> ~/.zshrc
    """
    if shell is None:
        shell = "bash" if os.environ.get("SHELL", "").endswith("bash") else "zsh"
    try:
        click.echo(render_shell_init(shell), nl=False)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _profiles_for_vars(consequence: str) -> dict[str, Profile]:
    """The profile map, for the two surfaces that need only variable NAMES.

    `mien unset` and `mien status` used to read no config at all; they read one
    now because the set of variables mien manages is no longer fixed — a
    profile's `custom` block names its own, and only the config knows them.

    So both must survive a config they cannot read, and neither may go quiet
    about it. `mien unset` is the sharper case: it is eval'd through the
    `mien-unset` shell wrapper (`clears="$(command mien unset)" || return $?`), so
    exiting non-zero means the wrapper never evals and NOTHING is cleared — the
    built-in scrub lost too, in a shell whose whole purpose was to stop carrying
    an identity. It therefore fails open like `guard` does, and like `guard` it
    says so: `consequence` is the caller's own "here is what you are not getting"
    clause, on stderr, which the wrapper does not capture.

    No config at all is not a failure and says nothing: an unconfigured mien has
    no custom variables to name, and `unset` still clears the built-ins.
    """
    try:
        cfg = load_config()
    except ConfigError as exc:
        click.echo(f"mien: {consequence} — config unreadable: {exc}", err=True)
        return {}
    return cfg.profiles if cfg else {}


@main.command("list")
def list_cmd() -> None:
    cfg = _require_config()
    if not cfg.profiles:
        click.echo("(no profiles configured — run `mien login <name> --service ...`)")
        return
    for name, prof in cfg.profiles.items():
        services = []
        if prof.google:
            services.append(f"google:{prof.google.email}")
        if prof.github:
            services.append(f"github:{prof.github.username}")
        if prof.slack:
            services.append(f"slack:[{', '.join(w.workspace for w in prof.slack)}]")
        if prof.aws:
            parts = []
            if prof.aws.profile:
                parts.append(f"profile={prof.aws.profile}")
            if prof.aws.region:
                parts.append(f"region={prof.aws.region}")
            services.append(f"aws({','.join(parts) if parts else 'keys'})")
        if prof.oci:
            services.append(f"oci:{prof.oci.profile or 'DEFAULT'}")
        if prof.atlassian:
            services.append(f"atlassian:{prof.atlassian.email}")
        if prof.notion:
            services.append("notion")
        if prof.custom:
            # Names only. The values are backend references rather than secrets,
            # but a reference is still a pointer at one and this is a listing.
            services.append(f"custom:[{', '.join(prof.custom)}]")
        click.echo(f"{name}\t{' '.join(services) or '(empty)'}")


@main.command("status")
def status_cmd() -> None:
    active = os.environ.get("MIEN_PROFILE")
    if not active:
        click.echo("no profile active in this shell")
        return
    click.echo(f"active: {active}")
    # The custom names come from the config, through the same `custom_vars` the
    # scrub uses, so what `status` reports set and what `unset` clears cannot
    # disagree. The built-in half is deliberately NOT the scrub list: this is a
    # display, so it omits mien's own bookkeeping variables and shows only the
    # ones a person would check — and each secret-bearing one is masked below.
    customs = custom_vars(_profiles_for_vars(
        "listing only mien's built-in variables; a custom variable may be set "
        "in this shell without appearing here, because mien could not read the "
        "config to learn its name"))
    for var in (
        "CLOUDSDK_ACTIVE_CONFIG_NAME",
        "CLOUDSDK_CORE_PROJECT",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GH_TOKEN",
        "MIEN_SLACK_TOKENS",
        "AWS_PROFILE",
        "AWS_DEFAULT_REGION",
        "AWS_ACCESS_KEY_ID",
        "OCI_CLI_PROFILE",
        "OCI_CLI_CONFIG_FILE",
        "ATLASSIAN_EMAIL",
        "ATLASSIAN_BASE_URL",
        "ATLASSIAN_API_TOKEN",
        "NOTION_TOKEN",
    ):
        if v := os.environ.get(var):
            shown = v if var not in ("GH_TOKEN", "AWS_ACCESS_KEY_ID", "ATLASSIAN_API_TOKEN", "NOTION_TOKEN") else "<set>"
            click.echo(f"  {var}={shown}")
    for var in customs:
        # Always `<set>`, never the value: every custom variable exists to carry
        # a credential, and mien has no way to know which one is harmless.
        if os.environ.get(var):
            click.echo(f"  {var}=<set>")


def _identity_card(prof: Profile) -> str:
    """One profile as one identity: every provider it bundles, in a single view.

    mien's unit is a whole human identity — cloud plus developer accounts plus
    user OAuth plus SaaS — not a secret. The card reifies that: it lists only the
    providers this profile actually is, so `work` reads as one self across all of
    them rather than a list of services. It shows names and selectors only, never
    a token.
    """
    rows: list[tuple[str, str]] = []
    if prof.google:
        rows.append(("google", prof.google.email))
    if prof.github:
        rows.append(("github", prof.github.username))
    if prof.slack:
        rows.append(("slack", ", ".join(w.workspace for w in prof.slack)))
    if prof.aws:
        region = f" ({prof.aws.region})" if prof.aws.region else ""
        rows.append(("aws", f"{prof.aws.profile or 'access key'}{region}"))
    if prof.oci:
        rows.append(("oci", prof.oci.profile or prof.oci.config_file or "configured"))
    if prof.atlassian:
        rows.append(("atlassian", f"{prof.atlassian.email} · {prof.atlassian.base_url}"))
    if prof.notion:
        rows.append(("notion", "configured"))
    if prof.custom:
        rows.append(("custom", ", ".join(prof.custom)))
    if prof.owns_remotes:
        rows.append(("owns", ", ".join(prof.owns_remotes)))
    if prof.default_for:
        rows.append(("claims", ", ".join(prof.default_for)))

    lines = [f"\033[1m{prof.name}\033[0m — one identity, every provider"]
    if rows:
        width = max(len(label) for label, _ in rows)
        lines += [f"  {label.ljust(width)}  {value}" for label, value in rows]
    else:
        lines.append("  (no providers configured yet)")
    return "\n".join(lines)


@main.command("whoami")
@click.argument("profile", required=False)
@click.option(
    "--live", is_flag=True,
    help="Ask each provider who the profile actually authenticates as, and "
    "compare to the config. Exits non-zero on any mismatch or dead credential.",
)
@click.option("--json", "as_json", is_flag=True,
              help="Emit the identity as JSON instead of the human card.")
def whoami_cmd(profile: str | None, live: bool, as_json: bool) -> None:
    cfg = _require_config()
    name = profile or os.environ.get("MIEN_PROFILE")
    if not name:
        raise click.ClickException("no profile (set $MIEN_PROFILE or pass an argument)")
    prof = cfg.profiles.get(name)
    if not prof:
        raise click.ClickException(f"profile {name!r} not found")

    if live:
        _whoami_live(cfg, prof)
        return

    if as_json:
        click.echo(json.dumps({
            "name": prof.name,
            "google": prof.google.email if prof.google else None,
            "github": prof.github.username if prof.github else None,
            "slack": [w.workspace for w in prof.slack],
            "aws": {"profile": prof.aws.profile, "region": prof.aws.region} if prof.aws else None,
            "oci": {"profile": prof.oci.profile} if prof.oci else None,
            "atlassian": {"email": prof.atlassian.email, "base_url": prof.atlassian.base_url} if prof.atlassian else None,
            "notion": True if prof.notion else None,
            # Names, not the map: the values are backend references, and this is
            # the machine-readable form of an identity, not of its storage.
            "custom": list(prof.custom),
            "owns_remotes": list(prof.owns_remotes),
            "default_for": list(prof.default_for),
        }, indent=2))
        return

    click.echo(_identity_card(prof))


def _whoami_live(cfg: Config, prof: Profile) -> None:
    """Probe each configured provider for its live identity and report it beside
    the configured value. A mismatch or a dead credential is a real problem and
    exits non-zero, so the command can gate a destructive action chained after
    it; a provider that could not be reached is surfaced but does not fail."""
    backend = load_backend(cfg.secrets_backend)
    # build_env writes plaintext credential files (Google ADC, ssh key, slack
    # tokens) to $TMPDIR/mien keyed by this process's pid. Like exec/run, this
    # command owns their whole lifetime and must delete them on the way out —
    # otherwise a verification command leaves credentials on disk.
    store = EphemeralStore()
    try:
        bundle = build_env(prof, backend, pid=store.pid)
        env = {**os.environ, **bundle.env}

        # Each probe is wrapped so an unexpected failure in one is reported, not
        # allowed to crash the whole check and hide the others.
        results = []
        if prof.github:
            results.append(run_probe_safely(
                "github", lambda: probe_github(prof.github.username, env)))
        if prof.aws:
            results.append(run_probe_safely(
                "aws", lambda: probe_aws(prof.aws.profile, env)))
        if prof.google and prof.google.refresh_token_ref and prof.google.oauth_client_secret_ref:
            results.append(run_probe_safely("google", lambda: probe_google(
                prof.google.email,
                prof.google.oauth_client_id,
                backend.get(prof.google.oauth_client_secret_ref).decode("utf-8"),
                backend.get(prof.google.refresh_token_ref).decode("utf-8"),
            )))
    finally:
        store.cleanup()

    if not results:
        # Named as what the probes actually require, not as which services exist:
        # the google probe needs a stored refresh token, so a gcloud-login-only
        # google lands here *with* a google configured — and a message listing
        # "google" as supported would read as a contradiction on that profile.
        raise click.ClickException(
            f"profile {prof.name!r} has no provider `--live` can probe — that "
            "needs github, aws, or a google with a stored refresh token "
            "(`mien login --service google`, not a gcloud-only login) — so this "
            "is 'could not check', not a wrong identity."
        )

    # Services this profile has but --live did not check. Naming them keeps a
    # clean report from reading as "everything verified" when it did not.
    # google belongs here too: a gcloud-login-only google (no client-secret /
    # refresh-token ref) fails the probe guard above, and the refresh-token probe
    # structurally cannot verify it — so it must be named, not silently dropped.
    probed = {r.service for r in results}
    configured_services = set()
    if prof.google:
        configured_services.add("google")
    if prof.slack:
        configured_services.add("slack")
    if prof.oci:
        configured_services.add("oci")
    if prof.atlassian:
        configured_services.add("atlassian")
    if prof.notion:
        configured_services.add("notion")
    if prof.custom:
        # No probe is possible: mien is told the variable name, never what the
        # credential is for. Named rather than dropped, so a clean report does
        # not read as "everything verified".
        configured_services.add("custom")
    unchecked = sorted(configured_services - probed)

    click.echo(f"profile {prof.name!r} — live identity check\n")
    width = max(len(r.service) for r in results)
    for r in results:
        if r.status is Status.UNCOMPARABLE:
            # No configured value to match against (AWS: a profile name is not an
            # ARN), so this reports the live caller — it does not verify it.
            configured = "(reported, not verified)"
        elif r.configured is not None:
            configured = r.configured
        else:
            configured = "—"
        live_str = r.live if r.live is not None else "—"
        line = (f"  {r.service:<{width}}  {r.status.value.upper():<12} "
                f"configured={configured}  live={live_str}")
        if r.detail and r.status in (Status.UNAUTHORIZED, Status.UNREACHABLE, Status.UNAVAILABLE):
            line += f"\n  {'':<{width}}  {r.detail}"
        click.echo(line)

    if unchecked:
        click.echo(f"\nnot checked (no live probe yet): {', '.join(unchecked)}")

    problems = [r for r in results if r.status in (Status.MISMATCH, Status.UNAUTHORIZED)]
    if problems:
        services = ", ".join(r.service for r in problems)
        raise click.ClickException(
            f"live identity check failed for: {services}. "
            "The active credentials do not match this profile, or are dead."
        )


def _require_config() -> Config:
    cfg = load_config()
    if cfg is None:
        raise click.ClickException("no config — run `mien init` first")
    return cfg


def _save_and_sync(cfg: Config, backend) -> None:
    save_config(cfg)
    if is_cloud_backend(cfg.secrets_backend):
        try:
            push_manifest(cfg, backend)
        except Exception as exc:
            click.echo(
                f"warning: could not sync config manifest ({exc}). "
                f"Run `mien push` later.",
                err=True,
            )


def _reject_reserved_secret_name(profile_name: str, secret_naming: SecretNaming) -> None:
    """Reject a profile whose rendered secret names would collide with the
    reserved config-manifest secret. The default template can't collide, but a
    custom template might."""
    candidates = [
        profile_name,
        render_name(secret_naming.default, profile=profile_name,
                    service="probe", kind="probe"),
        render_name(secret_naming.slack_token, profile=profile_name,
                    workspace="probe"),
    ]
    if MANIFEST_SECRET_NAME in candidates:
        raise click.ClickException(
            f"profile {profile_name!r} would collide with the reserved "
            f"config-manifest secret {MANIFEST_SECRET_NAME!r}; "
            f"rename the profile or adjust secret_naming"
        )


def _custom_var_name(service: str, name: str | None) -> str | None:
    """Resolve `--name` for `login`/`logout`: required for `custom`, refused elsewhere.

    `--name` is the environment variable a custom credential arrives as, so for
    every other service it is meaningless — and a meaningless flag that is
    silently ignored is how someone believes they stored `ANTHROPIC_API_KEY` and
    actually overwrote their github token. Both halves are errors.

    A ConfigError from the name check is translated rather than allowed to
    propagate: `MienGroup` would render it with the "the config is at ... until
    mien can read it" advice, which is false here — the config is fine, the flag
    is not.
    """
    if service != "custom":
        if name is not None:
            raise click.ClickException(
                f"--name is only meaningful with --service custom, where it names "
                f"the environment variable the secret arrives as. --service "
                f"{service} has no such name — drop --name, or pass --service "
                f"custom if a credential of your own is what you meant."
            )
        return None
    if not name:
        raise click.ClickException(
            "--name is required for --service custom: it is the environment "
            "variable the secret arrives as, e.g. --name ANTHROPIC_API_KEY."
        )
    try:
        check_custom_var_name("--name", name)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    return name


@main.command("login")
@click.argument("profile_name")
@click.option("--service", type=click.Choice(["google", "github", "slack", "aws", "oci", "atlassian", "notion", "custom"]), required=True)
@click.option("--name", "custom_name",
              help="(custom) environment variable the secret is delivered as "
                   "(e.g. ANTHROPIC_API_KEY). Required for --service custom.")
@click.option("--workspace", help="Slack workspace label (required for --service slack)")
@click.option("--email", help="(google) account email")
@click.option("--username", help="(github) username")
@click.option("--host", default="github.com", help="(github) host (for GHES)")
@click.option("--ssh-key-path", "ssh_key_path", help="(github) register SSH key by path (per-device)")
@click.option("--ssh-key", "ssh_key", help="(github) read SSH key file and store contents in the secrets backend")
@click.option("--token-stdin", "token_stdin", is_flag=True,
              help="(github/slack/aws/atlassian/notion/custom) read the secret from stdin instead of prompting")
@click.option("--secret-cmd", "secret_cmd",
              help="Run this command and use its stdout as the secret "
                   "(e.g. 'op read op://Private/item/field'). Keeps the secret out of argv/history.")
@click.option("--refresh-token-stdin", "refresh_token_stdin", is_flag=True,
              help="(google) read an existing refresh token from stdin instead of running the browser flow")
@click.option("--client-id", help="(google) OAuth client ID")
@click.option("--access-key-id", "access_key_id", help="(aws) AWS access key ID")
@click.option("--aws-profile", "aws_profile", help="(aws) existing ~/.aws profile name")
@click.option("--oci-profile", "oci_profile", help="(oci) existing ~/.oci/config profile name")
@click.option("--config-file", "config_file", help="(oci) path to OCI config file (default: ~/.oci/config)")
@click.option("--region", "region", help="(aws) default region")
@click.option("--atlassian-email", "atlassian_email", help="(atlassian) account email")
@click.option("--base-url", "base_url", help="(atlassian) base URL (e.g. https://yourco.atlassian.net)")
def login_cmd(
    profile_name: str,
    service: str,
    custom_name: str | None,
    workspace: str | None,
    email: str | None,
    username: str | None,
    host: str,
    ssh_key_path: str | None,
    ssh_key: str | None,
    token_stdin: bool,
    secret_cmd: str | None,
    refresh_token_stdin: bool,
    client_id: str | None,
    access_key_id: str | None,
    aws_profile: str | None,
    oci_profile: str | None,
    config_file: str | None,
    region: str | None,
    atlassian_email: str | None,
    base_url: str | None,
) -> None:
    # Before the config is read: a flag that cannot mean anything is a mistake to
    # report on its own terms, not one to blame a backend or a config for.
    var_name = _custom_var_name(service, custom_name)
    cfg = _require_config()
    if profile_name not in cfg.profiles:
        click.confirm(f"Profile {profile_name!r} not found. Create it?", default=False, abort=True)
    backend = load_backend(cfg.secrets_backend)
    _reject_reserved_secret_name(profile_name, cfg.secret_naming)

    if service == "github":
        prof = cfg.profiles.get(profile_name) or Profile(name=profile_name)
        gh = prof.github or GitHubService(
            username=username or "",
            host=host,
        )
        if username:
            gh.username = username
        gh.host = host

        did_something = False

        if ssh_key_path:
            gh.ssh_key_path = str(Path(ssh_key_path).expanduser())
            click.echo(f"registered ssh key path for {profile_name}: {gh.ssh_key_path}")
            did_something = True

        if ssh_key:
            content = _read_ssh_key(Path(ssh_key).expanduser())
            ref_name = render_name(cfg.secret_naming.default, profile=profile_name, service="github", kind="ssh_key")
            gh.ssh_key_ref = backend.put(ref_name, content)
            click.echo(f"stored ssh key for {profile_name} at {gh.ssh_key_ref}")
            did_something = True

        if not (ssh_key_path or ssh_key):
            if not gh.username:
                gh.username = click.prompt("GitHub username")
            token = _read_secret("Paste a GitHub token", secret_cmd=secret_cmd, from_stdin=token_stdin)
            ref_name = render_name(cfg.secret_naming.default, profile=profile_name, service="github", kind="token")
            gh.token_ref = backend.put(ref_name, token.encode("utf-8"))
            click.echo(f"stored github token for {profile_name} at {gh.token_ref}")
            did_something = True

            if not token_stdin and click.confirm("Also register an SSH key for git operations?", default=False):
                default_path = str(Path.home() / ".ssh" / "id_ed25519")
                with _readline_path_completion():
                    path = click.prompt("SSH private key path", default=default_path)
                expanded = Path(path).expanduser()
                if not expanded.exists():
                    raise click.ClickException(
                        f"SSH key not found at {expanded}.\n"
                        f"Generate one with: ssh-keygen -t ed25519 -f {expanded}"
                    )
                storage = click.prompt(
                    "Store key in the secrets backend (sm) or remember the path only (path)?",
                    type=click.Choice(["sm", "path"]),
                    default="sm",
                )
                if storage == "path":
                    gh.ssh_key_path = str(expanded)
                    click.echo(f"registered ssh key path: {gh.ssh_key_path}")
                else:
                    content = expanded.read_bytes()
                    ssh_ref_name = render_name(cfg.secret_naming.default, profile=profile_name, service="github", kind="ssh_key")
                    gh.ssh_key_ref = backend.put(ssh_ref_name, content)
                    click.echo(f"stored ssh key contents at {gh.ssh_key_ref}")

        if did_something:
            prof.github = gh
            cfg.profiles[profile_name] = prof
            _save_and_sync(cfg, backend)
        return

    if service == "slack":
        if not workspace:
            raise click.ClickException("--workspace is required for --service slack")
        token = _read_secret("Paste a Slack user token (xoxp-...)", secret_cmd=secret_cmd, from_stdin=token_stdin)
        ref_name = render_name(cfg.secret_naming.slack_token, profile=profile_name, workspace=workspace)
        ref = backend.put(ref_name, token.encode("utf-8"))
        prof = cfg.profiles.get(profile_name) or Profile(name=profile_name)
        prof.slack = [w for w in prof.slack if w.workspace != workspace]
        prof.slack.append(SlackWorkspace(workspace=workspace, user_token_ref=ref))
        cfg.profiles[profile_name] = prof
        _save_and_sync(cfg, backend)
        click.echo(f"stored slack token for {profile_name}/{workspace} at {ref}")
        return

    if service == "google":
        email = email or click.prompt("Google account email")
        if not client_id:
            _print_oauth_client_hint(cfg)
            client_id = click.prompt("OAuth client ID")
        client_secret = _read_secret("OAuth client secret", secret_cmd=secret_cmd, from_stdin=False)

        if refresh_token_stdin:
            refresh = sys.stdin.read().strip()
            if not refresh:
                raise click.ClickException("--refresh-token-stdin set but stdin was empty")
        else:
            refresh = google_installed_app_flow(
                client_id=client_id,
                client_secret=client_secret,
                scopes=GOOGLE_DEFAULT_SCOPES,
            )
        oauth_secret_ref = backend.put(
            render_name(cfg.secret_naming.default, profile=profile_name, service="google", kind="oauth_client_secret"),
            client_secret.encode("utf-8"),
        )
        refresh_ref = backend.put(
            render_name(cfg.secret_naming.default, profile=profile_name, service="google", kind="refresh"),
            refresh.encode("utf-8"),
        )

        prof = cfg.profiles.get(profile_name) or Profile(name=profile_name)
        prof.google = GoogleService(
            email=email,
            oauth_client_id=client_id,
            oauth_client_secret_ref=oauth_secret_ref,
            refresh_token_ref=refresh_ref,
            adc_ref=None,
            gcloud_config_name=profile_name,
            default_project=None,
        )
        cfg.profiles[profile_name] = prof
        _save_and_sync(cfg, backend)
        click.echo(f"stored google identity for {profile_name}")
        return

    if service == "aws":
        prof = cfg.profiles.get(profile_name) or Profile(name=profile_name)
        aws = prof.aws or AWSService()
        if region:
            aws.region = region
        if aws_profile:
            aws.profile = aws_profile
        if access_key_id:
            key_id = access_key_id
            secret = _read_secret("AWS secret access key", secret_cmd=secret_cmd, from_stdin=token_stdin)
            ref_id = backend.put(
                render_name(cfg.secret_naming.default, profile=profile_name, service="aws", kind="access_key_id"),
                key_id.encode("utf-8"),
            )
            ref_secret = backend.put(
                render_name(cfg.secret_naming.default, profile=profile_name, service="aws", kind="secret_access_key"),
                secret.encode("utf-8"),
            )
            aws.access_key_id_ref = ref_id
            aws.secret_access_key_ref = ref_secret
            click.echo(f"stored AWS credentials for {profile_name}")
        elif not aws_profile and not region:
            choice = click.prompt(
                "Store AWS credentials (keys) or reference an existing ~/.aws profile?",
                type=click.Choice(["keys", "profile"]),
                default="keys",
            )
            if choice == "profile":
                aws.profile = click.prompt("~/.aws profile name", default="default")
                aws.region = click.prompt("Default region (optional, blank to skip)", default="", show_default=False) or None
            else:
                key_id = click.prompt("AWS access key ID")
                secret = _read_secret("AWS secret access key", secret_cmd=secret_cmd, from_stdin=False)
                aws.region = click.prompt("Default region (optional, blank to skip)", default="", show_default=False) or None
                ref_id = backend.put(
                    render_name(cfg.secret_naming.default, profile=profile_name, service="aws", kind="access_key_id"),
                    key_id.encode("utf-8"),
                )
                ref_secret = backend.put(
                    render_name(cfg.secret_naming.default, profile=profile_name, service="aws", kind="secret_access_key"),
                    secret.encode("utf-8"),
                )
                aws.access_key_id_ref = ref_id
                aws.secret_access_key_ref = ref_secret
                click.echo(f"stored AWS credentials for {profile_name}")
        prof.aws = aws
        cfg.profiles[profile_name] = prof
        _save_and_sync(cfg, backend)
        return

    if service == "oci":
        prof = cfg.profiles.get(profile_name) or Profile(name=profile_name)
        oci = prof.oci or OCIService()
        if oci_profile:
            oci.profile = oci_profile
        if config_file:
            oci.config_file = config_file
        if not oci_profile and not config_file:
            oci.profile = click.prompt("~/.oci/config profile name", default="DEFAULT")
            cf = click.prompt("Custom OCI config file path (blank for default ~/.oci/config)", default="", show_default=False)
            if cf:
                oci.config_file = str(Path(cf).expanduser())
        prof.oci = oci
        cfg.profiles[profile_name] = prof
        _save_and_sync(cfg, backend)
        click.echo(f"stored OCI identity for {profile_name} (profile={oci.profile!r})")
        return

    if service == "atlassian":
        prof = cfg.profiles.get(profile_name) or Profile(name=profile_name)
        email_val = atlassian_email or (prof.atlassian.email if prof.atlassian else None) or click.prompt("Atlassian account email")
        url = base_url or (prof.atlassian.base_url if prof.atlassian else None) or click.prompt("Atlassian base URL (e.g. https://yourco.atlassian.net)")
        token = _read_secret("Atlassian API token", secret_cmd=secret_cmd, from_stdin=token_stdin)
        ref = backend.put(
            render_name(cfg.secret_naming.default, profile=profile_name, service="atlassian", kind="api_token"),
            token.encode("utf-8"),
        )
        prof.atlassian = AtlassianService(email=email_val, base_url=url.rstrip("/"), api_token_ref=ref)
        cfg.profiles[profile_name] = prof
        _save_and_sync(cfg, backend)
        click.echo(f"stored atlassian identity for {profile_name} at {ref}")
        return

    if service == "notion":
        prof = cfg.profiles.get(profile_name) or Profile(name=profile_name)
        token = _read_secret("Notion integration token", secret_cmd=secret_cmd, from_stdin=token_stdin)
        ref = backend.put(
            render_name(cfg.secret_naming.default, profile=profile_name, service="notion", kind="api_token"),
            token.encode("utf-8"),
        )
        prof.notion = NotionService(api_token_ref=ref)
        cfg.profiles[profile_name] = prof
        _save_and_sync(cfg, backend)
        click.echo(f"stored notion identity for {profile_name} at {ref}")
        return

    if service == "custom":
        # `var_name` is a validated variable name here — non-empty, a shell
        # identifier, and not a built-in's. See `_custom_var_name`, which is the
        # only thing that can put a non-None value there.
        prof = cfg.profiles.get(profile_name) or Profile(name=profile_name)
        secret = _read_secret(
            f"Secret for {var_name}", secret_cmd=secret_cmd, from_stdin=token_stdin)
        # The same `default` template every other service renders through, with
        # the variable name verbatim as the `kind` —
        # `mien-work-custom-ANTHROPIC_API_KEY`.
        #
        # Verbatim, deliberately: environment variable names are case-sensitive,
        # so `TOKEN` and `token` are two different variables carrying two
        # different secrets. Case-folding the kind rendered ONE secret name for
        # both, so the second login overwrote the first login's secret and the
        # config kept two names pointing at the survivor — silent credential
        # destruction, and a dangling ref as soon as either was logged out.
        # Every backend takes the name as written: GCP secret IDs allow
        # `[A-Za-z0-9_-]`, and Keychain/keyring match their service+account
        # attributes case-sensitively.
        ref = backend.put(
            render_name(cfg.secret_naming.default, profile=profile_name,
                        service="custom", kind=var_name),
            secret.encode("utf-8"),
        )
        # Only the reference is stored. That is what keeps config.json and the
        # backend manifest free of the secret — the way `project_env` is not.
        prof.custom[var_name] = ref
        cfg.profiles[profile_name] = prof
        _save_and_sync(cfg, backend)
        click.echo(f"stored {var_name} for {profile_name} at {ref}")
        return


def _stdout_is_tty() -> bool:
    """Indirection so tests can flip the heuristic without monkey-patching
    sys.stdout (which Click's CliRunner replaces during invoke)."""
    return sys.stdout.isatty()


@main.command("use")
@click.argument("profile_name")
@click.option("--print", "force_print", is_flag=True,
              help="Force emitting the loader to stdout even if stdout is a TTY. "
                   "Use only when you understand the snippet sources an env file "
                   "and won't paste the path anywhere it shouldn't go.")
@click.option("--owner-pid", type=int, default=None,
              help="PID that owns the ephemeral files' lifetime. The mien-use "
                   "wrapper passes $$ (the calling shell) so the files survive as "
                   "long as that shell — not just this short-lived process.")
def use_cmd(profile_name: str, force_print: bool, owner_pid: int | None) -> None:
    if _stdout_is_tty() and not force_print:
        raise click.ClickException(
            "stdout is a TTY — refusing to emit the env loader.\n"
            "`mien use` is meant to be eval'd, not run interactively.\n\n"
            "Use the wrapper (recommended):\n"
            f"  mien-use {profile_name}\n\n"
            "Or eval directly:\n"
            f'  eval "$(mien use --owner-pid $$ {profile_name})"\n\n'
            "If you really need raw output, pass --print."
        )
    cfg = _require_config()
    prof = cfg.profiles.get(profile_name)
    if not prof:
        raise click.ClickException(f"profile {profile_name!r} not found")
    backend = load_backend(cfg.secrets_backend)
    # Key the ephemeral files to the owner (the calling shell, via the wrapper's
    # $$) rather than this process. `use` deliberately does NOT clean them up —
    # the shell sources them after we exit — so their lifetime must be the
    # shell's. Attributed to this short-lived process instead, gc would see a
    # dead pid the instant we return and delete credentials still in use.
    bundle = build_env(prof, backend, pid=owner_pid)
    # Every profile's map, not just this one's: the loader scrubs whatever the
    # shell was carrying before, which includes custom variables only some OTHER
    # profile defines. Read from the config already in hand — `use` fails hard on
    # an unreadable config, so there is no fail-open branch to make here.
    sys.stdout.write(emit_use(bundle, cfg.profiles))


def _profile_fingerprint(prof) -> str:
    """Stable JSON of a Profile for change detection. sort_keys neutralises
    dict ordering; assumes all profile fields are JSON-serializable (they are:
    str/None/bool and lists of dataclasses)."""
    return json.dumps(asdict(prof), sort_keys=True)


@main.command("sync")
@click.option("--dry-run", "dry_run", is_flag=True, help="Show what would change; write nothing.")
@click.option("--yes", "-y", is_flag=True, help="Apply without confirmation.")
def sync_cmd(dry_run: bool, yes: bool) -> None:
    """Pull the config manifest from the backend and reconcile local config."""
    cfg = _require_config()
    ensure_known_backend(cfg.secrets_backend)
    if not is_cloud_backend(cfg.secrets_backend):
        raise click.ClickException("sync requires a cloud backend (gcp_secret_manager)")
    backend = load_backend(cfg.secrets_backend)
    remote = pull_manifest(backend)
    if remote is None:
        raise click.ClickException("no manifest found in backend (nothing to sync)")

    local, rem = set(cfg.profiles), set(remote.profiles)
    added = sorted(rem - local)
    removed = sorted(local - rem)
    changed = sorted(
        n for n in local & rem
        if _profile_fingerprint(cfg.profiles[n]) != _profile_fingerprint(remote.profiles[n])
    )
    click.echo(f"+ add:    {', '.join(added) or '(none)'}")
    click.echo(f"- remove: {', '.join(removed) or '(none)'}")
    click.echo(f"~ change: {', '.join(changed) or '(none)'}")

    if dry_run:
        return
    if not (added or removed or changed):
        click.echo("already in sync")
        return
    if removed:
        click.echo(
            f"WARNING: these local-only profiles will be DROPPED: {', '.join(removed)}",
            err=True,
        )
    if not yes:
        click.confirm("Replace local config with the manifest?", default=True, abort=True)
    save_config(remote)
    click.echo(f"synced {len(remote.profiles)} profiles from manifest")


@main.command("push")
def push_cmd() -> None:
    """Force-push the current local config to the backend manifest."""
    cfg = _require_config()
    # Before the local/cloud split: a backend type mien no longer knows is not a
    # local backend, and must not be reported as a successful no-op.
    ensure_known_backend(cfg.secrets_backend)
    if not is_cloud_backend(cfg.secrets_backend):
        # Intentionally exit 0 (not an error like sync): pushing a manifest to a
        # local-only backend is simply meaningless, not a user mistake.
        click.echo("push is a no-op for local backends (macos_keychain, keyring)")
        return
    backend = load_backend(cfg.secrets_backend)
    push_manifest(cfg, backend)
    click.echo("pushed config manifest to backend")


@main.group("env", cls=MienGroup)
def env_group() -> None:
    """Manage ambient per-project env (non-interactive zsh only)."""


def _warn_unexpandable_scopes(profiles: dict[str, Profile]) -> None:
    """Warn about `project_env` scopes whose variables are unset where they run.

    The generated script is sourced from `~/.zshenv`, which zsh reads BEFORE
    `~/.zshrc` and `~/.zprofile` — so a variable the user exports from their own
    dotfiles is unset by construction at match time. zsh expands it to nothing,
    and `case "$PWD/" in $WORK_ROOT/*)` becomes `/*`, which matches every
    absolute path: every shell would get that scope's env, including the
    credential-selecting kind (`AWS_PROFILE`).

    Warn and continue rather than reject: a config that works today (because the
    variable happens to be exported early enough, or because the scope's other
    segments make the collapse harmless) must not start failing on upgrade.
    """
    for name in sorted(profiles):
        for scope in profiles[name].project_env:
            missing = unexpandable_scope_vars(scope.match)
            if not missing:
                continue
            refs = ", ".join(f"${v}" for v in missing)
            click.echo(
                f"warning: profile {name!r} scope {scope.match!r} refers to {refs}, "
                "which will be unset where it is evaluated: the generated script is "
                "sourced from ~/.zshenv, and zsh reads that BEFORE ~/.zshrc and "
                "~/.zprofile, so variables defined there do not exist yet. zsh "
                "expands the reference to nothing, which can widen the scope to "
                "directories you did not intend. Write a literal path instead, or "
                "'~', which expands correctly this early.",
                err=True,
            )


@env_group.command("sync")
def env_sync_cmd() -> None:
    """Generate ~/.config/mien/ambient.zsh from every profile's project_env and
    ensure ~/.zshenv sources it. Non-secret only. Idempotent. The generated
    script is `zsh -n`-validated before anything is written or wired."""
    cfg = _require_config()
    _warn_unexpandable_scopes(cfg.profiles)
    try:
        ambient = write_ambient(cfg.profiles)          # renders + parse-gates + writes
    except AmbientParseError as exc:
        raise click.ClickException(
            f"Generated ambient script does not parse; nothing written.\n{exc}\n"
            "Check your project_env values (unbalanced quotes, stray newlines)."
        )
    zshenv = Path(os.environ.get("HOME", str(Path.home()))) / ".zshenv"
    changed = ensure_zshenv_sources(zshenv, ambient)
    total = 0
    for name in sorted(cfg.profiles):
        n = len(cfg.profiles[name].project_env)
        if n:
            click.echo(f"  {name}: {n} scope(s)")
            total += n
    click.echo(f"Wrote {total} scope(s) to {ambient}")
    click.echo(f"~/.zshenv {'updated' if changed else 'already wired'}: {zshenv}")
    if total == 0:
        click.echo("No project_env scopes configured. Add them under a profile's "
                   "project_env and re-run. (Non-interactive zsh only in v1.)")


@main.command("unset")
def unset_cmd() -> None:
    """Print the `unset` lines that clear every variable mien manages.

    Meant to be eval'd — the `mien-unset` wrapper from `mien shell-init` does it.
    The list is the built-ins plus every `custom` variable any profile defines, so
    it reads the config; when it cannot, it clears the built-ins and says on
    stderr that the custom ones may survive (see `_profiles_for_vars`).
    """
    sys.stdout.write(emit_unset(_profiles_for_vars(
        "unsetting only mien's built-in variables; any custom variable a profile "
        "defines may still be set in this shell, because mien could not read the "
        "config to learn its name")))


def _run_as_profile(cfg: Config, prof: Profile, argv: tuple[str, ...]) -> None:
    """Run argv with the profile's env, then exit with the child's status.

    Shared by `exec` and `run` so the cleanup below cannot drift between them.

    Whichever command spawns the child owns its whole lifetime, so it also owns
    the plaintext credential files build_env drops in $TMPDIR/mien (ADC blob with
    the client_secret + refresh_token, ssh key, slack token map). Unlike `use`,
    nothing downstream needs them to survive this process — and no shell EXIT trap
    fires for these paths, since MIEN_PROFILE is only ever set in the child's
    environment. Clean up unconditionally: normal exit, non-zero exit, child
    killed by a signal, Ctrl-C, or an exception.
    """
    backend = load_backend(cfg.secrets_backend)
    store = EphemeralStore()
    try:
        bundle = build_env(prof, backend, pid=store.pid)
        env = {**os.environ, **bundle.env}
        rc = subprocess.call(list(argv), env=env)
    finally:
        store.cleanup()
    sys.exit(rc)


def _declaration_here(cfg: Config, cwd: str) -> tuple[str | None, bool]:
    """The `.mien` profile declared at or above ``cwd``, and whether it counts.

    A declaration counts only when the user approved that exact (path, profile)
    and the profile is still in the config — a checked-out file may not choose an
    identity, and a renamed profile leaves a stale approval behind. The raw name
    comes back too, for the callers that say something about a declaration they
    are declining to honour.

    One implementation, because two readers that disagree about what "declared
    here" means is a bug the user experiences as `mien which` and `mien exec`
    answering differently in the same directory.
    """
    declared, decl_path = find_declaration(cwd)
    approved = bool(
        declared and decl_path and declared in cfg.profiles
        and is_allowed(decl_path, declared)
    )
    return declared, approved


def _refuse_wrong_identity(cfg: Config, profile_name: str) -> None:
    """Block an agent-driven `exec` that names an identity this place disowns.

    Gathers the signals and lets `handover.refusal_reason` decide, the way
    `guard_cmd` defers to `guard_reason` — the policy stays out of this file and
    stays testable without a CLI runner. An approved `.mien` is one of those
    signals and outranks the rest, so the gate agrees with every other resolver
    about a workspace the user bound by hand.

    `MIEN_EXEC=off` disarms it, following `MIEN_GUARD`. That escape is for a
    person debugging a false refusal, so it is documented in the README and
    deliberately absent from the refusal text: an agent that reads the error
    must not be handed the bypass in the same breath.

    Fail open on anything unexpected, exactly as `guard` does. This runs before
    a backend is loaded, so a refusal also spends no credential.
    """
    if os.environ.get("MIEN_EXEC", "").strip().lower() in _GUARD_OFF:
        return
    try:
        cwd = _logical_cwd()
        declared, declared_ok = _declaration_here(cfg, cwd)
        reason = refusal_reason(
            cfg.profiles, cwd, profile_name,
            remote=git_origin_remote(cwd),
            declared=declared if declared_ok else None,
            agent_driven=capture_context() is not None,
        )
    except Exception:
        return  # never wedge a handover because the check itself broke.
    if reason:
        raise click.ClickException(reason)


@main.command("exec", context_settings={"ignore_unknown_options": True})
@click.argument("profile_name")
@click.argument("argv", nargs=-1, required=True)
def exec_cmd(profile_name: str, argv: tuple[str, ...]) -> None:
    """Run a command as `profile_name`, with that identity's env.

    Refuses when an agent harness is driving the call and this place visibly
    belongs to a different profile — see `_refuse_wrong_identity`. A person at a
    terminal never triggers that check.
    """
    cfg = _require_config()
    prof = cfg.profiles.get(profile_name)
    if not prof:
        raise click.ClickException(f"profile {profile_name!r} not found")
    # Before any credential is loaded: a refused handover must cost nothing.
    _refuse_wrong_identity(cfg, profile_name)
    _run_as_profile(cfg, prof, argv)


def _logical_cwd() -> str:
    """The working directory as the shell names it: `$PWD` when it is honest.

    `os.getcwd()` resolves symlinks; the shell's `$PWD` does not. Standing in a
    directory reached through a symlink — `/tmp` -> `/private/tmp`, a relocated
    home, a projects tree on an external volume — the two disagree, and a scope
    like '*/Projects/acme' that the generated `case "$PWD/" in ...` matches would
    not match the physical path. Preferring `$PWD` keeps identity resolution
    answering about the same directory ambient env answers about.

    `$PWD` is trusted only after `os.path.samefile` confirms it is the directory we
    are actually in: it is inherited across `cd`-less subprocesses and can be stale,
    unset, or a lie, and a stale value would resolve to some other project's
    credentials. Any doubt falls back to the physical path.
    """
    physical = os.getcwd()
    pwd = os.environ.get("PWD")
    if pwd and os.path.isabs(pwd) and pwd != physical:
        try:
            if os.path.samefile(pwd, physical):
                return pwd
        except OSError:
            pass
    return physical


def _resolve_cwd_profile(cfg: Config) -> str | None:
    """Profile claimed by the current directory, honouring an explicit override.

    An activated MIEN_PROFILE wins: someone ran `mien use` on purpose and a
    directory default must not quietly undo that. The disagreement is still
    reported, because acting against the directory's default without noticing is
    the confusion this resolution exists to remove.

    An ambiguous directory only blocks the commands that would otherwise have to
    guess. With an override in hand there is nothing to guess, so the clash is
    reported on stderr and the activated profile is used.

    A name returned from here is always a profile that exists. Directory scopes
    come from the config, so only the override can name something else — a
    renamed or deleted profile leaves a stale MIEN_PROFILE exported in shells
    that are still open. Rejecting it here, rather than in each caller, keeps
    `which` from printing a name its own consumers cannot use.
    """
    active = os.environ.get("MIEN_PROFILE")
    if active and active not in cfg.profiles:
        raise click.ClickException(
            f"profile {active!r} is active in this shell but not found in the "
            "config; it may have been renamed or removed. Clear it with "
            "`mien-unset` (bare `mien unset` only prints the commands — it "
            "cannot change the calling shell), or activate an existing profile."
        )

    # A project-local `.mien` declaration outranks central default_for scopes —
    # but only once approved. An unapproved (or stale, or unknown-profile)
    # declaration must never silently route: a checked-out file cannot choose an
    # identity that acts until the user runs `mien allow`.
    declared, decl_path = find_declaration(_logical_cwd())
    if declared and decl_path:
        if is_allowed(decl_path, declared) and declared in cfg.profiles:
            if active and active != declared:
                click.echo(
                    f"warning: this directory declares {declared!r} (.mien), but "
                    f"{active!r} is active in this shell; using {active!r}", err=True)
            return active or declared
        if active:
            click.echo(
                f"warning: this directory declares {declared!r} (.mien) but it is "
                f"not allowed; using the active {active!r}. Run `mien allow` to "
                f"trust it.", err=True)
            return active
        missing = "" if declared in cfg.profiles else \
            f" (and {declared!r} is not in your config)"
        raise click.ClickException(
            f"this directory declares profile {declared!r} in .mien but it is not "
            f"allowed yet{missing}. Run `mien allow` to approve it — a checked-out "
            f".mien cannot choose an identity until you do — or remove the file.")

    try:
        from_dir = resolve_profile(cfg.profiles, _logical_cwd())
    except AmbiguousScope as exc:
        if not active:
            raise click.ClickException(str(exc)) from exc
        click.echo(
            f"warning: this directory is claimed by several profiles with equal "
            f"specificity, but {active!r} is active in this shell; using {active!r}",
            err=True,
        )
        return active

    if active:
        if from_dir and from_dir != active:
            click.echo(
                f"warning: this directory defaults to {from_dir!r}, but "
                f"{active!r} is active in this shell; using {active!r}",
                err=True,
            )
        return active
    return from_dir


@main.command("which")
def which_cmd() -> None:
    """Print the profile for the current directory, or exit non-zero."""
    name = _resolve_cwd_profile(_require_config())
    if not name:
        # Deliberately silent on stdout: callers substitute this into other
        # commands, so printing anything here would be taken for a profile name.
        raise click.ClickException(
            f"no profile claims {_logical_cwd()}. Declare one with `mien claim "
            "<profile>` (writes a local .mien), add a default_for scope, or name "
            "one explicitly."
        )
    click.echo(name)


@main.command("discover")
def discover_cmd() -> None:
    """Inventory the identities already on this machine and what to import.

    Read-only: it inspects local AWS/OCI profiles, gcloud configurations, and
    GitHub accounts, and reports which are already bound to a mien profile and
    which are not — with the `mien login` command to import each. It reads no
    secret and writes nothing; importing stays an explicit act.
    """
    cfg = load_config()
    profiles = cfg.profiles if cfg else {}
    click.echo(render_report(discover_all(), profiles))


@main.command("allow")
def allow_cmd() -> None:
    """Approve the project-local `.mien` declaration so it can drive identity here.

    A checked-out `.mien` names a profile but does not act until you approve this
    exact (path, profile) — so a cloned repository's `.mien` is inert until you
    say so, and an edited one must be re-approved. Also adds `.mien` to your
    global git ignore, since it is a private, local marker.
    """
    cfg = _require_config()
    declared, decl_path = find_declaration(_logical_cwd())
    if not declared or not decl_path:
        raise click.ClickException(
            "no .mien declaration found here or above. Create one with "
            "`mien claim <profile>`.")
    if declared not in cfg.profiles:
        raise click.ClickException(
            f".mien declares {declared!r}, which is not in your config.")
    record_allow(decl_path, declared)
    ensure_gitignored()
    click.echo(f"allowed: this workspace acts as {declared} ({decl_path}).")


@main.command("claim")
@click.argument("profile", required=False)
def claim_cmd(profile: str | None) -> None:
    """Bind this directory to a profile in one step.

    Writes a local `.mien` naming the profile, adds `.mien` to your global git
    ignore, and approves it — so `mien run`/`which` here (and everything beneath)
    act as that profile with no central scope to maintain.

    Called with no argument, it is the friendly front door: if a `.mien` already
    names a profile here it just approves it; otherwise it asks which of your
    configured identities this workspace is. Pass the profile explicitly in a
    non-interactive shell (an agent, a script).
    """
    cfg = _require_config()
    if not cfg.profiles:
        raise click.ClickException(
            "no profiles configured yet. Set one up with `mien login` first.")
    if profile is None:
        declared, _ = find_declaration(_logical_cwd())
        if declared and declared in cfg.profiles:
            profile = declared
            click.echo(f"found a .mien declaring {declared!r} — approving it.")
        else:
            names = sorted(cfg.profiles)
            click.echo("Which identity is this workspace?")
            for index, name in enumerate(names, 1):
                click.echo(f"  {index}. {name}")
            picked = click.prompt("Pick", type=click.IntRange(1, len(names)))
            profile = names[picked - 1]
    if profile not in cfg.profiles:
        raise click.ClickException(f"profile {profile!r} not found")
    decl_path = write_declaration(_logical_cwd(), profile)
    record_allow(decl_path, profile)
    ensure_gitignored()
    click.echo(
        f"this directory now acts as {profile}: wrote {decl_path}, approved it, "
        "and git-ignored .mien globally.")


def _statusline_cwd() -> str:
    """The directory to resolve identity for, from Claude Code's status-line JSON.

    Claude Code pipes a session object on stdin; `workspace.current_dir` (falling
    back to `cwd`) is the directory the session is in — which is NOT this
    process's own cwd, so it must be read from the payload. Any missing or
    malformed input falls back to the process cwd rather than failing.
    """
    data: dict = {}
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                parsed = json.loads(raw)
                # Valid JSON that is not an object (`[]`, `5`, `null`) parses
                # fine but has no `.get`; ignore it and fall back to the cwd,
                # rather than let an AttributeError skip the fallback.
                if isinstance(parsed, dict):
                    data = parsed
    except (OSError, ValueError):
        data = {}
    workspace = data.get("workspace") or {}
    return workspace.get("current_dir") or data.get("cwd") or os.getcwd()


def _identity_segment(cfg: Config, cwd: str) -> str:
    """Render the identity segment for ``cwd`` — shared by `statusline` (cwd from
    Claude Code's JSON) and `prompt` (cwd from the shell). Compares the active
    `MIEN_PROFILE` against whose place this is (repo remote owner, then a
    directory scope) and the git author a commit would carry. Secret-free: reads
    only profile names and scopes, never the backend.

    Remote/author are advisory display only, never an acting-identity choice — a
    checked-out repo controls both, so they inform this segment, not run/exec.
    """
    env_profile = os.environ.get("MIEN_PROFILE") or None
    env_unknown = bool(env_profile and env_profile not in cfg.profiles)
    # Before any of the identity comparisons: a token embedded in the remote URL
    # decides who git acts as regardless of what mien routed, and leaks itself
    # into every log that prints the remote. Nothing below can outrank that.
    origin = git_origin_remote(cwd)
    if remote_embeds_credential(origin):
        return render_segment(None, None, remote_credential=True)
    # A project-local `.mien` declaration, if present. Approved → it is the claim
    # (it outranks central scopes, as in resolution); present-but-unapproved →
    # surfaced as pending so the segment invites `mien allow` rather than acting.
    declared, declared_ok = _declaration_here(cfg, cwd)
    if declared and not declared_ok and not env_profile:
        return render_segment(None, None, pending=declared)
    claimed: str | None = None
    source: str | None = "dir"
    ambiguous = False
    if declared_ok:
        claimed, source = declared, "dir"
    else:
        try:
            claimed, source = claimed_profile(cfg.profiles, cwd, remote=origin)
        except AmbiguousScope:
            ambiguous = True
    author_email = git_author_email(cwd)
    author = profile_for_email(cfg.profiles, author_email) if author_email else None
    return render_segment(
        env_profile, claimed,
        source=source or "dir", author_profile=author,
        ambiguous=ambiguous, env_unknown=env_unknown,
    )


@main.command("statusline")
def statusline_cmd() -> None:
    """Emit a one-line mien identity segment for a Claude Code status line.

    Wire it up in `.claude/settings.json`:

        "statusLine": { "type": "command", "command": "mien statusline" }

    It reads the session JSON Claude Code passes on stdin (for the directory),
    then compares what `MIEN_PROFILE` is set to against whose place this is — the
    repository's `origin` owner (`owns_remotes`) first, then a directory
    (`default_for`) scope — and prints a coloured segment: green when they agree,
    red when the active identity is wrong here, which is the mis-commit this
    exists to catch. The remote owner is advisory only (display and warning); it
    never selects an identity that acts, since a checked-out repo controls it.

    Secret-free and offline: it reads only the config's profile names and scopes,
    never the backend, so it is cheap enough to run at status-line frequency. And
    it never errors out — a status line that crashes is worse than a blank one —
    so any failure exits 0: no config or unreadable input prints nothing. An
    unreadable config is the one exception: it shows a compact marker instead of
    staying blank, because a blank segment reads as "nothing to report" when in
    fact mien can no longer tell who you are here.
    """
    try:
        cfg = load_config()
        if cfg is None:
            return  # mien is not set up here — stay silent rather than nag.
        click.echo(_identity_segment(cfg, _statusline_cwd()))
    except ConfigError:
        # Deliberately stdout, not stderr: Claude Code renders this command's
        # stdout as the status line and discards the rest, so a message on
        # stderr would leave the segment blank — the very silence this exists to
        # break. Compact by necessity too: it has to fit one status-line row, so
        # it points at `mien doctor` rather than carrying the parse error.
        click.echo("\033[31m⚠ mien:config unreadable — run 'mien doctor'\033[0m")
    except Exception:
        return


@main.command("prompt")
def prompt_cmd() -> None:
    """Emit the identity segment for a shell prompt (e.g. zsh `RPROMPT`).

    The same segment as `mien statusline`, but resolved from THIS shell's own
    directory and `MIEN_PROFILE` rather than Claude Code's session JSON — so the
    ambient "who am I here" shows in an ordinary terminal too, not only inside
    Claude Code. Wire it into a prompt:

        # zsh
        setopt PROMPT_SUBST; RPROMPT='$(mien prompt)'
        # bash
        PROMPT_COMMAND='PS1="… $(mien prompt) "'

    Secret-free and never errors — prints nothing when mien is unconfigured or
    on an unexpected failure, so it is safe to run on every prompt. An
    unreadable config shows a compact marker instead of staying blank, because
    a blank segment reads as "nothing to report" when in fact mien can no
    longer tell who you are here.
    """
    try:
        cfg = load_config()
        if cfg is None:
            return
        click.echo(_identity_segment(cfg, _logical_cwd()), nl=False)
    except ConfigError:
        # Deliberately stdout, not stderr: a prompt redraws constantly and its
        # stderr goes straight to the terminal, so a full message there would
        # spam every redraw. The marker rides along in the segment instead.
        click.echo("\033[31m⚠mien:config\033[0m", nl=False)
    except Exception:
        return


_GUARD_OFF = {"off", "0", "false", "no"}

_CAPTURE_OK = {"capture-ok", "off"}

# What `mien exec` puts in the environment for each service `mien token` prints,
# so the refusal can name the exact variable to reach for. Google is the odd one:
# `exec` supplies an ADC *credentials file*, not the bare access token this
# command mints, so the substitute is a path rather than a token string.
_EXEC_ENV_FOR = {
    "notion": "NOTION_TOKEN",
    "atlassian": "ATLASSIAN_API_TOKEN",
    "google": "GOOGLE_APPLICATION_CREDENTIALS",
}

# The shape each service's credential actually takes on the wire, so the
# refusal's one-off example is correct rather than merely plausible. Notion is
# `Authorization: Bearer`; Atlassian Cloud is HTTP Basic (email:token), never
# Bearer; and google has no bearer-token variable at all — its `exec` substitute
# is an ADC file path, so sending it as a Bearer token would always 401. Google
# therefore gets an honest remedy in place of an example.
_HTTP_HINT_FOR = {
    "notion": (
        "  For a one-off HTTP call, let the child shell expand it:\n"
        "    mien exec <profile> -- sh -c 'curl -H \"Authorization: Bearer "
        "$NOTION_TOKEN\" -H \"Notion-Version: 2022-06-28\" "
        "https://api.notion.com/v1/users/me'"
    ),
    "atlassian": (
        "  For a one-off HTTP call, let the child shell expand it — Atlassian "
        "Cloud uses HTTP Basic, not Bearer:\n"
        "    mien exec <profile> -- sh -c 'curl -u "
        "\"$ATLASSIAN_EMAIL:$ATLASSIAN_API_TOKEN\" "
        "\"$ATLASSIAN_BASE_URL/rest/api/3/issue/PROJ-123\"'"
    ),
    "google": (
        "  There is no bare-token variable to send in a header: "
        "$GOOGLE_APPLICATION_CREDENTIALS is a file path, not a token. Let a "
        "Google client library read it directly under `mien exec`; if you truly "
        "need the string, use --force here, or:\n"
        "    mien exec <profile> -- sh -c 'curl -H \"Authorization: Bearer "
        "$(gcloud auth application-default print-access-token)\" ...'"
    ),
}


def capture_context() -> str | None:
    """The harness marker suggesting this command's stdout is being recorded.

    Presence-gated: any one marker set means "an agent is driving". The names come
    from `mien.shell.CAPTURE_MARKER_VARS` rather than a tuple of their own, and
    that is the point — the same map is what `check_custom_var_name` refuses as a
    `custom` credential name, so a marker cannot be detected here while the scrub
    is still free to `unset` it.
    """
    for marker in CAPTURE_MARKER_VARS:
        if os.environ.get(marker, "").strip():
            return marker
    return None


@main.command("guard")
@click.option("--force", "-f", is_flag=True, help="Skip the check and exit 0.")
def guard_cmd(force: bool) -> None:
    """Refuse to proceed when the identity is confidently wrong for this repo.

    The acting counterpart of `mien statusline`: where the status line *shows* a
    wrong identity, `guard` *blocks* on it. It exits non-zero — refusing the
    action — only on a confident mismatch: the active `MIEN_PROFILE`, or the git
    author a commit here would carry, positively belongs to a different profile
    than the repository's `origin` owner. It exits 0 (allows) on every
    uncertainty — no config, an unknown owner, an unrecognized author — so it
    never blocks on a guess, and on any internal error, so a mien bug can't wedge
    your commits.

    Wire it as a pre-commit hook to stop a mis-authored commit before it lands:

        echo 'exec mien guard' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit

    or chain it before a push (`mien guard && git push`). Override once with
    `MIEN_GUARD=off` (or `git commit --no-verify` for the hook), or `--force`.

    Using the repository's own signals to *block* is safe: a crafted `origin`
    can at worst cause a false refusal (an annoyance you can override), never a
    mis-action — unlike selecting an acting identity, which never trusts the repo.
    """
    if force or os.environ.get("MIEN_GUARD", "").strip().lower() in _GUARD_OFF:
        return
    try:
        cfg = load_config()
        if cfg is None:
            return
        cwd = _logical_cwd()
        env_profile = os.environ.get("MIEN_PROFILE") or None
        env_known = bool(env_profile and env_profile in cfg.profiles)
        try:
            claimed, source = claimed_profile(
                cfg.profiles, cwd, remote=git_origin_remote(cwd)
            )
        except AmbiguousScope:
            claimed, source = None, None
        author_email = git_author_email(cwd)
        author = profile_for_email(cfg.profiles, author_email) if author_email else None
        reason = guard_reason(
            env_profile, claimed, source=source or "dir",
            author_profile=author, env_known=env_known,
        )
    except ConfigError as exc:
        # Still fail open — a broken config must not wedge your commits — but a
        # guard that has silently stopped guarding is worse than one that says so.
        click.echo(
            f"mien: guard is NOT enforcing — config unreadable: {exc}", err=True)
        return
    except Exception:
        return  # fail open: never wedge an action because guard itself broke.
    if reason:
        click.echo(f"mien: refusing — {reason}.", err=True)
        click.echo(
            "  Fix: activate the right profile (`mien-use <profile>`) or correct "
            "git user.email.\n"
            "  Override once: MIEN_GUARD=off <command> "
            "(or `git commit --no-verify` for a hook).",
            err=True,
        )
        raise SystemExit(1)


@main.command("run", context_settings={"ignore_unknown_options": True})
@click.argument("argv", nargs=-1, required=True)
def run_cmd(argv: tuple[str, ...]) -> None:
    """Run a command as the profile claimed by the current directory."""
    cfg = _require_config()
    name = _resolve_cwd_profile(cfg)
    if not name:
        raise click.ClickException(
            f"no profile claims {_logical_cwd()}. Add a default_for scope to a "
            "profile, or use `mien exec <profile> -- ...`."
        )
    # _resolve_cwd_profile only ever returns a profile that exists.
    _run_as_profile(cfg, cfg.profiles[name], argv)


@main.command("token")
@click.argument("service", type=click.Choice(["google", "atlassian", "notion"]))
@click.option(
    "--profile",
    "profile",
    default=None,
    help="Profile to mint for. Defaults to $MIEN_PROFILE. Prefer passing this "
    "explicitly from an agent, whose shell state does not survive between calls.",
)
@click.option("--force", "-f", is_flag=True,
              help="Print the secret even where stdout looks recorded.")
def token_cmd(service: str, profile: str | None, force: bool) -> None:
    """Print a credential for `service` on stdout — a raw secret.

    Prefer `mien exec <profile> -- <cmd...>`, which hands the credential to the
    command in its environment instead of writing it anywhere readable. This
    command exists for the case that genuinely needs the bare string, and it
    refuses by default where stdout looks recorded (see `--force`).
    """
    cfg = _require_config()
    name = profile or os.environ.get("MIEN_PROFILE")
    if not name:
        raise click.ClickException(
            "no profile: pass --profile <name>, or set $MIEN_PROFILE via "
            'eval "$(mien use --owner-pid $$ <profile>)" in this same shell'
        )
    prof = cfg.profiles.get(name)
    if not prof:
        raise click.ClickException(f"profile {name!r} not found")
    identity = getattr(prof, service)
    if not identity:
        raise click.ClickException(f"profile {name!r} has no {service} identity")

    # The capture check sits here on purpose: *after* the identity is resolved,
    # *before* the backend is touched. Refusing first would replace a real
    # misconfiguration ("profile has no notion identity") with advice to read
    # $NOTION_TOKEN — a variable `mien exec` never sets for such a profile —
    # and `exec` overlays the environment without scrubbing, so following that
    # advice could pick up another identity's ambient token and act as the
    # wrong person. Failing loud on the identity first keeps that impossible;
    # refusing before `load_backend` keeps a blocked call from spending one.
    marker = None if force else capture_context()
    if marker and os.environ.get("MIEN_TOKEN", "").strip().lower() not in _CAPTURE_OK:
        var = _EXEC_ENV_FOR[service]
        substitute = (
            f"    mien exec {name} -- <your command>   # arrives as ${var}"
            if service != "google" else
            f"    mien exec {name} -- <your command>   # arrives as ${var}\n"
            "    (an ADC credentials file — Google client libraries read it "
            "directly; there is no env form of a bare access token)"
        )
        raise click.ClickException(
            f"refusing to print a raw secret: ${marker} is set, so this looks like "
            "an agent session where anything on stdout can be captured into a "
            "transcript that outlives the command.\n"
            "  Give the credential to the program instead of printing it:\n"
            f"{substitute}\n"
            f"{_HTTP_HINT_FOR[service]}\n"
            "  Override once: MIEN_TOKEN=capture-ok mien token ... (or --force)."
        )

    backend = load_backend(cfg.secrets_backend)
    if service == "google":
        client_secret = backend.get(identity.oauth_client_secret_ref).decode("utf-8")
        refresh = backend.get(identity.refresh_token_ref).decode("utf-8")
        access = exchange_refresh_token(
            client_id=identity.oauth_client_id,
            client_secret=client_secret,
            refresh_token=refresh,
        )
        click.echo(access)
    else:
        click.echo(backend.get(identity.api_token_ref).decode("utf-8").strip())


@main.command("logout")
@click.argument("profile_name")
@click.option("--service", type=click.Choice(["google", "github", "slack", "aws", "oci", "atlassian", "notion", "custom"]), required=True)
@click.option("--name", "custom_name",
              help="(custom) environment variable to forget (e.g. ANTHROPIC_API_KEY). "
                   "Required for --service custom.")
@click.option("--workspace", help="Slack workspace label (required for --service slack)")
def logout_cmd(profile_name: str, service: str, custom_name: str | None,
               workspace: str | None) -> None:
    var_name = _custom_var_name(service, custom_name)
    cfg = _require_config()
    prof = cfg.profiles.get(profile_name)
    if not prof:
        raise click.ClickException(f"profile {profile_name!r} not found")
    backend = load_backend(cfg.secrets_backend)
    if service == "github" and prof.github:
        if prof.github.token_ref:
            backend.delete(prof.github.token_ref)
        if prof.github.ssh_key_ref:
            backend.delete(prof.github.ssh_key_ref)
        prof.github = None
    elif service == "google" and prof.google:
        if prof.google.refresh_token_ref:
            backend.delete(prof.google.refresh_token_ref)
        if prof.google.oauth_client_secret_ref:
            backend.delete(prof.google.oauth_client_secret_ref)
        if prof.google.adc_ref:
            backend.delete(prof.google.adc_ref)
        prof.google = None
    elif service == "slack":
        if not workspace:
            raise click.ClickException("--workspace required for --service slack")
        kept = []
        for w in prof.slack:
            if w.workspace == workspace:
                backend.delete(w.user_token_ref)
            else:
                kept.append(w)
        prof.slack = kept
    elif service == "aws" and prof.aws:
        if prof.aws.access_key_id_ref:
            backend.delete(prof.aws.access_key_id_ref)
        if prof.aws.secret_access_key_ref:
            backend.delete(prof.aws.secret_access_key_ref)
        prof.aws = None
    elif service == "oci":
        prof.oci = None
    elif service == "atlassian" and prof.atlassian:
        backend.delete(prof.atlassian.api_token_ref)
        prof.atlassian = None
    elif service == "notion" and prof.notion:
        backend.delete(prof.notion.api_token_ref)
        prof.notion = None
    elif service == "custom":
        # `var_name` is non-None for this service — `_custom_var_name` refused the
        # call otherwise. A name this profile does not have is a typo, and
        # "removed" would be a lie about a credential — so this fails rather than
        # reporting a no-op the way an absent `oci` block does.
        if var_name not in prof.custom:
            raise click.ClickException(
                f"profile {profile_name!r} has no custom variable {var_name!r}"
                + (f" (it has: {', '.join(prof.custom)})" if prof.custom else "")
            )
        backend.delete(prof.custom.pop(var_name))
        _save_and_sync(cfg, backend)
        click.echo(f"removed custom variable {var_name} from {profile_name}")
        return
    _save_and_sync(cfg, backend)
    click.echo(f"removed {service} from {profile_name}")


def _check_remote_credentials(cwd: str) -> None:
    """Report any remote of the repository at ``cwd`` that embeds a token.

    A credential in a remote URL is the one identity mien cannot route around:
    git authenticates as that token's owner whatever profile is active, and the
    secret is written in cleartext into `.git/config`, so `git remote -v`, a
    push error, or an agent listing repositories copies it into a log or a
    transcript. Stripping the userinfo is the whole fix — a credential helper
    then supplies the secret per call instead of storing it in the tree.

    Prints the remote *names* and never a URL, since the URL is the secret.

    ponytail: checks the repository you are standing in, not the machine. A
    walker belongs with `mien discover`, which already traverses repositories.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "remote"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return  # not a repository, or no git — nothing to report.
        names = [n for n in result.stdout.split() if n]
        bad = [
            n for n in names
            if remote_embeds_credential(
                subprocess.run(
                    ["git", "-C", cwd, "remote", "get-url", n],
                    capture_output=True, text=True, timeout=2,
                ).stdout.strip()
            )
        ]
    except (OSError, subprocess.SubprocessError):
        return
    if not bad:
        return
    click.echo(
        f"remotes:   ⚠ {len(bad)} remote(s) here embed a credential in the URL: "
        f"{', '.join(bad)}\n"
        "             git acts as that token's owner whatever profile is active, "
        "and every command that prints a remote leaks it.\n"
        "             Strip it, then let a credential helper supply the secret:\n"
        + "".join(
            f"               git remote set-url {n} "
            f"$(git remote get-url {n} | sed -E 's#//[^@/]+@#//#')\n"
            for n in bad
        )
        + "               git config --global credential.helper "
        + ("osxkeychain" if sys.platform == "darwin" else "store --file ~/.git-credentials")
        + "\n             The embedded token stays valid until you revoke it — "
        "assume it is in a log and rotate it."
    )


@main.command("doctor")
@click.option("--gc", is_flag=True, help="Sweep stale ephemeral files for dead PIDs")
def doctor_cmd(gc: bool) -> None:
    cfg = _require_config()
    click.echo(f"config:    {config_path()}")
    click.echo(f"backend:   {cfg.secrets_backend.type}")
    for k, v in cfg.secrets_backend.options.items():
        click.echo(f"             {k}={v}")
    for k, v in (cfg.bootstrap or {}).items():
        click.echo(f"bootstrap: {k}={v}")
    names = ", ".join(cfg.profiles) or "(none)"
    click.echo(f"profiles:  {len(cfg.profiles)} [{names}]")

    backend = load_backend(cfg.secrets_backend)
    try:
        backend.health_check()
    except Exception as e:
        raise click.ClickException(f"backend health check failed: {e}")
    click.echo("backend health: OK")

    if cfg.secrets_backend.type == "gcp_secret_manager":
        _check_adc_quota_project(cfg.secrets_backend.options.get("project"))

    _check_remote_credentials(_logical_cwd())

    if gc:
        EphemeralStore.gc()
        click.echo("ephemeral GC: done")


@main.command("preflight")
@click.option("--backend", type=click.Choice(["gcp_secret_manager", "macos_keychain"]),
              default="gcp_secret_manager", help="Backend to check prerequisites for.")
@click.option("--project", help="(gcp) project to verify access on")
@click.option("--account", help="(gcp) account email to verify")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON for agent orchestration.")
def preflight_cmd(backend: str, project: str | None, account: str | None, as_json: bool) -> None:
    """Check environment readiness before `mien init`. Useful for agent-driven setup."""
    findings: list[dict] = []

    def add(name: str, ok: bool, detail: str = "", fix: str = "") -> None:
        findings.append({"check": name, "ok": ok, "detail": detail, "fix": fix})

    if backend == "gcp_secret_manager":
        try:
            r = subprocess.run(["gcloud", "--version"], capture_output=True, text=True, check=True)
            first = (r.stdout.splitlines() or [""])[0]
            add("gcloud installed", True, first)
        except (FileNotFoundError, subprocess.CalledProcessError):
            add("gcloud installed", False, "", "Install Google Cloud SDK: https://cloud.google.com/sdk/docs/install")

        if project:
            cmd = ["gcloud", "projects", "describe", project]
            if account:
                cmd.append(f"--account={account}")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                add(f"project {project!r} accessible", True)
            else:
                add(f"project {project!r} accessible", False, r.stderr.strip().splitlines()[-1] if r.stderr else "",
                    f"gcloud projects list --account={account or '<email>'}  # find the right project ID")

            r = subprocess.run(
                ["gcloud", "services", "list", "--enabled", f"--project={project}",
                 "--filter=config.name:secretmanager.googleapis.com", "--format=value(config.name)"]
                + ([f"--account={account}"] if account else []),
                capture_output=True, text=True,
            )
            enabled = "secretmanager.googleapis.com" in r.stdout
            if enabled:
                add("Secret Manager API enabled", True)
            else:
                add("Secret Manager API enabled", False, "",
                    f"gcloud services enable secretmanager.googleapis.com --project={project}"
                    + (f" --account={account}" if account else ""))

        adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if adc_path.exists():
            try:
                adc = json.loads(adc_path.read_text())
                qp = adc.get("quota_project_id")
                add("ADC present", True, f"quota_project_id={qp or '(unset)'}")
            except (OSError, json.JSONDecodeError) as e:
                add("ADC present", False, str(e),
                    f"gcloud auth application-default login --account={account or '<email>'}")
        else:
            add("ADC present", False, "no application_default_credentials.json",
                f"gcloud auth application-default login --account={account or '<email>'}")

    elif backend == "macos_keychain":
        # The backend talks to the Keychain in-process, not via the security CLI,
        # so check the real path: can it reach the Keychain?
        try:
            load_backend(BackendConfig(type="macos_keychain", options={})).health_check()
            add("macOS Keychain", True)
        except Exception as e:
            add("macOS Keychain", False, str(e),
                "macOS only — this backend isn't supported on this OS")

    if as_json:
        click.echo(json.dumps({"backend": backend, "checks": findings}, indent=2))
        if any(not f["ok"] for f in findings):
            sys.exit(1)
        return

    for f in findings:
        mark = "✓" if f["ok"] else "✗"
        line = f"  {mark} {f['check']}"
        if f["detail"]:
            line += f" — {f['detail']}"
        click.echo(line)
        if not f["ok"] and f["fix"]:
            click.echo(f"      fix: {f['fix']}")
    if any(not f["ok"] for f in findings):
        sys.exit(1)
