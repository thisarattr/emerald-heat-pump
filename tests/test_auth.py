from app.auth import Sessions


def test_session_expiry_and_hash_only_storage(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("app.auth.time.monotonic", lambda: now[0])
    sessions = Sessions()
    token = sessions.issue()
    assert sessions.valid(token)
    assert not sessions.valid("made-up-cookie")
    assert token not in sessions.sessions
    now[0] += 8 * 60 * 60 + 1
    assert not sessions.valid(token)


def test_attempt_limit_resets_after_one_minute(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("app.auth.time.monotonic", lambda: now[0])
    sessions = Sessions()
    assert all(sessions.allow_attempt() for _ in range(8))
    assert not sessions.allow_attempt()
    now[0] += 60
    assert sessions.allow_attempt()
