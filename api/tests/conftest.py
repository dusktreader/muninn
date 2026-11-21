"""Configure test-only OIDC settings before importing the API package."""

import os

os.environ.setdefault("MUNINN_OIDC_ISSUER_URL", "https://keycloak.test/realms/muninn")
os.environ.setdefault("MUNINN_OIDC_JWKS_URL", "https://keycloak.test/realms/muninn/protocol/openid-connect/certs")
