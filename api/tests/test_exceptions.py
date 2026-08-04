"""Test stable project error translation at the HTTP boundary."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from muninn.auth.exceptions import AuthError, AuthenticationRequiredError, CapabilityDeniedError, CredentialError
from muninn.api.exceptions import RecordingUploadError
from muninn.exceptions import MuninnError, ResourceNotFoundError, install_exception_handlers
from muninn.job_manager import CompletionRejectedError, JobManagerError
from muninn.mcp.exceptions import DelegatedSessionRequiredError, McpError, McpOffsetOutOfRangeError
from muninn.queue.exceptions import ObjectStoreError, QueueError
from muninn.worker.exceptions import ProcessingError, WorkerError


def error_client(error: Exception) -> TestClient:
    """Build a client whose route raises the supplied project error."""
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/error")
    def raise_error() -> None:
        raise error

    return TestClient(app)


@pytest.mark.unit
def test_muninn_error_handler_preserves_request_id_and_safe_message() -> None:
    """Return the stable envelope without exposing the exception's internal text."""
    response = error_client(ResourceNotFoundError("secret database details")).get(
        "/error", headers={"X-Request-ID": "00000000-0000-0000-0000-000000000123"}
    )

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == "00000000-0000-0000-0000-000000000123"
    assert response.json() == {
        "error": {
            "kind": "resource_not_found",
            "message": "Resource not found",
            "request_id": "00000000-0000-0000-0000-000000000123",
        }
    }
    assert "secret" not in response.text


@pytest.mark.unit
def test_package_exceptions_have_package_roots() -> None:
    """Keep package failures distinguishable while retaining the common project base."""
    assert issubclass(AuthError, MuninnError)
    assert issubclass(CredentialError, AuthError)
    assert issubclass(AuthenticationRequiredError, AuthError)
    assert issubclass(CapabilityDeniedError, AuthError)
    assert issubclass(McpError, MuninnError)
    assert issubclass(McpOffsetOutOfRangeError, McpError)
    assert issubclass(DelegatedSessionRequiredError, McpError)
    assert issubclass(QueueError, MuninnError)
    assert issubclass(ObjectStoreError, QueueError)
    assert issubclass(WorkerError, MuninnError)
    assert issubclass(ProcessingError, WorkerError)
    assert issubclass(JobManagerError, MuninnError)
    assert issubclass(CompletionRejectedError, JobManagerError)


@pytest.mark.unit
def test_muninn_error_handler_maps_authentication_and_mcp_errors() -> None:
    """Expose each API error's stable status, kind, and authentication challenge."""
    cases = [
        (AuthenticationRequiredError("missing"), 401, "authentication_required"),
        (CredentialError("credential_expired"), 401, "credential_expired"),
        (CapabilityDeniedError("private"), 403, "capability_denied"),
        (McpOffsetOutOfRangeError("bad offset"), 422, "offset_out_of_range"),
        (DelegatedSessionRequiredError("missing session"), 401, "delegated_session_required"),
    ]

    for error, status_code, kind in cases:
        response = error_client(error).get("/error")
        assert response.status_code == status_code
        assert response.json()["error"]["kind"] == kind

    assert error_client(AuthenticationRequiredError("missing")).get("/error").headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.unit
def test_recording_upload_error_has_stable_unavailable_response() -> None:
    """Expose unexpected upload failures without leaking their internal cause."""
    response = error_client(RecordingUploadError("database details")).get("/error")

    assert response.status_code == 503
    assert response.json()["error"]["kind"] == "request_failed"
    assert response.json()["error"]["message"] == "Recording upload failed"
    assert "database details" not in response.text
