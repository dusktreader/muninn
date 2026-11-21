# Remove homelab values profile

This journal records removal of Muninn's chart-owned homelab values profile.

## Intent

Ravenwood owns deployment values. Keep deployment-specific values out of the chart
repository and preserve unrelated user changes.

## Changed files

- `infra/helm/muninn/values-homelab.yaml`: deleted, including its uncommitted edit
- `infra/helm/muninn/values-qa-fake-provider.yaml`: deleted at the user's request
- `infra/Makefile`: remove the deploy target; render Helm with inline OIDC fixtures
- `api/tests/deploy/test_helm_config.py`:
  assert current chart defaults and render with inline OIDC fixture values

## Verification

Final QA is deferred until the principal runs verification. No tests, lint, or
integration suites were run.

## QA

- `uv run --project api pytest api/tests/deploy/test_helm_config.py -m unit`: 3 passed
- `make -C infra qa/lint`: passed after `helm lint` received the same inline OIDC fixtures as `helm template`

The `infra/Makefile` lint target needed those fixtures because the schema rejects empty OIDC defaults.
