---
name: mien
description: Use when the user wants to act as a specific identity/profile across Google (Gmail/Calendar/Drive/GCP), GitHub, Slack, Atlassian, Notion, AWS, or OCI — e.g., "as my work account", "switch to <name>", "post in <workspace>", "send mail from <email>". Also covers a credential of the user's own kept per identity — an LLM API key, an npm/PyPI token, a database URL — delivered as an environment variable ("my work Anthropic key", "the npm token for this profile"). Activates per-shell credentials for `gh`, `gcloud`, `bq`, `aws`, `oci`, and `curl` calls without polluting other agent sessions.
version: 0.6.0
author: arinyaho
license: MIT
compatibility: works best with `mien` on PATH; falls back to source if available
metadata:
  hermes:
    tags: [identity, credentials, multi-account, gcp, github, slack, atlassian, notion, aws, oci, api-keys]
    related_skills: []
---

# mien — Multi-identity credential router

The user maintains multiple identities, each bundling a Google account (Gmail/Calendar/Drive + GCP) and optionally a GitHub account, one or more Slack workspaces, an Atlassian account (Jira/Confluence), a Notion integration token, AWS credentials, an OCI profile, and/or any credential of their own stored per identity (`custom`: one environment variable name, one secret — an LLM API key, an npm token, a database URL). Use `mien` to activate the right identity in this shell session.

## Rule zero — if mien holds the credential, do not use a connector

**A service the active profile has credentials for must be reached with `mien exec <profile> -- ` and a direct API call. Never through the harness's own connector/MCP integration for that service.**

This is the mistake that costs the most time, and it is silent. `mien` changes one plane only: the environment variables of the shell it spawns (`gh`, `aws`, `gcloud`, `curl`). A harness connector — Atlassian/Jira/Confluence, Slack, Notion, Gmail, Drive — authenticates once, out of band, to a single account, and does not observe `mien` at all. Switching profiles has no effect on it.

So a session that has "switched to `work-b`" still reads Jira as whatever single account the connector holds — usually `work-a`, or a personal account. Nothing errors. The tool returns real data for the wrong identity, which is precisely the failure this project exists to prevent.

- Profile has the service (`mien whoami <profile>` lists it) → `mien exec <profile> -- ` + REST API. Always.
- Profile does not have it → a connector is fine, and is the only option. Say out loud which account it is acting as, since `mien` is not the one choosing.
- Never mix the two for one service inside a task. Data gathered through a connector cannot be attributed to the profile you named.

## When to use

Trigger any time:
- The user names a profile (`personal`, `work-foo`, etc.) and asks for an action that needs auth.
- The user asks "as <email>" / "from <email>" / "with my <something> account".
- A multi-account task (one profile per agent session) — `mien` is what isolates them.
- The user wants to call Atlassian APIs (Jira, Confluence) under a specific identity.
- The user wants to call the Notion API under a specific identity.
- The user wants to run AWS CLI / SDK calls under a specific identity.
- The user wants to run OCI CLI calls under a specific identity.
- The user needs a credential that is not one of the built-in services — an LLM API key, an npm/PyPI token, a database URL — under a specific identity. That is `--service custom` (below): check `mien whoami <profile>` or `mien list` for a `custom` line naming it.

If the user has not configured `mien`, **don't just punt** — drive the conversational setup flow described in `references/setup-flow.md`. Detect state with `mien doctor` (or `mien list` if doctor fails), then guide the user step by step.

## Invoking mien

Before running any `mien` command, resolve the binary:

```bash
# Prefer installed binary; fall back to source repo
if command -v mien &>/dev/null; then
  MIEN="mien"
elif [ -f "$HOME/Projects/mien/src/mien/__main__.py" ]; then
  MIEN="uv run --project $HOME/Projects/mien mien"
else
  echo "mien not found — install it (no checkout needed): uv tool install git+https://github.com/arinyaho/mien"
  exit 1
fi
```

Use `$MIEN` instead of `mien` in all subsequent commands.

## Core commands

