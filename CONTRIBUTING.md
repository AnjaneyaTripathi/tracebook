# Contributing to Tracebook

Thanks for helping. Tracebook is local-first and privacy-sensitive, so changes are
reviewed with that in mind.

## Set up

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and Node.js for the
dashboard. See the [development section of the README](README.md#development-and-validation).

```sh
uv sync --locked
cd frontend && npm ci
```

## Before you open a pull request

Run the same checks CI runs:

```sh
uv run ruff check .
uv run ruff format --check src tests scripts
uv run mypy --strict src
uv run pytest --cov=tracebook --cov-fail-under=85
cd frontend && npm run build && npm test
```

If you change the dashboard, commit the rebuilt bundle in `src/tracebook/static/`
from `npm run build`. CI does not rebuild it for you.

## Rules for data and fixtures

- Never commit real prompts, transcripts, credentials, runtime databases or local
  configuration. Test data must be synthetic or sanitized.
- Adapter fixtures go under `tests/fixtures/<agent>/<version>/`, with a
  `provenance.json` that says where the schema came from and how the content was
  replaced. Mark unverified schemas as `unverified`.
- Redaction changes need a test that proves the secret is absent from both the raw
  and canonical stores.

## Adapters

Adding or changing an agent adapter starts with [schema discovery](docs/SCHEMA_DISCOVERY.md)
and follows [the adapter guide](docs/WRITING_AN_ADAPTER.md). Do not claim live
compatibility or measured performance without evidence, and say so in the docs.

## Pull requests

- Keep each change focused, and explain why in the description.
- Add or update tests for behavior changes.
- Update the docs when user-visible behavior, privacy behavior or defaults change.

## Security

Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).

## License

Tracebook is licensed under the [Apache License 2.0](LICENSE). Unless you state
otherwise, contributions you submit are licensed under the same terms.
