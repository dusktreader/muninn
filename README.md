# Muninn

Muninn is a self-hosted, AI assisted meeting note taker that records, transcribes, and summarizes meetings for you.


## Monorepo Components

- `api/`: FastAPI service, workers, migrations, Python packaging, and tests
- `mobile-ui/`: Android application and tests
- `web-ui/`: React/Vite application and browser tests
- `docs/`: Zensical documentation project
- `infra/`: Helm charts and operational scripts


## Run locally

Requires Python 3.13 and `uv`.

```shell
make dev/up
```