```
$MIEN discover                      # inventory local AWS/OCI/gcloud/GitHub identities and what to import
$MIEN list                          # see profiles
$MIEN status                        # what is active in *this* shell
$MIEN whoami [<profile>]            # the whole bundled identity as a card; --json for machine form; --live to verify
$MIEN use <profile>                 # prints a `source …; rm …` loader; eval to activate (same call only)
$MIEN exec <profile> -- <cmd...>    # run cmd with the profile's env — prefer this; refuses a profile this place disowns
$MIEN which                         # profile claimed by the current directory
$MIEN claim <profile>               # bind THIS workspace to a profile via a local .mien (writes, approves, git-ignores)
$MIEN allow                         # approve an existing .mien so it can drive identity here
$MIEN run -- <cmd...>               # run cmd as that profile
$MIEN token google --profile <p>    # LAST RESORT: prints a raw secret on stdout; refuses under an agent harness
$MIEN statusline                    # one-line identity segment for a Claude Code status line
$MIEN prompt                        # same segment for a shell prompt (zsh RPROMPT / bash PS1)
$MIEN guard                         # exit non-zero if the identity is confidently wrong for this repo
```

**Never pipe a secret into a program — hand it over in the environment.** To give a command a credential, use `mien exec <profile> -- <cmd...>`: the secret arrives as an env var (`NOTION_TOKEN`, `ATLASSIAN_API_TOKEN`, `GH_TOKEN`, `GOOGLE_APPLICATION_CREDENTIALS`, …) and never reaches stdout, so it cannot land in the session transcript — some of those variables carry a path to a 0600 ephemeral file rather than the value itself. Do **not** do `mien token <svc> | <program>` — `token` prints a raw secret on stdout, and any program that echoes its input (a traceback, a usage error, a debug log) leaks it into the session transcript. `mien token` therefore refuses by default when it detects an agent harness — the built-in markers (`$CLAUDECODE`, `$CLAUDE_CODE_ENTRYPOINT`) are Claude Code's, and `MIEN_CAPTURED=1` extends the same refusal to any other harness — naming `mien exec` in the error; override with `MIEN_TOKEN=capture-ok` or `--force` only when a bare string is genuinely required. For a one-off HTTP call let the child shell expand it: `mien exec <p> -- sh -c 'curl -H "Authorization: Bearer $NOTION_TOKEN" …'` — the value never transits the agent's own shell. It does still land in the child's argv (visible to `ps` on this machine), exactly as the old `TOKEN=$(…)` form did: this buys transcript safety, not argv safety. One caution: `exec` **overlays** the environment without scrubbing, so if the named profile has no such service mien sets nothing and an ambient value from another identity survives — the call then succeeds as the wrong person. `mien token` fails loud in that case (`profile 'p' has no notion identity`); when in doubt confirm with `mien whoami <profile>` before trusting an `exec` recipe.

**Commit author is git's own job.** mien does not set `user.email` — git already does this natively with `includeIf` (`gitdir:` by directory, `hasconfig:remote.*.url:` by repo owner) pointing at a per-identity gitconfig. The scp `git@host:owner/` remote form needs its own `**host:owner/**` rule alongside the `**/*host/owner/**` one that covers https/ssh. To sharpen mien's own warning, add `git_email` to the profile in `~/.config/mien/config.json` (a hand-edited field, same as `owns_remotes`/`default_for`) — mien reads it only for the author cross-check, so `guard` and the status line warn when a commit's `user.email` disagrees with the identity acting here. Prevention is git's includeIf; `guard` is the backstop.

**Project-local declaration (`.mien`).** The simplest way to bind a workspace to a profile is a `.mien` file naming it — `mien claim <profile>` writes it, approves it, and adds it to the global git ignore in one step. `mien run`/`which` and the status line then act as that profile for the whole tree, with no central scope. Security: a `.mien` is a checked-out file, so it does **not** drive the acting identity until the user approves it (`mien allow`) — a cloned repo's `.mien` is inert, and an edited one must be re-approved. If a directory declares an unapproved `.mien`, `mien which`/`run` fail loud (they don't silently route); tell the user to run `mien allow`. Precedence: `MIEN_PROFILE` → approved `.mien` → central `default_for`/`owns_remotes`.

