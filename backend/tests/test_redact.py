from patchproof.redact import redact


def test_redacts_known_secret_shapes():
    text = ("key=bc_live_abcdef123456 Authorization: Bearer tok_123456789 api_key: 'qwertyuiop' "
            "ghp_abcdefghijklmnopqrstuvwxyz12 sk-abcdefghijklmnopqrstu")
    out = redact(text)
    for secret in ("bc_live_abcdef123456", "tok_123456789", "qwertyuiop", "ghp_abcdefghij", "sk-abcdefghij"):
        assert secret not in out
    assert out.count("[REDACTED]") == 5


def test_redacts_explicit_and_env_secrets(monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", "plainsecretvalue")
    assert redact("x plainsecretvalue y") == "x [REDACTED] y"
    assert redact("abc mytoken99 def", ("mytoken99",)) == "abc [REDACTED] def"
    assert redact("") == "" and redact(None) == ""
