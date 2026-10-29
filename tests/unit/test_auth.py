from skillhub import auth


class _FakeConfig:
    def authenticate(self):
        return {"Authorization": "Bearer t0k"}


class _FakeW:
    def __init__(self, **kw):
        self.kw = kw
        self.config = _FakeConfig()


def test_auth_headers_come_from_sdk_config(monkeypatch):
    monkeypatch.setattr(auth, "WorkspaceClient", _FakeW)
    auth._default_client.cache_clear()
    assert auth.auth_headers() == {"Authorization": "Bearer t0k"}


def test_user_client_uses_explicit_pat(monkeypatch):
    monkeypatch.setattr(auth, "WorkspaceClient", _FakeW)
    w = auth.user_workspace_client("https://example.invalid", "user-token")
    assert w.kw == {"host": "https://example.invalid", "token": "user-token", "auth_type": "pat"}