**Ambient identity across harnesses.** Three surfaces show/enforce "who am I here", with different reach:
- `mien statusline` — the **Claude Code** status line (wired via `.claude/settings.json` `statusLine`). Claude Code-specific; other harnesses (e.g. Codex) expose no equivalent status-line hook.
- `mien prompt` — a **shell prompt** segment (`RPROMPT='$(mien prompt)'`), so the same indicator shows in any ordinary terminal, independent of harness.
- `mien guard` — the **enforcement**, and it is harness-agnostic: as a git pre-commit hook it blocks a mis-authored commit in *any* session (Claude Code, Codex, a plain shell), and it is the strongest of the three guarantees. Prefer it when the goal is prevention rather than display.

**Refusal gate.** `mien guard` is the acting counterpart of the status line: it exits non-zero (refusing the action) only on a confident mismatch — the active `MIEN_PROFILE`, or the git author a commit would carry, positively belongs to a different profile than the repo's `origin` owner. If the user wants mis-authored commits blocked, wire it as a pre-commit hook: `echo 'exec mien guard' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit` (or a global `core.hooksPath`). It fails open (allows) on any uncertainty or error, and every refusal is overridable (`MIEN_GUARD=off`, `--force`, `git commit --no-verify`). Blocking with repo signals is safe (a crafted remote at worst causes a false refusal you override); the acting path still never trusts the repo.

**Status line.** If the user wants the active identity always visible (e.g. "show which profile I'm on"), wire `mien statusline` into their `.claude/settings.json`: `"statusLine": { "type": "command", "command": "mien statusline" }`. It reads Claude Code's session JSON on stdin and compares `MIEN_PROFILE` against whose place this is — the repository's `origin` owner (`owns_remotes`) or a directory `default_for` scope — printing green when they agree and red (`✗ repo is <other>'s` / `✗ dir wants <other>`) when the active identity is wrong here. It also cross-checks the repo's git `user.email` against the emails profiles declare and warns (`author:<other> ✗ …`) when a commit here would be authored as a *different* profile than the owner — catching a mis-commit even with nothing activated, while staying quiet on an unrecognized email. The remote owner and the author check are advisory-only (display/warning, never used to pick an acting identity, since a checked-out repo controls both its remote and its `user.email`). Secret-free and silent when `mien` is unconfigured.

## Activation pattern

**Your shell state does not survive between tool calls.** Claude Code, Codex, and most
agent harnesses start a fresh shell for every command invocation, so environment
variables set by `eval "$($MIEN use ...)"` are gone by your next call.

Nothing errors when this happens. The next command simply runs as whatever identity the
ambient shell already had. For a credential router that is the worst possible failure
mode: you believe you are acting as `work-foo`, and you are acting as something else.

Three patterns are safe. Use one of them; never rely on an `eval` from an earlier call.

**1. `$MIEN run` — preferred when the project pins its own identity.**

If the user has given profiles `default_for` scopes, the directory already knows who to
be, and you never have to name a profile:

```bash
$MIEN which                       # confirm, if the identity matters
$MIEN run -- gh pr list
$MIEN run -- gcloud projects list
```

**`run` is not purely directory-driven.** If `MIEN_PROFILE` is already set, it wins over the
directory — and it very often is: a session launched from a terminal where the user ran
`mien-use work` inherits it into every one of your commands, so `run` acts as `work` even in
a directory pinned to something else. `$MIEN which` reports the conflict, but only as a
warning on stderr, and it still exits 0. So when the identity matters, do not just check the
exit status — read what `which` printed, or name the profile with `exec` and leave nothing
to inherit.

**2. `$MIEN exec` — when you must name the profile.**

```bash
$MIEN exec work-foo -- gh pr list
$MIEN exec work-foo -- aws sts get-caller-identity
```

Both carry the identity per invocation, so there is no earlier `eval` left to expire. Both
layer the profile *over* the ambient env rather than replacing it, though: for a service the
profile does not define — a profile with no `aws` block still inherits an ambient
`AWS_ACCESS_KEY_ID` — the ambient credential still wins, so confirm with the service's own
identity check below.

