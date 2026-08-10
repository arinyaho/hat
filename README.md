# mien

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2)

Multi-identity credential router for Google, GitHub, and Slack — designed for developers juggling multiple accounts (personal + work) across services.

## What it does

Activate a named identity in your current shell:

```bash
eval "$(mien use --owner-pid $$ personal)"   # or: mien-use personal (the wrapper passes $$ for you)
gh pr list                 # uses your personal GitHub
gcloud projects list       # uses your personal GCP
TOKEN=$(mien token google) # mint a Gmail/Cal/Drive access token on demand
```

A second shell can run `mien-use work` (or `eval "$(mien use --owner-pid $$ work)"`) independently — activation touches only that shell. Tokens live in a secrets backend rather than in a dotfile.

## Architecture

- **Per-session env vars** activate `gcloud`, `gh`, etc.
- **Ephemeral files** (mode 0600, `${TMPDIR}/mien/`) hold per-session ADC, SSH key and Slack tokens. `mien exec` and `mien run` delete theirs when the child exits; the ones `mien use` leaves for your shell are swept on a best-effort basis — see [SECURITY.md](SECURITY.md#lifetime-and-cleanup).
- **Pluggable secrets backend**: GCP Secret Manager, macOS Keychain, or keyring (Linux Secret Service / Windows Credential Locker — free, no cloud, requires a desktop session).

[SECURITY.md](SECURITY.md) describes what is stored where, who can read it, and what `mien` deliberately does not protect — including that it does **not** hide credentials from an AI agent it hands them to.

## Install

### CLI

```bash
uv tool install git+https://github.com/arinyaho/mien    # or: pipx install git+https://github.com/arinyaho/mien
echo 'eval "$(mien shell-init)"' >> ~/.zshrc            # adds the mien-use / mien-unset wrappers
```

No checkout needed — both lines install from the repo directly. `mien shell-init` prints the shell wrappers; `eval`-ing it defines `mien-use` and `mien-unset` and wires the exit-trap cleanup. Use `--shell bash` (or add to `~/.bashrc`) for bash.

To hack on it, clone and install from the working tree instead:

```bash
git clone https://github.com/arinyaho/mien ~/projects/mien
cd ~/projects/mien && uv tool install --editable .
```

### As an agent skill

`mien` ships a SKILL.md that teaches AI agents (Claude Code, Codex, Hermes Agent) when and how to invoke the CLI on your behalf. The skill assumes the `mien` binary is already on `PATH` — install the CLI first (above), then add the skill:

> **One rule worth knowing yourself:** `mien` routes the environment-variable plane only. Your agent's built-in service connectors (Atlassian, Slack, Notion, Google) hold one fixed account each and ignore a profile switch entirely — so a session told to use `work` still reads Jira as whoever the connector authenticated as, silently. For any service a profile has credentials for, the agent should call the REST API under `mien exec <profile> -- `. The skill says so as its first rule; see [SECURITY.md](SECURITY.md#protection-goals-and-what-is-not-protected).

**Claude Code:**

```bash
/plugin marketplace add arinyaho/mien
/plugin install mien@arinyaho
```

**Codex:**

```bash
codex plugin marketplace add arinyaho/mien --ref main   # register the marketplace
codex plugin add mien@arinyaho                           # install the plugin
```

Update the Codex plugin: `codex plugin marketplace upgrade arinyaho` then re-run `codex plugin add mien@arinyaho`. Uninstall: `codex plugin remove mien@arinyaho` (and, optionally, `codex plugin marketplace remove arinyaho`).

**Hermes Agent:**

```bash
# Install directly from GitHub
hermes skills install arinyaho/mien/skills/mien

# Or add the repo as a tap source, then install
hermes skills tap add arinyaho/mien
hermes skills install mien

# Or manually
git clone https://github.com/arinyaho/mien ~/.hermes/skills/_src/mien
ln -s ~/.hermes/skills/_src/mien/skills/mien ~/.hermes/skills/mien
```

Once installed, the agent invokes `mien` automatically when you mention identity-scoped work — e.g. "as my work account", "switch to personal".

## Bootstrap

```bash
mien discover                           # what identities are already on this machine?
mien init                               # pick a backend
mien login personal --service github
mien login personal --service google --email me@x.com --client-id <id>
mien login personal --service slack --workspace team-a
mien login personal --service custom --name ANTHROPIC_API_KEY   # a credential of your own
```

`mien discover` is the onboarding shortcut: it inventories the AWS/OCI profiles, gcloud configurations, and GitHub accounts already configured locally, and shows which are bound to a mien profile and which are not — with the `mien login` command to import each:

```
GitHub accounts:
  ✓ octocat (github.com) — in a mien profile
  · octo-work (github.com) — not imported
      mien login <profile> --service github --username octo-work
AWS profiles:
  · work — not imported
      mien login <profile> --service aws --aws-profile work
```

It reads no secret and writes nothing — importing stays an explicit `mien login`.

See `skills/mien/references/` for full docs.

## Bind a workspace to an identity

The simplest way to say "this workspace is `work`" is to declare it where the work is, with a `.mien` file:

```bash
mien claim work        # writes .mien, approves it, and git-ignores it globally
```

From then on, `mien run`/`mien which` (and everything beneath this directory) act as `work` with no central config to maintain. `.mien` is a private, local marker — `mien claim` adds it to your global git ignore so it never lands in a repo.

Because a `.mien` is a checked-out file, it does not choose an identity until you approve it — a cloned repository's `.mien` is **inert** until you run `mien allow`, and an edited one must be re-approved:

```
$ cd some-cloned-repo && mien which
Error: this directory declares profile 'work' in .mien but it is not allowed yet.
Run `mien allow` to approve it — a checked-out .mien cannot choose an identity
until you do — or remove the file.

$ mien allow           # you decide, not the repo
```

This is direnv's `allow`, applied to identity: the declaration lives with the project, but only your approval — recorded in your own config — lets it act. Precedence is `MIEN_PROFILE` (an explicit `mien use`) → an approved `.mien` → central scopes below.

## Project-pinned identity

For repositories scattered with no per-workspace file — or when you'd rather declare once for a whole tree or a whole GitHub org — a profile can claim directories (`default_for`) or remote owners (`owns_remotes`) centrally. A `.mien` in a workspace overrides these. Directory scopes:

```json
"profiles": {
  "work":     { "default_for": ["*/Projects/acme*"] },
  "personal": { "default_for": ["*/Projects/mien", "*/Projects/sayu"] }
}
```

```bash
mien which                       # → work
mien run -- gh pr list           # runs as whichever profile claims this directory
```

A scope covers the directory itself and everything under it. Sibling directories that merely share a prefix are not covered — `*/Projects/acme` does not capture `acme-fork`. When two profiles claim a directory, the longer scope wins; when they are equally specific, `mien` refuses rather than guessing, since picking one would misroute credentials silently.

`~` and `$VAR` are expanded, so `~/Projects/acme` and `$HOME/Projects/acme` both work. A variable that is not set — or is set to the empty string — is left as written, which matches nothing; the same goes for `~` under an empty `HOME`. The generated `project_env` shell would instead drop it and widen the scope, and quietly claiming more directories than intended is the wrong way to fail here.

If a profile is already active in the shell (`MIEN_PROFILE`), it wins — an explicit `mien use` is a deliberate act and a directory default should not undo it. `mien which` prints a warning to stderr when the two disagree.

Scopes live in your own config. Nothing in a checked-out repository can contribute a scope, name a profile, or reach an identity you have not already granted to some directory.

A cloned directory is still matched like any other, though: `*` spans `/`, the same as in the shell patterns these scopes compile to, so a scope of `*/work/*` claims a `work` directory at any depth — including one inside a repository you just cloned. `git clone` names the target after the remote by default, so this is reachable by accident, and it turns a directory that would otherwise fail closed into one that runs under a real identity. Anchor scopes at `~` or a literal root rather than a leading `*` if that matters to you.

Because resolution reads the filesystem on every call and keeps no state, it works the same in a long-lived terminal and in an AI agent that starts a fresh shell for every command.

## Who am I here — in the status line

The identity you would act as is worth seeing *before* you act, not after a personal commit has landed in a work repository. `mien statusline` renders a one-line segment for a [Claude Code status line](https://docs.claude.com/en/docs/claude-code/statusline) that keeps the answer in view and turns red when the active identity is wrong for the directory:

```json
// .claude/settings.json
{ "statusLine": { "type": "command", "command": "mien statusline" } }
```

The same segment shows in an ordinary terminal via `mien prompt` — so the indicator follows you into any interactive shell, not only Claude Code's status bar (it renders in a human terminal's prompt, not inside an agent's non-interactive turn):

```bash
# zsh
setopt PROMPT_SUBST; RPROMPT='$(mien prompt)'
```

```
🟢 mien:work                       # this place's identity agrees with what's active
🔴 mien:personal ✗ repo is work's  # personal is active, but this repo belongs to work
🔴 mien:personal ✗ dir wants work  # ...or a directory scope claims work
🔴 author:personal ✗ repo is work's # nothing active, but git user.email would commit as personal
🟡 mien:— no profile here          # nothing set, and nothing claims this place
```

The red case is the one that matters: an agent session that inherited `personal` from the shell it launched in, sitting in a `work` repository, is exactly how the wrong identity commits.

The `author:` case closes that even when you activate nothing: it compares the git `user.email` a commit here would carry against the emails your profiles already declare (their Google and Atlassian accounts, and the GitHub no-reply address). If that email positively belongs to a *different* profile than the repository's owner, it warns — including the subtle case where the right profile is active but `user.email` is still stale. It stays quiet on an email it does not recognize, so a legitimate alternate address raises no false alarm.

It figures out whose place this is from two signals — the repository's `origin` owner (`owns_remotes`) and directory scopes (`default_for`) — so it works whether or not you organize by directory. If you keep repositories side by side with no per-employer folder, the remote owner is what makes it sharp:

```json
"profiles": {
  "work":     { "owns_remotes": ["github.com/acme-*/*"] },
  "personal": { "owns_remotes": ["github.com/me/*", "github.com/me-labs/*"] }
}
```

A profile can own several remote patterns — a personal account and the organizations it also manages. The remote owner **never selects an identity**: it drives the status line's display and warning, and it can *refuse* an action (`mien guard`, and the `mien exec` check below), but it never decides which profile `which`/`run` resolve to — because a checked-out repository controls its own remote and must not be able to choose the identity that acts. Blocking is the safe direction: a crafted `origin` can at worst cost you a false refusal, never a mis-action. It reads config names and scopes only, never a token, so it is safe to run at status-line frequency, and it prints nothing when `mien` is not configured.

## Refuse to act as the wrong you

The status line *shows* a wrong identity; `mien guard` *stops* it. It exits non-zero — refusing the action — only when the identity is confidently wrong for the repository: the active profile, or the git author a commit would carry, positively belongs to a different profile than the repository's owner. Wire it as a pre-commit hook and a mis-authored commit never lands:

```bash
echo 'exec mien guard' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```

```
$ git commit -m "wip"
mien: refusing — a commit here would be authored as personal, but this repository belongs to work.
  Fix: activate the right profile (`mien-use <profile>`) or correct git user.email.
  Override once: MIEN_GUARD=off <command> (or `git commit --no-verify` for a hook).
```

For a global hook across every repository, point `core.hooksPath` at a directory holding a `pre-commit` that runs `exec mien guard`. Chain it before other risky actions too — `mien guard && git push`.

It refuses only what it is sure about: no config, an unknown owner, or an unrecognized author all *allow* (it never blocks on a guess), and any internal error allows too, so a bug can't wedge your commits. Every refusal is overridable — `MIEN_GUARD=off`, `--force`, or `git commit --no-verify` — so it guides rather than traps. Using the repository's own signals to *block* is safe in a way that using them to *act* is not: a crafted `origin` can at worst cause a false refusal you override, never a mis-action.

### `mien exec` refuses the wrong identity for this place

`mien exec <profile> -- <cmd>` names its identity explicitly, which is why it is the right form for an agent — no shell state to survive between calls. The cost is that the name is only as good as whoever typed it, and an agent that types the wrong one hands a whole credential bundle to a command running in someone else's repository, with nothing in the transcript that looks unusual.

So when an **agent harness is driving the call** and the named profile contradicts the profile this place visibly claims, `exec` refuses before any credential is loaded:

```
$ mien exec personal -- gh pr create
Error: refusing to hand over credentials: this call asked for profile 'personal', but this repository belongs to 'work' (its git origin owner).
  Nothing ran and no credential was loaded — mien declined the handover; the command itself did not fail.
  Act as the identity this place claims:
    mien exec work -- <your command>
  If 'personal' really is the right identity for this work, run it somewhere that belongs to 'personal'.
```

An **approved `.mien` outranks the repository**: in a workspace you bound with `mien claim`/`mien allow`, that profile is the claim, so `exec` allows it even when `origin` belongs to someone else — and refuses the *other* profile against the declaration. That keeps the gate agreeing with `which`, `run` and the status line about the same directory. An unapproved `.mien` counts for nothing here, exactly as it counts for nothing when choosing an identity that acts.

A person at a terminal never meets this check at all: it fires only under the same agent-harness detection `mien token` uses (`$CLAUDECODE`, `$CLAUDE_CODE_ENTRYPOINT`, or `MIEN_CAPTURED=1` for a harness mien does not recognize). There is deliberately **no `--force`** — an override an agent can reach for is one it will reach for, and the refusal is the entire control.

Like `guard`, it fails open on every uncertainty: no config, nothing claiming this place, an ambiguous claim, no repository or no remote, or any internal error all allow the handover. For a false refusal you need to get past while you fix the underlying config, set `MIEN_EXEC=off` (also `0`, `false`, `no`) — the same convention as `MIEN_GUARD`. It is documented here and not in the error text, because the human who needs it reads the docs and the agent that must not have it reads the error.

## Commit as the right you — git's own job

Blocking a mis-authored commit is the backstop; the better fix is git never producing one. Git already does this natively with `includeIf` — mien does not reinvent it. Point git at a per-identity config by directory or by repository owner, and git itself stamps the correct `user.email`/`user.name`:

```gitconfig
# ~/.gitconfig
[includeIf "gitdir:~/work/"]          # everything under ~/work commits as work
    path = ~/.gitconfig-work
# by repo owner — one hasconfig covers https:// and ssh://, a second covers the scp git@…: form
[includeIf "hasconfig:remote.*.url:**/*github.com/acme-inc/**"]
    path = ~/.gitconfig-work
[includeIf "hasconfig:remote.*.url:**github.com:acme-inc/**"]
    path = ~/.gitconfig-work
```

```gitconfig
# ~/.gitconfig-work
[user]
    email = me@acme.example
    name  = acme-me
```

A single `**/*github.com/acme-inc/**` glob matches `https://` and `ssh://` remotes but *not* the scp `git@github.com:acme-inc/…` form (its leading `**` can't cross the `:` boundary), so the scp form needs the second `**github.com:acme-inc/**` rule — both verified against real `git config` resolution. Then tell mien the address that profile commits under by adding `git_email` to it in `~/.config/mien/config.json` — the same place routing scopes like `owns_remotes` live:

```jsonc
"work": { "owns_remotes": ["github.com/acme-*/*"], "git_email": "me@acme.example" }
```

mien never writes git's `user.email` (that's git's includeIf above); it reads `git_email` only so the [guard](#refuse-to-act-as-the-wrong-you) and status line can warn when a commit's `user.email` disagrees with the identity acting here — the case git's own rules can't catch.

## Your own credentials

The seven built-in services are not the whole of an identity. An LLM API key, an npm or PyPI token, a database URL — anything that is one env var carrying one secret — goes in the same profile and arrives the same way:

```bash
mien login work --service custom --name ANTHROPIC_API_KEY          # hidden prompt, nothing in argv or history
mien login work --service custom --name NPM_TOKEN --secret-cmd 'op read op://Private/npm/token'
mien exec  work -- claude -p "review this"                          # arrives as $ANTHROPIC_API_KEY
mien logout work --service custom --name ANTHROPIC_API_KEY          # deletes the secret, forgets the name
```

The secret goes to your backend under the usual rendered name, with the variable name in it exactly as you wrote it (`mien-work-custom-ANTHROPIC_API_KEY`) — env var names are case-sensitive, so `TOKEN` and `token` are two variables and get two secrets. The profile stores only a **reference** to it:

```jsonc
"work": {
  "custom": {
    "ANTHROPIC_API_KEY": "<backend-ref>",
    "NPM_TOKEN":         "<backend-ref>"
  }
}
```

That is the difference from `project_env` below, which is for non-secret values only: a `project_env` value is stored verbatim in `config.json` and uploaded verbatim in the backend manifest, while a `custom` value never leaves the backend. `mien status` shows a custom variable as `<set>`; `mien list` and `mien whoami` show the names. There is no `mien token custom` — `token` prints a raw secret on stdout, and `mien exec` is the answer.

Switching profiles clears them. `mien use` unsets every variable mien manages before exporting the new profile's, and that list includes every `custom` name **any** profile defines — so moving from a profile with an `ANTHROPIC_API_KEY` to one without it clears the key instead of handing one identity's credential to another. (`mien exec`/`run` still layer over the ambient environment without scrubbing, as they always have.)

A name has to be a shell identifier (`[A-Za-z_][A-Za-z0-9_]*`) — `mien use` writes a script that gets sourced, and a name the shell won't take breaks the loader — and it may not be one of the nineteen variables mien already exports itself, whether for a built-in service or for its own bookkeeping (`MIEN_PROFILE`, `MIEN_EPHEMERAL_DIR`). Beyond those, one rule: a variable that the shell or mien *reads as an instruction* cannot be taken over to carry payload, because the scrub list is the union over *all* profiles — one such name in one profile makes every `mien use` and every `mien-unset`, in every shell, `unset` it, so a name in a profile you never activate reaches shells that have nothing to do with it. Two sets fall under that rule. Six are shell- or mien-critical — `PATH`, `HOME`, `IFS`, `PS1`, `TMPDIR`, `MIEN_CONFIG` — where stripping or overwriting the value breaks the shell, or mien's own loader, ephemeral store and config. Three are the markers mien reads to know an agent harness is recording this command — `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `MIEN_CAPTURED` — where *absence* is the permissive state, so the scrub's `unset` disarms `mien token`'s refusal to print a secret into a recorded transcript and [`mien exec`'s refusal to act as the wrong identity](#mien-exec-refuses-the-wrong-identity-for-this-place), and disarms them silently. The match is exact, so `MY_PATH` and `PATH_TO_KEY` are fine. All four refusals fire when you log in *and* when the config is parsed, so a hand-edit fails the same way; each error says what the name already means to mien or the shell, and a collision with a service's variable names the service it would have fought so you can reach for that service's own `--service` instead.

## Ambient per-project env

Some env vars (e.g. `AWS_PROFILE`) are handy set automatically just by `cd`-ing into a project directory, without running `mien use`. Configure them under a profile's `project_env`, then materialize them:

```bash
mien env sync
```

This renders every profile's `project_env` scopes into `~/.config/mien/ambient.zsh` and wires `~/.zshenv` to source it (idempotent — safe to re-run). Values are non-secret only (no secrets-backend references). Coverage is **non-interactive zsh only** — `~/.zshenv` is read by every zsh invocation, but not `/bin/sh` or `bash -c`. Before writing anything, the generated script is validated with `zsh -n`; if it fails to parse, `env sync` aborts (nonzero exit) and leaves the previous ambient file and `~/.zshenv` untouched.

Example config (`match` is a bare directory glob — the directory itself and everything under it):

```json
"profiles": {
  "work": {
    "project_env": [
      {"match": "*/work/arinyaho", "env": {"AWS_PROFILE": "work"}}
    ]
  }
}
```

### Variables in a `match` scope

The generated script is sourced from `~/.zshenv`, and zsh reads `~/.zshenv` **before** `~/.zshrc` and `~/.zprofile`. A variable you export from your own dotfiles is therefore unset at the moment the scope is matched, and zsh expands it to nothing: `{"match": "$WORK_ROOT/*"}` becomes the pattern `/*`, which matches every absolute path, so that scope's env — `AWS_PROFILE` and all — is applied in every directory. A reference in the middle of a path is not broader but wrong in a different way: `$WORK_ROOT/acme` becomes `/acme`, disjoint from the tree you meant.

Only parameters that already have a value that early are safe: the ones zsh sets itself (`$HOME`, `$PWD`, `$PATH`, `$UID`, …) and the ones the login process puts in the environment zsh inherits (`$USER`, `$SHELL`, …). `~` is safe too — tilde expansion consults the password database and needs no variable. `$TMPDIR` is deliberately *not* on that safe list: macOS launchd sets it, but stock sshd and a default Linux PAM do not, so `env sync` warns about a `$TMPDIR`-rooted scope rather than assume one OS. For anything else, write a literal path. `mien env sync` warns on stderr, naming the profile and the scope, and still writes the file, so a config that works today keeps working.

A scope only covers a git worktree if the worktree's path is itself under the scope's glob — a worktree created in a sibling `/worktrees/` directory outside `*/work/arinyaho` is NOT covered.
