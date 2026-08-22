# Agent Instructions

The repository keeps provider-neutral finalization scripts in `scripts/hooks/`.
Do not create separate copies under an individual agent's configuration
directory.

- Before development, create a clean environment with
  `bash scripts/rebuild-env.sh`.
- After changing Python, run `bash scripts/hooks/finalize-python.sh`.
- After changing Markdown, run `bash scripts/hooks/finalize-markdown.sh`.
- Keep the committed dependency source for `hvbrowser` on PyPI. Install local
  editable checkouts manually, in dependency order, only after rebuilding the
  environment.
- Automated tests must remain offline and must never start or flee a battle.

## Git Workflow

- Do not create or switch to a development branch.
- Perform all development work directly on the repository's primary branch
  (`main`).