**3. Single-call `eval` — for a sequence that must share one shell.**

Every line below must be in **one** command invocation:

```bash
eval "$($MIEN use work-foo)"
gh pr list                        # uses GH_TOKEN
gcloud projects list              # uses CLOUDSDK_ACTIVE_CONFIG_NAME
bq ls -p                          # ditto
```

Splitting those lines across two tool calls is the bug described above.

**Confirm the identity when the action is destructive or the profile matters.** `$MIEN
whoami --live <profile>` asks GitHub, AWS, and Google who the profile actually
authenticates as, compares it to the config, and exits non-zero on any mismatch or dead
credential — so it gates the action when chained before it:

```bash
$MIEN whoami --live work-foo && $MIEN exec work-foo -- gh pr merge 123
```

Its exit code is the gate; a wrong live identity or a revoked token stops the `&&`. Caveats worth knowing: a provider that could not be reached is reported but does not fail the check (could-not-check is not the same as wrong); AWS is reported, not verified, since a profile name is not an ARN there is nothing to compare; and **GitHub, AWS and Google are the only services with a live probe at all** — everything else the profile configures is listed by name under `not checked (no live probe yet)` rather than pretended verified, so read that line instead of assuming a clean report covered the service you care about (`custom` appears there, since mien is told a variable name and never what the credential is for, and so does a gcloud-login-only `google`, which the probe structurally cannot verify — though only when some other provider was probed, since otherwise no report prints at all). **`--live` needs a *probeable* provider to run at all** — github, aws, or a google with a stored refresh token (one logged in via `mien login --service google`, not a gcloud-only login). With none it exits non-zero saying it could not check, and prints no report at all, so a `custom`-only, notion-only or gcloud-login-only-google profile fails the `&&` without having checked anything. That fails closed, so nothing runs as the wrong identity — but read it as "could not check", not "wrong identity", and gate those profiles with the inline comparison below or the service's own check.

For a single service you can also inline the comparison, since a bare `gh api user` succeeds under *any* valid token and exit status alone gates nothing:

```bash
[ "$($MIEN run -- gh api user -q .login)" = "expected-login" ] \
  && $MIEN run -- gh pr merge 123
```

### Read the profile's environment before you write a single URL

Site URLs, account emails and workspace names are **in the profile**. Guessing them wastes time and, worse, sometimes succeeds: an Atlassian site that exists but grants this account nothing authenticates cleanly and returns an empty result set, which reads exactly like "no data" rather than "wrong site".

```bash
$MIEN whoami <profile>
```

That card is built from the profile's configuration, so it is the authority on which services this profile actually carries — and it already prints the values you would otherwise guess: the Atlassian site URL and account email, the GitHub username, the Google address, the Slack workspaces, the AWS profile and region, the names of any custom variables.

Do not use an `env` dump for that question. `exec` merges the profile's variables *over* the ambient environment rather than replacing it, so an inherited `ATLASSIAN_BASE_URL` or `GH_TOKEN` from another identity prints exactly like one the profile set. Reading a single variable under `exec` is fine once `whoami` has told you the profile carries that service:

```bash
$MIEN exec <profile> -- printenv ATLASSIAN_BASE_URL
```

(Only for the non-secret ones — `ATLASSIAN_BASE_URL`, `ATLASSIAN_EMAIL`, `AWS_PROFILE`, `AWS_DEFAULT_REGION`. Never print a token-valued variable; see *Important rules*.)

What each service contributes, when the profile configures it:

