from __future__ import annotations

from typing import Protocol

import keyring
from pydantic import BaseModel


class CredentialBackend(Protocol):
    def get(self, service: str, profile: str) -> str | None: ...
    def set(self, service: str, profile: str, secret: str) -> None: ...
    def delete(self, service: str, profile: str) -> None: ...


class KeyringCredentialBackend:
    def get(self, service: str, profile: str) -> str | None:
        return keyring.get_password(service, profile)

    def set(self, service: str, profile: str, secret: str) -> None:
        keyring.set_password(service, profile, secret)

    def delete(self, service: str, profile: str) -> None:
        try:
            keyring.delete_password(service, profile)
        except keyring.errors.PasswordDeleteError:
            pass


class MemoryCredentialBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get(self, service: str, profile: str) -> str | None:
        return self.values.get((service, profile))

    def set(self, service: str, profile: str, secret: str) -> None:
        self.values[(service, profile)] = secret

    def delete(self, service: str, profile: str) -> None:
        self.values.pop((service, profile), None)


class CredentialStatus(BaseModel):
    profile: str
    exists: bool
    backend: str


class CredentialService:
    def __init__(self, backend: CredentialBackend, service_name: str = "coding-agent-harness") -> None:
        self.backend = backend
        self.service_name = service_name

    def set(self, profile: str, secret: str) -> None:
        if not secret.strip():
            raise ValueError("empty_credential")
        self.backend.set(self.service_name, profile, secret)

    def update(self, profile: str, secret: str) -> None:
        self.set(profile, secret)

    def status(self, profile: str) -> CredentialStatus:
        return CredentialStatus(profile=profile, exists=self.backend.get(self.service_name, profile) is not None, backend=type(self.backend).__name__)

    def clear(self, profile: str) -> None:
        self.backend.delete(self.service_name, profile)

    def get_for_client(self, profile: str) -> str:
        secret = self.backend.get(self.service_name, profile)
        if secret is None:
            raise ValueError("credential_not_configured")
        return secret
