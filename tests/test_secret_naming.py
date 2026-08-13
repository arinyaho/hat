import pytest

from mien.secret_naming import (BUILTIN_DEFAULT, BUILTIN_SLACK_TOKEN,
                                REQUIRED_TOKENS, render_name, template_fields,
                                template_tokens)


def test_renders_default_template():
    assert render_name(
        "mien-{profile}-{service}-{kind}",
        profile="personal", service="google", kind="refresh",
    ) == "mien-personal-google-refresh"


def test_renders_slack_template_with_workspace():
    assert render_name(
        "mien-{profile}-slack-{workspace}-token",
        profile="work", workspace="team-a",
    ) == "mien-work-slack-team-a-token"


def test_missing_token_raises():
    with pytest.raises(KeyError):
        render_name("mien-{profile}-{kind}", profile="x")


def test_template_tokens_reports_what_a_template_substitutes():
    assert template_tokens(BUILTIN_DEFAULT) == {"profile", "service", "kind"}
    assert template_tokens(BUILTIN_SLACK_TOKEN) == {"profile", "workspace"}
    assert template_tokens("mien-secret") == set()


def test_template_tokens_counts_only_what_actually_substitutes():
    """The two cases that make a substring search for "{kind}" the wrong test.

    `{{kind}}` is an escaped literal — it renders the characters `{kind}` for
    every credential — and `{kind[0]}` reproduces one character of the token, so
    two kinds sharing a first character still render the same name. Neither keeps
    two credentials apart, so neither counts as the token being present.
    """
    assert template_tokens("mien-{profile}-{{kind}}") == {"profile"}
    assert render_name("mien-{profile}-{{kind}}", profile="work") == "mien-work-{kind}"


@pytest.mark.parametrize("template", [
    # Truncating specs: `.1s` keeps one character of the token, `.0s` keeps none.
    "mien-{profile}-{kind:.1s}",
    "mien-{profile}-{kind:.0s}",
    # A width pads with spaces; a conversion quotes. Neither reproduces the token.
    "mien-{profile}-{kind:>20}",
    "mien-{profile}-{kind!r}",
    # Reaching into the token rather than spending it.
    "mien-{profile}-{kind[0]}",
    "mien-{profile}-{kind.foo}",
])
def test_a_decorated_field_is_not_the_token(template):
    """The hole this closes: a spec renders SOMETHING, so it looked like presence.

    `{kind:.0s}` renders every credential to one name — identical to dropping
    `{kind}` — and `{kind:.1s}` collapses ANTHROPIC_API_KEY onto AWS_THING at the
    first character. Since the values are already `str`, a spec can only pad,
    truncate, or do nothing, so nothing is lost by refusing all of them: only a
    bare `{kind}` reproduces the token, and only that keeps two credentials on two
    secrets.
    """
    assert "kind" not in template_tokens(template)
    assert template_tokens(template) == {"profile"}


def test_template_fields_classifies_every_field_and_quotes_it_back():
    """What the error message needs: which field, and written as the user wrote it."""
    assert template_fields("mien-{profile}-{kind:.1s}") == [
        ("profile", True, "{profile}"),
        ("kind", False, "{kind:.1s}"),
    ]
    # A positional field has no name to check, which is why the renderability
    # rule cannot be a set difference over names.
    assert template_fields("mien-{}-{0}") == [
        ("", True, "{}"), ("0", True, "{0}")]
    # Escaped braces are literal text, not a field.
    assert template_fields("mien-{{kind}}") == []


def test_the_builtin_templates_spend_every_token_they_are_rendered_with():
    """The rule's own fixed point: what mien ships must satisfy what mien demands."""
    assert set(REQUIRED_TOKENS["default"]) <= template_tokens(BUILTIN_DEFAULT)
    assert set(REQUIRED_TOKENS["slack_token"]) <= template_tokens(BUILTIN_SLACK_TOKEN)