| Service in the profile | Variables `exec` sets | Notes |
|---|---|---|
| `atlassian` | `ATLASSIAN_BASE_URL`, `ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN` | base URL is the site — `https://<site>.atlassian.net`; never guess it |
| `github` | `GH_TOKEN` (+ `GIT_SSH_COMMAND` when a key is stored) | |
| `google` | `GOOGLE_APPLICATION_CREDENTIALS`, `CLOUDSDK_ACTIVE_CONFIG_NAME` | a **file path**, not a token |
| `notion` | `NOTION_TOKEN` | |
| `slack` | `MIEN_SLACK_TOKENS` (path to a 0600 JSON map) + `MIEN_SLACK_DEFAULT_TOKEN` when there is exactly one workspace | |
| `aws` | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`, or `AWS_PROFILE`; `AWS_DEFAULT_REGION` | |
| `oci` | `OCI_CLI_PROFILE`, `OCI_CLI_CONFIG_FILE` | |
| `custom` | whatever names the user chose | `whoami` lists the names |

Profiles differ, and the difference is the point — one may carry Atlassian + GitHub + Google, another add Slack and AWS, a personal one carry only Notion + GitHub + Google. Do not carry an assumption from one profile to the next; run `whoami` per profile. A service the profile does *not* carry is the dangerous case, and it is invisible from inside `exec`: the overlay never scrubs, so an ambient value from another identity survives, the variable looks set, and the call succeeds as the wrong person. Only the identity card can tell you the profile has nothing there.

For Gmail/Calendar/Drive (no helper in v1). Google is the one service with no bare-token
variable: `exec` exports `GOOGLE_APPLICATION_CREDENTIALS`, an ADC *file path*, which a
Google client library reads directly — prefer that. For a raw HTTP call, let the child
shell mint the token from that same ADC file so it never reaches your shell or the
transcript:

```bash
$MIEN exec work-foo -- sh -c 'curl -s -H "Authorization: Bearer $(gcloud auth application-default print-access-token)" \
  "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=from:foo"'
```

With no `gcloud` available, `$MIEN token google --profile work-foo` still mints a bare
access token — but that is exactly the case the harness refusal covers, so it needs
`--force` (or `MIEN_TOKEN=capture-ok`) and the secret lands on stdout.

For Slack (multi-workspace per profile):

```bash
# $MIEN_SLACK_TOKENS is a path to a 0600 file; resolve the token in the child
# shell so it never lands in the agent's own shell or the transcript
$MIEN exec work-foo -- sh -c 'TOKEN=$(jq -r ".\"team-a\"" "$MIEN_SLACK_TOKENS");
  curl -s -H "Authorization: Bearer $TOKEN" \
    https://slack.com/api/conversations.list'
```

If the profile has only one workspace, `$MIEN_SLACK_DEFAULT_TOKEN` is also exported.

For Atlassian (Jira/Confluence):

```bash
# Basic auth, and the credential never reaches argv: a shell builtin writes a
# curl config to a pipe, curl reads it from stdin with -K -.
$MIEN exec work-foo -- sh -c 'printf "user = \"%s:%s\"\n" "$ATLASSIAN_EMAIL" "$ATLASSIAN_API_TOKEN" \
  | curl -sK - -H "Accept: application/json" \
      --url "$ATLASSIAN_BASE_URL/rest/api/3/myself"'
```

Atlassian Cloud is HTTP **Basic** (email:token), never Bearer. `ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN`, and `ATLASSIAN_BASE_URL` all arrive from `mien exec` (and `mien use`). `printf` is a shell builtin, so no separate process carries the value in its argv; the `-u "$USER:$TOKEN"` form works too but leaves the token visible to `ps` on this machine for the life of the call. Use the `-K -` form in anything you write down. The same trick covers a Bearer header (`printf 'header = "Authorization: Bearer %s"\n' "$TOKEN"`).

**Send it to the right host.** These are *site* credentials: the base URL is `https://<site>.atlassian.net`, taken from `$ATLASSIAN_BASE_URL`, not guessed. `api.atlassian.com` is the OAuth 3LO gateway and will reject an API token no matter which profile you use.

### What `mien token <service>` actually returns

Not all three are OAuth. Assuming they are is a 401 that reads like a broken login:

| Service | Shape | How it is sent | Lifetime |
|---|---|---|---|
| `atlassian` | Atlassian **API token** (`ATAT…`) | HTTP **Basic**, `email:token`, against `https://<site>.atlassian.net` | long-lived until revoked |
| `notion` | Notion **integration token** (`ntn_…`) | `Authorization: Bearer`, against `https://api.notion.com` | long-lived until revoked |
| `google` | **OAuth 2.0 access token**, minted on demand from the stored refresh token | `Authorization: Bearer`, against `*.googleapis.com` | ~1 hour |

