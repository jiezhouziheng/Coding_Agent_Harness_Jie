import pytest

from coding_agent_harness.credentials import CredentialService, MemoryCredentialBackend


def test_credentials_lifecycle_never_returns_secret_in_status() -> None:
    backend = MemoryCredentialBackend()
    service = CredentialService(backend, service_name="coding-agent-harness")
    service.set("default", "sk-secret")
    status = service.status("default")
    assert status.exists is True
    assert "sk-secret" not in status.model_dump_json()
    service.update("default", "sk-replaced")
    assert service.get_for_client("default") == "sk-replaced"
    service.clear("default")
    assert service.status("default").exists is False


def test_credentials_reject_blank_and_missing_profiles() -> None:
    service = CredentialService(MemoryCredentialBackend())
    with pytest.raises(ValueError, match="empty_credential"):
        service.set("default", "   ")
    with pytest.raises(ValueError, match="credential_not_configured"):
        service.get_for_client("default")
