# pco-services-mcp

A local [MCP](https://modelcontextprotocol.io) server that lets Claude (Claude Code, Claude
Desktop, or any MCP client) work with **your own** Planning Center Services schedule as a
volunteer. It's for churches that haven't turned on Planning Center's hosted MCP connector,
and it covers the one thing that connector can't do: managing your blockout dates.

It signs in with your own Personal Access Token, so it sees exactly what you can see in
Planning Center and nothing more.

## What it can do

| Tool | Does |
|---|---|
| `my_schedule` | When you're next serving: date, service, team, position, and whether you've accepted |
| `plan_team` | Who else is scheduled on that plan, grouped by team |
| `team_contacts` | Email addresses and phone numbers for the people on that plan |
| `contact_info` | Someone's email and phone, found by name among the people on your upcoming plans |
| `service_order` | The order of service: songs, keys, arrangements, lengths |
| `list_attachments` | Charts, lead sheets and audio attached to the plan's songs |
| `download_attachments` | Saves those files to your download folder (PDFs only with `file_types: ["pdf"]`) |
| `list_blockouts` | Your blockout dates |
| `add_blockout` | Block out a day, a date range or a time, once or repeating (daily/weekly/monthly/yearly, every 1–8, "2nd Sunday" style monthly) |
| `remove_blockout` | Delete a blockout (a repeating one is deleted as a whole series) |
| `skip_blockout_date` | Make yourself available on one date of a repeating blockout |

The plan tools only work for plans **you're scheduled on**. `plan_team` shows people by name,
team, position and status only. The two contact tools show email addresses and phone numbers,
and only where Planning Center already lets you see them (usually the people on your own
teams); for anyone else they say the details aren't visible, rather than working around it.

## Set up

Needs [uv](https://docs.astral.sh/uv/) and Python 3.10+.

```sh
git clone https://github.com/<you>/pco-services-mcp
cd pco-services-mcp
uv sync
```

1. Create a Personal Access Token at <https://api.planningcenteronline.com/oauth/applications>.
2. Run `uv run pco-services-mcp login` in a terminal and paste the Application ID and Secret
   when asked (they aren't shown). They're checked with Planning Center, then stored in your
   operating system's credential store (Windows Credential Manager, macOS Keychain, Secret
   Service on Linux). They are never written to a file.
3. `uv run pco-services-mcp status` confirms the token works; `logout` removes it.

### Claude Code

```sh
claude mcp add --scope user pco-services -- uv --directory /path/to/pco-services-mcp run pco-services-mcp
```

### Claude Desktop

Add to `claude_desktop_config.json` (on Windows, use the full path to `uv.exe`):

```json
{
  "mcpServers": {
    "pco-services": {
      "command": "uv",
      "args": ["--directory", "/path/to/pco-services-mcp", "run", "pco-services-mcp"]
    }
  }
}
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PCO_DOWNLOAD_DIR` | `~/Documents/Planning Center` | Where attachments are saved, one folder per plan |
| `PCO_TIME_ZONE` | your church's time zone in Planning Center | IANA zone (e.g. `America/Chicago`) used for whole-day blockouts |
| `PCO_APPLICATION_ID`, `PCO_SECRET` | unset | Use instead of the credential store; set both or neither |

## How it keeps your data and token safe

- The token is only ever sent to `https://api.planningcenteronline.com/services/v2/…`, plus
  one People endpoint, `/people/v2/people/<id>/phone_numbers` (Services has no phone numbers).
  Every URL, including paging links in Planning Center's own responses, is checked first.
- Files are fetched from Planning Center's storage host by a separate client that carries no
  credentials. Download links never appear in errors or logs.
- Downloads only go under the download folder; file names come from Planning Center and are
  cleaned before use. A small `.pco-attachments.json` in each plan folder records what this
  tool wrote, so it never overwrites a file it didn't create.
- Whole-day blockouts run 00:00:00–23:59:59 in the church's time zone, exactly like ones made
  in Planning Center. If that zone can't be determined, it refuses rather than guessing.

## Development

```sh
uv run pytest
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
```

All tests use synthetic data; there are no recorded Planning Center responses in this repo.

### Keeping your church private

This repo is designed to be published without revealing which church its maintainer attends.

1. `git config core.hooksPath .githooks` turns on the `pre-commit` and `commit-msg` hooks.
2. Create `.leakcheck-denylist` (gitignored) with one identifying term per line: church name,
   city, campus names, website domain, address, your Planning Center ids, people's full names.
   The hooks refuse to run without it.
3. The hooks block any commit whose paths, added lines or message contain a denylisted term,
   your current token (raw or Base64-encoded), or something shaped like a secret.
4. Before publishing, run `uv run python scripts/leak_check.py --history`, which checks every
   commit, message and branch.

The check can't find a term you never listed, or an old token that matches no known shape.
Removing something in a later commit doesn't remove it from history; rewrite history before
publishing instead.

## Licence

MIT