Only `google` is OAuth. Sending an Atlassian API token as a Bearer to `api.atlassian.com/oauth/token/accessible-resources` returns 401 for *every* profile — the credential is fine, the call is wrong.

For Notion:

```bash
$MIEN exec work-foo -- sh -c 'curl -s -H "Authorization: Bearer $NOTION_TOKEN" \
  -H "Notion-Version: 2022-06-28" https://api.notion.com/v1/users/me'
```

`NOTION_TOKEN` arrives from `mien exec` (and is exported by `mien use`).

For a credential of the user's own (`custom`):

```bash
$MIEN whoami work-foo                # the `custom` row lists the variable NAMES this profile carries
$MIEN exec work-foo -- claude -p "…" # arrives as $ANTHROPIC_API_KEY, like any other variable
$MIEN exec work-foo -- npm publish   # arrives as $NPM_TOKEN
$MIEN exec work-foo -- sh -c 'curl -s -H "Authorization: Bearer $ANTHROPIC_API_KEY" …'
```

A `custom` credential is one environment variable name chosen by the user, carrying one secret from the backend — nothing else about it is special: it arrives through `exec`/`run`/`use` exactly like `NOTION_TOKEN` does, and the value must be expanded in the *child* shell, never in yours. There is deliberately **no `mien token custom`**; `exec` is the whole interface. `mien status` shows such a variable as `<set>`, and `mien list` / `mien whoami --json` show names only, so nothing you can read prints the value.

**Never run the `login` yourself** (see *Important rules*) — the secret would land in the transcript. Give the user the command to run in their own terminal, or a reference form:

```bash
mien login  <profile> --service custom --name ANTHROPIC_API_KEY                  # hidden prompt (user runs this)
mien login  <profile> --service custom --name NPM_TOKEN --secret-cmd 'op read op://Private/npm/token'
mien logout <profile> --service custom --name ANTHROPIC_API_KEY                   # deletes the secret
```

`--name` is the environment variable name. It is required with `--service custom` and refused with any other service. mien refuses a name **four** ways — at `login` time and again every time the config is parsed, so a hand-edited config fails exactly as the CLI would have, and each error says what the name already means:

