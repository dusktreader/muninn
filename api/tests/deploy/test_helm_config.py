"""Protect production Helm configuration and API environment wiring."""

from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).parents[3]
CHART = ROOT / "infra/helm/muninn"


@pytest.mark.unit
def test_production_values_use_postgresql_18_and_non_development_environment() -> None:
    """Keep the homelab render on PostgreSQL 18 and out of the local shortcut profile."""
    values = (CHART / "values-homelab.yaml").read_text()
    defaults = (CHART / "values.yaml").read_text()
    assert "environment: production" in values
    assert "image: postgres:18" in values
    assert "summaryModel: gemma-4-31b" in defaults
    assert "summaryModel: gemma-4-31b" in values


@pytest.mark.unit
def test_api_imports_the_prefixed_service_configuration() -> None:
    """Ensure the API receives the settings names consumed by Pydantic settings."""
    config = (CHART / "templates/config.yaml").read_text()
    deployment = (CHART / "templates/deployment.yaml").read_text()
    topology = (CHART / "templates/topology.yaml").read_text()
    secrets = (CHART / "templates/secrets.yaml").read_text()
    assert "MUNINN_DATABASE_URL" in config
    assert "MUNINN_ENVIRONMENT" in config
    assert "MUNINN_AUTH_MODE" in config
    assert "object-store-bucket" in deployment
    assert "ensure_bucket()" in deployment
    assert "MUNINN_OBJECT_STORE_ACCESS_KEY" in secrets
    assert "MUNINN_ARTIFACT_GRANT_SECRET" in secrets
    assert "MINIO_ROOT_USER" in topology
    assert "configMapRef: {name: muninn-config}" in deployment
    assert "checksum/config" in deployment


@pytest.mark.unit
def test_homelab_render_wires_production_api_configuration() -> None:
    """Render the chart and prove API pods receive production-only settings."""
    rendered = subprocess.run(
        ["helm", "template", "muninn", str(CHART), "-f", str(CHART / "values-homelab.yaml")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    api_document = next(
        document
        for document in rendered.split("---")
        if "kind: Deployment" in document and "name: muninn-api" in document
    )
    manager_document = next(
        document
        for document in rendered.split("---")
        if "kind: Deployment" in document and "name: muninn-job-manager" in document
    )
    assert "MUNINN_ENVIRONMENT: \"production\"" in rendered
    assert "MUNINN_DATABASE_URL: \"postgresql+psycopg://" in rendered
    assert "MUNINN_OIDC_JWKS_URL: \"https://" in rendered
    assert 'MUNINN_ASSEMBLYAI_SUMMARY_MODEL: "gemma-4-31b"' in rendered
    assert "configMapRef: {name: muninn-config}" in api_document
    assert "checksum/config:" in api_document
    assert "secretRef: {name: muninn-runtime}" in api_document
    assert "name: MUNINN_OBJECT_STORE_ENDPOINT" in manager_document
    assert 'value: "http://muninn-minio:9000"' in manager_document
    assert "name: MUNINN_OBJECT_STORE_ACCESS_KEY" in manager_document
    assert "name: MUNINN_OBJECT_STORE_SECRET_KEY" in manager_document
    assert "sqlite" not in api_document.lower()
