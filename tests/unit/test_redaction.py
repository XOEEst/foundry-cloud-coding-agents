from foundry_opt.preflight.redaction import redact


def test_redact_masks_sensitive_header_values_without_losing_surrounding_text() -> None:
    text = (
        "request headers: Authorization: Bearer bearer-token, "
        "authorization=Basic dXNlcjpwYXNz; "
        "X-API-Key: header-key and api_key = alternate-key"
    )

    assert redact(text) == (
        "request headers: Authorization: Bearer [REDACTED], "
        "authorization=Basic [REDACTED]; "
        "X-API-Key: [REDACTED] and api_key = [REDACTED]"
    )


def test_redact_masks_sensitive_query_parameters_and_not_similar_words() -> None:
    text = (
        "GET /deploy?SIG=abc&client-secret=secret&api_key=key"
        "&access-token=token&password=pw&region=west "
        "notes: monkey tokenizer code-review keyboard"
    )

    assert redact(text) == (
        "GET /deploy?SIG=[REDACTED]&client-secret=[REDACTED]"
        "&api_key=[REDACTED]&access-token=[REDACTED]"
        "&password=[REDACTED]&region=west "
        "notes: monkey tokenizer code-review keyboard"
    )


def test_redact_preserves_query_fragments_and_message_delimiters() -> None:
    text = "first=?token=secret#details; second=?key=other, complete"

    assert redact(text) == (
        "first=?token=[REDACTED]#details; "
        "second=?key=[REDACTED], complete"
    )


def test_redact_accepts_query_parameter_separator_variants() -> None:
    text = (
        "?client_secret=one&client-secret=two&clientSecret=three"
        "&api_key=four&api-key=five&apiKey=six"
        "&access_token=seven&access-token=eight&accessToken=nine"
    )

    assert redact(text) == (
        "?client_secret=[REDACTED]&client-secret=[REDACTED]"
        "&clientSecret=[REDACTED]&api_key=[REDACTED]"
        "&api-key=[REDACTED]&apiKey=[REDACTED]"
        "&access_token=[REDACTED]&access-token=[REDACTED]"
        "&accessToken=[REDACTED]"
    )


def test_redact_masks_connection_string_values() -> None:
    text = (
        "Endpoint=sb://service/;AccountKey=account-secret;"
        "SharedAccessKey=shared-key;SharedAccessSignature=signature;"
        "Password=p@ssword;ClientSecret=client-secret;Database=orders"
    )

    assert redact(text) == (
        "Endpoint=sb://service/;AccountKey=[REDACTED];"
        "SharedAccessKey=[REDACTED];SharedAccessSignature=[REDACTED];"
        "Password=[REDACTED];ClientSecret=[REDACTED];Database=orders"
    )


def test_redact_masks_quoted_connection_string_values() -> None:
    text = 'Password="two words";ClientSecret=\'semi;colon\';User ID=app'

    assert redact(text) == (
        'Password="[REDACTED]";ClientSecret=\'[REDACTED]\';User ID=app'
    )


def test_redact_masks_connection_values_containing_ampersands() -> None:
    text = "Password=p&ss;SharedAccessSignature=sr=x&sig=y&se=z;Mode=test"

    assert redact(text) == (
        "Password=[REDACTED];SharedAccessSignature=[REDACTED];Mode=test"
    )


def test_redact_masks_multiple_credentials_in_json_text() -> None:
    text = (
        '{"Authorization": "Bearer json-token", "x_api_key": "json-key", '
        '"url": "https://example.test/?code=auth-code&region=west", '
        '"connection": "AccountKey=account-key;Endpoint=https://storage/"}'
    )

    assert redact(text) == (
        '{"Authorization": "Bearer [REDACTED]", '
        '"x_api_key": "[REDACTED]", '
        '"url": "https://example.test/?code=[REDACTED]&region=west", '
        '"connection": "AccountKey=[REDACTED];Endpoint=https://storage/"}'
    )


def test_redact_masks_credentials_in_terminal_output() -> None:
    text = (
        "curl -H 'Authorization: Basic dXNlcjpwYXNz==' "
        "-H 'api-key: terminal-key' "
        "'https://example.test/run?access_token=terminal-token&mode=dry-run'"
    )

    assert redact(text) == (
        "curl -H 'Authorization: Basic [REDACTED]' "
        "-H 'api-key: [REDACTED]' "
        "'https://example.test/run?access_token=[REDACTED]&mode=dry-run'"
    )


def test_redact_masks_literal_provided_secrets_longest_first() -> None:
    text = "failure for literal.*[secret]-long, not literal.*[secret]"

    assert redact(
        text,
        secrets=("literal.*[secret]", "literal.*[secret]-long"),
    ) == "failure for [REDACTED], not [REDACTED]"


def test_redact_does_not_mask_ordinary_security_words() -> None:
    text = (
        "Use basic authorization and a bearer token. "
        "Read the API key, password rotation, and code review guidance."
    )

    assert redact(text) == text


def test_redact_masks_bare_provider_tokens_and_jwts() -> None:
    github_token = "ghp_" + "a" * 36
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signaturevalue"

    assert redact(f"tokens: {github_token} and {jwt}") == (
        "tokens: [REDACTED] and [REDACTED]"
    )


def test_redact_masks_url_userinfo_and_standalone_token_fields() -> None:
    text = (
        "clone https://user:super-secret@example.test/repo.git "
        "then token=another-secret"
    )

    assert redact(text) == (
        "clone https://[REDACTED]@example.test/repo.git "
        "then token=[REDACTED]"
    )


def test_redact_masks_private_key_blocks() -> None:
    text = (
        "key follows\n-----BEGIN PRIVATE KEY-----\n"
        "private-material\n-----END PRIVATE KEY-----\ncomplete"
    )

    assert redact(text) == "key follows\n[REDACTED]\ncomplete"
