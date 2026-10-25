# Project working rules

- Do all work directly in the main session. Do not use subagents under any circumstances.
- Keep the repository private. Commit and push tested checkpoints.
- Discover actual agent schemas before changing adapters; keep sanitized fixtures and provenance.
- Never commit real prompts, transcripts, credentials, runtime databases or local configuration.
- Run `uv run ruff check .`, `uv run mypy --strict src`, `uv run pytest` and the frontend build for relevant changes.
- Do not claim measured performance or live agent compatibility without evidence.
