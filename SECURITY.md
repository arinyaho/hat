# Security

`mien` holds credentials. This document says what it protects, what it does not, and where every secret it touches actually lives — including the parts that are uncomfortable. A tool in this category earns trust by being specific about its limits, so the sections below name them rather than implying they do not exist.

Everything here is stated against the code. If you find a claim the code does not support, that is a bug in this file and worth reporting.

## Protection goals, and what is not protected

**Isolating identities between concurrent shells.** Activation only ever writes to the current process environment and to files keyed by that process. Two terminals, or two AI agent sessions, can hold different identities at once without coordinating.

*Not protected:* the credential stores those identities point at are shared. `AWS_PROFILE` selects among credentials in `~/.aws`; `CLOUDSDK_ACTIVE_CONFIG_NAME` selects a gcloud configuration whose credential store is global. Two sessions that run an interactive `gcloud auth login` at the same time will race.

**Routing the environment-variable plane.** Activation sets environment variables, so anything that reads its credentials from the environment — `gh`, `aws`, `gcloud`, `oci`, `curl`, a client library — acts as the profile you named.

*Not protected:* nothing outside that plane. An AI harness's own service connectors (Atlassian, Slack, Notion, Google) authenticate out of band to one fixed account and are unaffected by a profile switch — a session that "switched to `work`" still reads Jira as whatever account the connector holds, with no error. For any service a profile has credentials for, go through `mien exec <profile> -- ` and the service's REST API rather than a connector; the agent skill (`plugins/mien/skills/mien/SKILL.md`) states this as its first rule.

**Keeping secret values out of your home directory.** GitHub tokens, Slack tokens, Atlassian and Notion tokens, Google refresh tokens, and AWS keys live in a secrets backend, not in a dotfile. What lands on disk locally is references and identifiers.

