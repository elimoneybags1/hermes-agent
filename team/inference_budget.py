"""Admission control for subscription inference, shared by a boss and its helpers."""
from __future__ import annotations
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    max_calls: int = 12
    max_input_tokens: int = 120_000
    max_request_bytes: int = 200_000


class BudgetExceeded(ValueError):
    pass


class Budget:
    """A completed call can cross the reported-token threshold; no next call starts.

    Request count and request bytes are hard preflight bounds. Tokens are
    provider-reported after execution, so in-flight calls can overshoot that cap.
    Reset is host-only, once per new user turn, never per helper/retry.
    """
    def __init__(self, limits=Limits()):
        self.limits = limits
        self.lock = threading.Lock()
        self.active = 0
        self.begin()

    def begin(self):
        with self.lock:
            if self.active:
                raise BudgetExceeded('Cannot start a new turn while inference is in flight')
            self.calls = self.tokens = 0

    def admit(self, request_bytes):
        with self.lock:
            if request_bytes > self.limits.max_request_bytes:
                raise BudgetExceeded('Inference request exceeds the byte limit; narrow context before retrying')
            if self.calls >= self.limits.max_calls or self.tokens >= self.limits.max_input_tokens:
                raise BudgetExceeded('Inference budget exhausted for this user turn; stop and report partial progress')
            self.calls += 1
            self.active += 1

    def finish(self, tokens=0):
        with self.lock:
            self.tokens += tokens
            self.active -= 1
