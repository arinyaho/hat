from __future__ import annotations

from string import Formatter
from typing import NamedTuple

# The built-in templates. Defined here, beside the renderer, rather than at each
# use, so the parser's fallback (`mien.config._config_from_dict`), the fresh
# config `mien init` writes, and the fix a template error recommends cannot drift
# apart -- a message that recommended a template the parser then refused would be
# worse than no message.
BUILTIN_DEFAULT = "mien-{profile}-{service}-{kind}"
BUILTIN_SLACK_TOKEN = "mien-{profile}-slack-{workspace}-token"

# The tokens each template MUST spend, so that one credential gets one secret
# name. Every one is load-bearing: `kind` separates two credentials of one
# service (github `token` from `ssh_key`, google `oauth_client_secret` from
# `refresh`, and for `--service custom` one variable name from the next, which
# is all that keeps ANTHROPIC_API_KEY and NPM_TOKEN apart); `service` separates
# two services on one profile (atlassian's `api_token` from notion's); `profile`
# separates the identities, without which `work` and `personal` share one github
# secret; `workspace` separates two Slack workspaces. `slack_token` takes no
# `service` because the literal "slack" is the template's own text.
#
# This is EXACTLY the set each template is supplied with, so
# `_check_secret_name_template` reads it in both directions: a required token
# the template does not spend collapses two credentials onto one secret, and a
# field it asks for that is not here cannot render at all. A new call site that
# supplies a further token belongs here, or every existing config becomes
# unrenderable.
REQUIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "default": ("profile", "service", "kind"),
    "slack_token": ("profile", "workspace"),
}

# What a template that drops a token collapses onto a single secret name. Used to
# say, in the error, which credentials would land on top of each other.
TOKEN_SEPARATES: dict[str, str] = {
    "profile": "every profile renders the same name",
    "service": "every service renders the same name",
    "kind": "every credential of one service renders the same name, and every "
            "`--service custom` variable with it",
    "workspace": "every Slack workspace renders the same name",
}


def render_name(template: str, **tokens: str) -> str:
    return template.format_map(_StrictDict(tokens))


class TemplateField(NamedTuple):
    """One replacement field of a template, as written.

    ``name`` is the field name (``"kind"``, ``"kind[0]"``, ``""`` for a
    positional ``{}``). ``plain`` says whether the field reproduces that token
    VERBATIM: no conversion, no format spec, no attribute access, no indexing.
    ``text`` is the field written back out, for an error message to quote.
    """

    name: str
    plain: bool
    text: str


def template_fields(template: str) -> list[TemplateField]:
    """Every replacement field of ``template``, in order, classified.

    Parsed with ``string.Formatter`` rather than searched for as substrings,
    because the two disagree in exactly the cases that matter. ``"{{kind}}"`` is
    an escaped literal that renders the characters ``{kind}`` for every
    credential -- it contains ``{kind}`` and substitutes nothing, and ``parse``
    reports no field for it at all.

    A malformed template (``"mien-{profile"``) raises ``ValueError`` from
    ``parse``, which is the caller's to translate -- it is a template that cannot
    render at all.
    """
    fields = []
    for _, name, spec, conversion in Formatter().parse(template):
        if name is None:
            continue
        plain = conversion is None and not spec and "." not in name and "[" not in name
        text = "{" + name
        if conversion is not None:
            text += "!" + conversion
        if spec:
            text += ":" + spec
        fields.append(TemplateField(name, plain, text + "}"))
    return fields


def template_tokens(template: str) -> set[str]:
    """The token names ``template`` substitutes VERBATIM, and nothing else.

    Only plain fields count, because only a plain field reproduces the token --
    which is the whole of what keeps two credentials on two secret names:

    - ``{kind[0]}`` / ``{kind.foo}`` reach INTO the token instead of spending it,
      so two kinds sharing a first character still render one name; the field
      name is not ``kind`` either way.
    - ``{kind:.1s}`` truncates it to one character, and ``{kind:.0s}`` renders
      nothing at all -- byte-identical to dropping ``{kind}``. Every value here is
      already a ``str``, so a format spec can only pad it, truncate it, or do
      nothing; none of those is a use, and truncation is an active collision.
    - ``{kind!r}`` wraps it in quotes: renderable, but a quoted secret name is
      never what someone meant.

    So a decorated field is not "the token, presented differently" -- it is a
    different function of the token, and the collision check has no business
    treating it as the token. See ``mien.config._check_secret_name_template``.
    """
    return {f.name for f in template_fields(template) if f.plain}


class _StrictDict(dict):
    def __missing__(self, key: str) -> str:
        raise KeyError(f"missing token {{{key}}} in template")