*Not protected:* several services are *selected*, not replaced — see [What stays in your home directory](#what-stays-in-your-home-directory). And "not in your home directory" is not "not on disk": activation writes short-lived credential files under `$TMPDIR`, described below.

**Keeping secrets out of transcripts, shell history, and `ps`.** `mien use` never prints a secret; it writes the exports to a mode-0600 file and prints a one-liner that sources and deletes it. On a real terminal it refuses to run at all unless you pass `--print`. `mien login` reads secrets through a hidden prompt, a `--secret-cmd` reference, or stdin.

*Not protected:* `mien token <service>` prints a token to stdout by design — that is its purpose — and for Atlassian and Notion that token is long-lived, not a short-lived access token. Whatever captures that stdout holds the secret. Because the usual way that goes wrong is accidental — piping the token into a program that echoes its input, so a traceback or usage error writes the secret into a session log — `mien token` refuses by default when it detects an agent harness that records output — the only harnesses it recognizes on its own are Claude Code's (`$CLAUDECODE`, `$CLAUDE_CODE_ENTRYPOINT`) — and names `mien exec <profile> -- <cmd...>`, which passes the credential in the environment instead. The detection is a heuristic and fails *open*: an unrecognized harness is not detected unless you mark it yourself by setting `MIEN_CAPTURED=1`, which triggers the same refusal in any harness. Every refusal is overridable (`MIEN_TOKEN=capture-ok`, `--force`), so it guides rather than traps, and it changes nothing for a person at a terminal. It reduces accidents; it does not make the value unreadable — see the next paragraph.

**Hiding secrets from an AI agent: NOT a goal.** This is the most important line in this document. `mien` puts credentials into the environment so that ordinary tools pick them up. An agent that can run commands in that environment can read them, and so can every process it starts. If your threat model is a prompt-injected agent exfiltrating a token, `mien` is the wrong layer — you want a credential proxy that never lets the value reach the agent at all. `mien` trades that isolation for working with every CLI and SDK unmodified, and for letting a person choose which identity to act as.

**Integrity of the configuration: NOT protected, and the consequence is worse than misrouting.** The config file is plain JSON with no signature or checksum. Anything that can write it — or set `$MIEN_CONFIG` — decides which backend `mien` talks to and which directories map to which identity.

It also reaches further than that. A `project_env` value is emitted into `ambient.zsh` inside a double-quoted `export`, with only `\` and `"` escaped — `$` and backticks are left intact deliberately, because that is how a value like `$HOME/bin` is meant to work. `mien env sync` validates the result with `zsh -n`, which *parses* without executing, so a command substitution passes the gate. `~/.zshenv` then sources that file in **every** zsh you start, including non-interactive ones. So a `project_env` value is executable code, and anyone who can write your config — or your backend manifest, which `mien init --yes` imports without prompting — can run commands as you.

Treat the config file and the backend manifest as trusted input. Do not import a manifest you did not create.

**Choosing an identity that acts never trusts the repository — only your approval.** `mien run`, `exec`, and `which` resolve identity from your own config — an explicit `MIEN_PROFILE`, or a directory `default_for` scope — or from a project-local `.mien` declaration, **but only one you have approved with `mien allow`**. A `.mien` is a checked-out file, so the profile name in it is repo-controlled; what authorizes it to act is your approval, recorded in your own state (`~/.config/mien/allowed.json`, 0600, keyed by the file's absolute path *and* the profile it names) — never the file alone. A cloned repository's `.mien` is inert until you approve it, and an edited declaration must be re-approved, so a clone still cannot choose which identity acts: you do, and an unapproved declaration makes `mien which`/`run` fail loud rather than route. A profile's `owns_remotes`, which matches a repository's `origin` owner, and the git-author cross-check, which reads the repository's `user.email`, are used *only* by the status line: to display whose repository this is and to warn when the active identity — or the email a commit would carry — disagrees. All of these repo-controlled signals are deliberately kept off the acting path except through your explicit approval, because a clone controls its own `.mien`, `origin`, and repo-local `user.email` and could otherwise steer which identity a command runs as. Displaying a warning spends no credential and cannot mis-act, so the advisory use stays safe even against a crafted remote or a planted `user.email`.

`mien guard` uses the same repo signals to *block* an action, and that direction is safe where selecting an identity is not: a crafted `origin` can at worst trigger a false refusal — an annoyance you override with `MIEN_GUARD=off` — never a mis-action. The gate is fail-open by design (it allows on any uncertainty or internal error), so it is a guard rail, not a guarantee: it stops the mistakes it is sure about and never wedges a legitimate action.

`mien exec` blocks in that same direction, and only in that direction. When an agent harness is driving the call — detected exactly as `mien token`'s capture refusal detects it — and the profile named on the command line contradicts the profile this place claims by `owns_remotes` or `default_for`, the handover is refused before any credential is loaded. The repo signal still cannot *choose* an identity here: it can only veto the one that was named explicitly, so a crafted `origin` buys a false refusal and never a mis-routed credential. This gate is fail-open on every uncertainty and on any internal error too, and it never fires for a person at a terminal. It has no per-call `--force`, on the reasoning that an override the agent can see is an override the agent will use; `MIEN_EXEC=off` exists for a human debugging a false refusal and is documented in the README rather than named in the refusal.

**Non-repudiation: no protections attempted.** `mien` keeps no audit log. Nothing records which identity was activated when, or which command ran under it.

## What is stored where

| Location | Mode | Contents | Removed by |
|---|---|---|---|
| `~/.config/mien/config.json` (override with `$MIEN_CONFIG`) | 0600 | Backend type and options, bootstrap account, secret-name templates, per-profile identifiers and **references** — including a `custom` block's variable names and the reference each points at, never its secret. No secret value, with one exception: `project_env` values are stored verbatim, so a secret typed there lands here | nothing |
| `~/.config/mien/ambient.zsh` | 0600 | Generated `case` blocks exporting each profile's `project_env` values | rewritten by `mien env sync` |
| `~/.zshenv` (a marked region) | 0600 | One line sourcing `ambient.zsh` | nothing |
| `~/.config/mien/allowed.json` | 0600 | Approved `.mien` declarations (declaration path → profile). No secret | edited by `mien allow` / `mien claim` |
| `~/.config/git/ignore` (git's `core.excludesFile`) | your umask | One `.mien` line appended so declarations are globally git-ignored. No secret | nothing |
| `$TMPDIR/mien/<pid>-<profile>-adc.json` | 0600 | **Secret.** Google OAuth client secret + refresh token | see [Lifetime](#lifetime-and-cleanup) |
| `$TMPDIR/mien/<pid>-<profile>-ssh_key.json` | 0600 | **Secret.** A GitHub SSH private key | see [Lifetime](#lifetime-and-cleanup) |
| `$TMPDIR/mien/<pid>-<profile>-slack.json` | 0600 | **Secret.** Every Slack token on the profile, by workspace | see [Lifetime](#lifetime-and-cleanup) |
| `$TMPDIR/mien/env-<random>.sh` | 0600 | **Secret. The highest-value file here** — every exported variable, including every token, in one place | the `. '<path>' && rm -f '<path>'` one-liner `mien use` prints |
| Secrets backend | backend's own | Every secret value | `mien logout` |

`$TMPDIR/mien/` itself is created with your umask, so typically 0755. The files inside are 0600, but their **names encode the profile and PID**. Where `TMPDIR` is a shared `/tmp`, other local users can list which identities exist and when they are active. On macOS the default `TMPDIR` is a per-user 0700 directory, so that exposure does not apply there.

Beyond the table above, `mien` writes nowhere else. It never writes to `~/.ssh`, `~/.aws`, `~/.oci`, `~/.config/gh`, `~/.gitconfig`, or gcloud's credential store. Two documented writes edit a tool's own config, never a credential: `mien claim` appends `.mien` to git's global ignore (`~/.config/git/ignore`, in the table), and `mien init` with the GCP backend runs `gcloud auth application-default set-quota-project`, causing gcloud to update its own ADC file. Authoring git commits per identity is left to git's native `includeIf` — mien reads a profile's `git_email` for its author cross-check but never writes any gitconfig.

## The environment-variable surface

This is where a secret is most exposed, and it is worth being exact about who can see it.

Variables that carry a **secret value directly**: `GH_TOKEN`, `MIEN_SLACK_DEFAULT_TOKEN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `ATLASSIAN_API_TOKEN`, `NOTION_TOKEN`, and every variable named in a profile's `custom` block (that is what the block is for — one name you chose, one secret from the backend).

Variables that carry a **path to a secret**: `GOOGLE_APPLICATION_CREDENTIALS`, `MIEN_SLACK_TOKENS`, and the key path inside `GIT_SSH_COMMAND`.

Variables that carry **only a selector**: `MIEN_PROFILE`, `MIEN_EPHEMERAL_DIR`, `CLOUDSDK_ACTIVE_CONFIG_NAME`, `CLOUDSDK_CORE_PROJECT`, `AWS_PROFILE`, `AWS_DEFAULT_REGION`, `OCI_CLI_PROFILE`, `OCI_CLI_CONFIG_FILE`, `ATLASSIAN_EMAIL`, `ATLASSIAN_BASE_URL`.

Once `mien use` has run in a shell, the first group is readable by that shell and **every process it starts afterwards** — an editor, a language server, a build script, an AI agent — whether or not that process has anything to do with the profile. On Linux, anything able to read `/proc/<pid>/environ` for your UID sees them; on macOS, `ps -E` does. There is no expiry and no scoping: the exposure is bounded by the shell's lifetime, and `mien-unset` is the only thing that ends it early. (Bare `mien unset` only prints the commands — like `mien use`, it cannot change the calling shell.)

`mien exec` and `mien run` narrow this considerably — the variables exist only for the duration of one child process — which is why they are the recommended form, especially for agents.

`mien exec` and `mien run` do not **scrub**. The profile's variables are layered over the environment you already had. A profile with no AWS identity leaves an ambient `AWS_ACCESS_KEY_ID` untouched, so the child uses it — and the same is true of a `custom` variable another profile defines. If you need certainty about a service the profile does not define, clear it yourself or check with that service's own identity command. (`mien use` is the exception: it `unset`s every managed variable before re-exporting the profile's, so activating a second profile in a shell replaces the first cleanly rather than layering over it — the child-process forms cannot, since they build the child env from the ambient one. "Every managed variable" includes every `custom` name defined by *any* profile in the config, not just the one being activated, so switching identities does not leave live in the next one's shell a custom credential whose name the config still holds. When the config cannot be read, `mien use` fails outright and `mien unset` clears the built-ins and says on stderr that custom ones may survive.)

The scrub list is derived from the config every time it is needed, so it covers exactly the `custom` names the config holds **at that moment** — and nothing about a name that has since left it. A name leaves when you run `mien logout <profile> --service custom --name <VAR>`, when that key is renamed in the config, or when its profile is removed from the config (by hand, or by a `mien sync` that pulls a manifest without it). A shell that already exported `VAR` keeps it: no later `mien use` or `mien-unset` clears it, and `mien status` — which reads the same custom half of the list — no longer reports it as set, so the stale credential is neither cleared nor visible. Run `mien-unset` in that shell *before* the name leaves the config, or `unset VAR` by hand afterwards; a shell that never inherited the variable is unaffected.

## What stays in your home directory

`mien` replaces a home-directory credential for: GitHub tokens, Slack, Atlassian, Notion, per-profile Google ADC, AWS in access-key mode, and GitHub SSH when the key was stored in the backend.

It only *selects among* credentials that remain on disk for:

| Service | What remains |
|---|---|
| AWS, profile mode | `~/.aws/credentials` or `~/.aws/config` — `mien` stores no credential and sets `AWS_PROFILE`, plus `AWS_DEFAULT_REGION` when the profile carries a region |
| OCI, always | `~/.oci/config` and the API-key PEM it names. `mien` stores no OCI secret at all |
| gcloud CLI identity | The gcloud configuration and its credential store. `CLOUDSDK_ACTIVE_CONFIG_NAME` names a configuration; it does not supply one |
| GitHub SSH, path mode | Your private key stays in `~/.ssh` |
| `mien`'s own backend access | The bootstrap Google ADC, for the GCP backend |
| `macos_keychain` backend | Your login keychain, `~/Library/Keychains/` — encrypted at rest, but the secrets are in `$HOME` |
| `keyring` backend | The OS credential store, typically `~/.local/share/keyrings` — same caveat |

So `mien` writing no token files into your home directory is true of the files it writes itself. It is not a claim that no credential of yours remains in `$HOME` — the table above is the list that does remain.

## Backends

| Backend | Where values live | Authenticated by | A compromise of that yields | Network |
|---|---|---|---|---|
| `gcp_secret_manager` | Google Secret Manager | Application Default Credentials | read, write and delete of every `mien` secret the principal's IAM allows, plus the config manifest | yes |
| `macos_keychain` | macOS login keychain | your logged-in session | every `mien-*` item | no |
| `keyring` | OS credential store | an unlocked desktop session | every stored item | no |

Compromise of the bootstrap credential is equivalent to compromise of every identity the cloud backend holds. It is the single most valuable thing on the machine and deserves the strongest protection you can give it.

**The manifest.** With a cloud backend, `mien` stores a copy of the configuration as a secret named `mien-config-manifest`, and pushes it after `mien login` and `mien logout`, on a best-effort basis — a failed push is a warning, not an error. `mien init` writes the local config without pushing, and `mien push` is the explicit manual path. It contains references and identifiers only — no secret value — with one exception: values you place in `project_env` are copied verbatim, so a secret typed there is uploaded. Do not put secrets in `project_env`.

The cloud backend updates the manifest in place on each push — a new version of one fixed secret — so the copy a second machine pulls tracks your latest config. (Nothing deletes the manifest; it is only ever rewritten.)

Two commands adopt that manifest. `mien init --yes` imports it without prompting, and `mien sync` pulls it and replaces the local config — also without prompting under `--yes`. Its interactive confirmation lists only profile *names*, so a changed `project_env` value shows up as `~ change: work` and nothing more. A backend an attacker controls can therefore redefine every profile — including which directories claim which identity, and including the `project_env` values that become executable code — through either path.

## Lifetime and cleanup

`mien exec` and `mien run` delete the credential files they created when the child exits — on normal exit, a non-zero exit, a signal, or Ctrl-C.

`mien use` deliberately leaves its files behind, because your shell needs them after the process ends. Cleaning them up is best-effort:

- The shell integration runs `mien doctor --gc` from an `EXIT` trap, but only when a profile is active in that shell.
- That command performs a backend health check first, so **offline, or with an expired bootstrap credential, nothing is swept.**
- A shell that never exits — a long-lived terminal, a multiplexer pane, an agent session — never fires the trap.

The practical consequence: sweeping is intent, not a guarantee. Assume a 0600 file may persist under `$TMPDIR` until something sweeps it, and treat `$TMPDIR` as sensitive.

The `env-<random>.sh` file deserves its own note, since it holds every exported secret in one place. `mien use` writes it and prints a one-liner that sources and then deletes it — so it is removed as soon as you `eval` the output. If you never do (you captured stdout instead, used `--print`, or the `source` failed so the `&& rm -f` never ran), it stays. The sweep only reclaims it after five minutes, and only when a `mien doctor --gc` actually runs, which the previous paragraph's conditions govern. Capturing `mien use` output without evaluating it therefore leaves a complete set of your credentials on disk.

`mien-unset` clears the variables. It does not delete anything on disk. Bare `mien unset` only prints the `unset` lines for you to eval, and unlike `mien use` it has no terminal guard, so it looks like it worked.

`mien logout` removes a secret from the backend. On GCP the secret and all its versions are destroyed at once; note though that re-running `mien login` for a service adds a new version without disabling the old ones, so rotating this way leaves the previous value readable until you delete the secret. Logout also does not reach into shells that already hold the value, or the files they point at — those keep working until the shell exits or a sweep runs.

## Known weaknesses

These are real and currently unfixed. They are tracked, and listed here rather than omitted.

- **A bare `eval "$(mien use <profile>)"` — without `--owner-pid` and not through the `mien-use` wrapper — keys its ephemeral credential files to mien's own process**, which exits immediately. A later `mien doctor --gc` from any shell then reclaims those files as belonging to a dead process, while the shell that activated is still using them. The effect is breakage, not disclosure. The `mien-use` wrapper passes `$$` so the files are keyed to your shell instead; use it, or pass `--owner-pid $$`.
- **`mien env sync` rewrites `~/.zshenv` atomically**, which replaces the file and normalizes its mode to 0600. Content outside `mien`'s marked region is preserved, but hard links, ACLs and extended attributes on that file are not.
- **`$MIEN_CONFIG` is honoured without validation**, so anything able to set that variable redirects the whole configuration.
- **`--secret-cmd` runs its argument through a shell.** It is yours to supply, so it crosses no privilege boundary, but it is an execution path in a credential tool.
- **Storing a GitHub SSH private key in a cloud backend is offered without comment**, and nothing checks whether the key has a passphrase.

## Verifying a release

Releases are not currently signed, and installation is from source. Until that changes, verify by reading: the code is MIT-licensed and the storage surface is small enough to audit in an afternoon — the four functions that write to disk are in `config.py`, `ambient.py`, `ephemeral.py`, and `shell.py`.

## Reporting a vulnerability

Open a GitHub issue for anything already public, such as a wrong claim in this file. For something exploitable that is not yet public, use GitHub's private vulnerability reporting on this repository rather than an issue.

There is no bug bounty and no guaranteed response time — this is a single-maintainer project, and pretending otherwise would be its own kind of security theatre.
