import hashlib
import secrets
import time
from collections import deque


class LoginRequired(Exception):
    pass


class LoginRejected(Exception):
    pass


class Sessions:
    def __init__(self):
        self.sessions = {}
        self.attempts = deque(maxlen=8)

    def issue(self):
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        self.sessions = {key: end for key, end in self.sessions.items() if end > now}
        if len(self.sessions) >= 16:
            self.sessions.clear()
        self.sessions[self._digest(token)] = now + 8 * 60 * 60
        return token

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode()).digest()

    def valid(self, token):
        return bool(token) and self.sessions.get(self._digest(token), 0) > time.monotonic()

    def allow_attempt(self):
        now = time.monotonic()
        while self.attempts and self.attempts[0] <= now - 60:
            self.attempts.popleft()
        if len(self.attempts) >= 8:
            return False
        self.attempts.append(now)
        return True

    def clear(self):
        self.sessions.clear()