- it is not a shell identifier (`[A-Za-z_][A-Za-z0-9_]*`, ASCII only);
- it is a variable mien already exports itself — either for a built-in service (`GH_TOKEN`, `AWS_PROFILE`, `NOTION_TOKEN`, …), where the error names the service it would fight so you can use that built-in's own `--service` instead, or for mien's own bookkeeping (`MIEN_PROFILE`, `MIEN_EPHEMERAL_DIR`), where the only remedy is another name;
- it is one the shell or mien itself reads as an instruction: `PATH`, `HOME`, `IFS`, `PS1`, `TMPDIR`, `MIEN_CONFIG`;
- it is one of the agent-harness capture markers mien reads to know an agent is driving: `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `MIEN_CAPTURED`.

The last two exist because `mien use` and `mien-unset` `unset` every managed name in *every* shell, and that list is the union over all profiles: such a name in one profile would break shells that have nothing to do with it, or — for a marker, whose absence mien reads as "no agent is watching" — silently disarm the `mien token` and `mien exec` refusals described above. Matching is exact and case-sensitive throughout (`MY_PATH` is an ordinary name); `references/schema.md` carries the full list and the reason for each. Only a backend *reference* is written to the config, so the secret is not in `config.json` and not in the pushed manifest.

For AWS:

```bash
$MIEN exec work-foo -- aws s3 ls                     # uses AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or AWS_PROFILE
$MIEN exec work-foo -- aws sts get-caller-identity   # verify active identity
```

`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE`, and `AWS_DEFAULT_REGION` are exported by `mien use`.

For OCI:

```bash
$MIEN exec work-foo -- oci iam user get --user-id <ocid>   # uses OCI_CLI_PROFILE / OCI_CLI_CONFIG_FILE
```

`OCI_CLI_PROFILE` and `OCI_CLI_CONFIG_FILE` are exported by `mien use`.

## Important rules

- **Never assume a profile is still active.** Environment set by an earlier tool call is gone (see *Activation pattern*). Every invocation must carry its own identity — via `mien exec`, an `eval` in that same invocation, or `--profile`. A command that "worked a moment ago" is not evidence the profile is still set.
- **Never paste resolved tokens into the conversation.** Let the *child* shell expand them — `$MIEN exec <p> -- sh -c 'curl -H "Authorization: Bearer $NOTION_TOKEN" …'` — so the value resolves inside the command that needs it and never appears in tool-call arguments, your own shell, or the transcript. `TOKEN=$($MIEN token …)` is not that: it resolves in *your* shell, which is why `token` refuses under an agent harness.
- **Never run `mien use` bare** — always `eval` it (or use the `mien-use` wrapper). As an extra safety net `mien use` writes exports to a 0600 ephemeral file and only prints a `source …; rm …` one-liner, so a missed `eval` no longer leaks tokens to stdout. On a real TTY `mien use` refuses outright (use `mien-use` or `eval`).
- **In a persistent human shell, pass an owner pid** — use the `mien-use` wrapper (it passes `$$`), or `eval "$(mien use --owner-pid $$ <profile>)"`. Without it the ephemeral files are keyed to mien's already-exited process, and a stray `mien doctor --gc` then deletes credentials the shell is still using. This does **not** apply to the single-call agent form above: that shell is short-lived and dies with the invocation, so keying the files to it or to mien amounts to the same thing, and they are meant to be reclaimed.
- **Never run `mien login` yourself to enter a secret.** The agent's shell is non-interactive, so you would have to put the secret in the command — which lands in the session transcript and shell history. Instead:
  - Tell the user to run the `mien login <profile> --service ...` command **themselves** in their own terminal (the hidden `getpass` prompt keeps it out of argv/history), **or**
  - Use a credential reference, not the value: `mien login <profile> --service <svc> --secret-cmd 'op read op://Vault/item/field'` (also works with `gcloud secrets versions access`, `security find-generic-password`, etc.). The `op://…` reference is safe to appear in history; the secret never does. This is the form to prefer for `--service custom --name <VAR>` too.
  - For Google, a pre-existing refresh token can be piped: `… --refresh-token-stdin < tokenfile` with the client secret via `--secret-cmd`.
- **Don't switch the active profile in this shell** if the user is asking for a one-off in another identity — use `mien exec <other> -- <cmd>` so the parent shell stays clean.
- **A refused `exec` is not a broken command.** Under an agent harness, `mien exec <p> -- …` refuses when this place visibly belongs to a *different* profile — an approved `.mien` declaration, else the repository's `origin` owner or a `default_for` scope. Nothing runs and no credential is loaded, and the error names the profile that does claim the place: re-run as that profile, or stop and ask the user if you believe the profile you named is the right one. Treat the refusal as the answer — do not go looking for a phrasing that gets past it. It is a mistake-catcher, not a security boundary, so it is not hard to evade; evading it is how you end up acting as the wrong person, which is the whole thing it exists to stop. (A person at a terminal never triggers this check.)
- **Don't use the agent's native Google/Slack/Atlassian/Notion connectors** for a service the profile has credentials for — they are single-account, bypass the user's vault, and do not react to a profile switch. See *Rule zero* at the top; this is the failure that looks like success.
- **Don't guess a site URL, workspace or account email.** It is in the profile's environment — see *Read the profile's environment before you write a single URL*. A wrong-but-real endpoint authenticates fine and returns nothing, which is indistinguishable from an honest empty result.
- **Don't assume a credential is OAuth.** Only `google` is; `atlassian` and `notion` are long-lived API tokens with their own auth scheme. See the table above before debugging a 401 as a broken login.

## References

- `references/setup-flow.md` — conversational setup: walk a fresh user from zero to a working profile
- `references/schema.md` — config file format
- `references/bootstrap.md` — first-time setup per backend (manual, for users who'd rather type the commands themselves)
- `references/usage.md` — recipes for common tasks
- `references/concurrency.md` — what each variable/file isolates vs. shares, and directory-pinned identity rules
- `references/troubleshooting.md` — common errors
