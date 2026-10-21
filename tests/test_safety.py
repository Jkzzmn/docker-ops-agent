from backend.sanitize import redact

def test_redacts_common_secrets():
    value = redact("password=hunter2 API_KEY=abc123 sk-abcdefghijklmnop")
    assert "hunter2" not in value and "abc123" not in value and "sk-abc" not in value

