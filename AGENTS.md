# Agent instructions

Local MCP server for one volunteer's own Planning Center Services data. Python, `uv`, the MCP
SDK's `MCPServer`, `httpx`. See README.md for the tool list and setup.

## Privacy rules (the repo is public)

- Never put real Planning Center data in the repo: no church, campus or people's names, no real
  ids, no recorded API responses, no downloaded files, no screenshots. Tests use synthetic data
  only ("Example Church", ids like `1001`). This also applies to commit messages and PR text.
- Never print, log or commit the Personal Access Token or a pre-signed download URL.
- Keep `.githooks` enabled (`git config core.hooksPath .githooks`); never bypass them with
  `--no-verify`. If the leak check blocks a commit, remove the match; do not edit the denylist
  to make it pass.
- Live checks against a real account belong in the session's scratch space, never in `tests/`.

## Code rules

- Every authenticated request goes through `PcoClient.request`, which enforces the allowlist in
  `client.api_url`: the Services API plus People's `people/<id>/phone_numbers`, nothing else.
  Never add a second authenticated client or bypass it, and widen the allowlist only for a
  specific endpoint, with a test that refuses its neighbours.
- Ids from tool arguments go through `check_id`. Filesystem paths are built only by
  `downloads.confined`; tools never accept a path from the model.
- Errors the model should see are `PcoError` (a `ToolError`); plain exceptions reach the client
  as an opaque failure.
- Plan-scoped tools call `_my_plan` first, so they only work for plans on the user's schedule.
- Contact details are shown only where Planning Center returns them; a 403/404 means "not
  visible to you" and is reported as such, never worked around.
- Each security guard has a test that fails if the guard is removed. Keep it that way.

## Checks

```sh
uv run pytest
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
```
