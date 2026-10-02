# eXtractorT

**eXtractorT** - Telegram Intelligence & Extraction Toolkit

A CLI-first toolkit to collect, search, analyse and export Telegram data.
No GUI, no daemon: everything is driven from the terminal, the way `git`,
`curl` or `nmap` are.

```
xtg <command> [arguments] [options]
```

---

## Status

This repository is the **Phase 1 foundation**:

| Area | State |
| --- | --- |
| CLI framework, global options, help, exit codes | done |
| Configuration (`.env` + `XTG_*` environment) | done |
| Logging (stderr + rotating file) | done |
| SQLite schema (7 tables, SQLAlchemy 2.0) | done |
| Proxy subsystem (load, parse, check, rotate) | done |
| `status`, `db`, `proxy`, `info`, `search` | working against the local dataset |
| `auth login`, `collect`, `export`, `analyze graph` | declared, implemented in Phase 2/3 |

Not-yet-implemented commands answer with a clear message and exit code `9`
instead of pretending to work.

---

## Installation

The project lives in the `projects` uv workspace (`../pyproject.toml` declares
`telegram-osint` as a member), so `uv` manages the environment for you:

```bash
cd telegram-osint
uv sync                # install dependencies into the workspace environment
uv run xtg --help      # or: uv run python -m xtg --help
```

Phase 2 needs the MTProto client. It is not declared yet so that Phase 1 stays
installable in restricted environments:

```bash
uv add "telethon>=1.34"
```

---

## Configuration

Configuration comes from real environment variables, then the `.env` file in
the project root, then the defaults. Copy the template and fill it in:

```bash
cp .env.example .env
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `XTG_API_ID` | - | Telegram API id from <https://my.telegram.org> |
| `XTG_API_HASH` | - | Telegram API hash (never printed in clear text) |
| `XTG_HOME` | project root | Overrides where `data/` and `.env` live |
| `XTG_SESSION_NAME` | `xtg` | Telethon session name |
| `XTG_DATABASE_PATH` | `data/database.db` | SQLite file, relative to `XTG_HOME` |
| `XTG_PROXY_ENABLED` | `true` | Route connections through the proxy list |
| `XTG_PROXY_FILE` | bundled list | Alternative proxy list file |
| `XTG_LOG_LEVEL` | `INFO` | Default log level |
| `XTG_LOG_TO_FILE` | `true` | Also write `data/logs/xtg.log` |
| `XTG_REQUEST_DELAY` | `0.5` | Delay between API calls (flood protection) |
| `XTG_MAX_RETRIES` | `3` | Retries for transient failures |
| `XTG_CONNECT_TIMEOUT` | `10.0` | Socket timeout in seconds |

`.env` is git-ignored; secrets never reach the logs or `xtg status` output.

---

## Commands

### Global options

```
-v, --verbose / -vv   increase log verbosity
-q, --quiet           only warnings
--no-color            disable coloured output
-V, --version         print the version and exit
```

### `xtg status`

One screen with version, Python, storage paths, database size and row counts,
authentication state and proxy state. `--json` for scripting.

### `xtg auth`

```bash
xtg auth status        # credentials + local session state
xtg auth login         # Phase 2: interactive MTProto login
xtg auth logout        # delete the local session files
```

### `xtg proxy`

```bash
xtg proxy list                 # table of the parsed list
xtg proxy check                # TCP availability check of every proxy
xtg proxy check P1 P2          # only selected proxies
xtg proxy status               # configuration + health counters
xtg proxy show P3              # detail view (password masked, use --reveal)
```

The list file is `xtg/proxy/list_proxy.json`:

```json
{
  "proxys": {
    "P1": "socks5://host:port",
    "P2": "socks5://user:pass@host:port"
  }
}
```

Accepted entry formats: `socks5://host:port`, `http://host:port`,
`host:port:user:pass` and `host:port`. Entries without a scheme are treated as
SOCKS5. Invalid entries are reported by `xtg proxy list` instead of aborting
the run.

### `xtg db`

```bash
xtg db init      # create the file and the schema (idempotent)
xtg db stats     # row counts per table and file size
xtg db path      # print the database path
xtg db vacuum    # reclaim disk space
xtg db reset     # destructive: backup, drop and recreate
```

### `xtg search`

```bash
xtg search "crypto wallet"
xtg search "bitcoin" --chat @channel --limit 100 --json
```

### `xtg info`

```bash
xtg info chat @channel
xtg info user @durov
xtg info stats
```

### `xtg collect` (Phase 2)

```bash
xtg collect @channel --messages
xtg collect @channel --members
xtg collect @channel --all --limit 1000 --download
```

The reference is validated immediately (`@username`, numeric id or `t.me`
link), so a typo fails fast with exit code `2`.

### `xtg analyze` / `xtg export` (Phase 3)

`xtg analyze stats`, `xtg analyze entities email`, `xtg analyze graph` and
`xtg export json|csv|markdown|obsidian` are declared; the writers and the
graph builder arrive with the next phases. `xtg export --formats` shows which
formats are implemented.

---

## Exit codes

`xtg` is meant to be used inside scripts, so failures are predictable:

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | generic error |
| 2 | usage error (bad arguments) |
| 3 | configuration error |
| 4 | database error |
| 5 | authentication error |
| 6 | network error |
| 7 | proxy error |
| 8 | requested entity not found |
| 9 | feature not available in this phase |
| 130 | cancelled by the user |

```bash
xtg status --json || echo "xtg is not healthy"
```

Data goes to **stdout**, logs and errors go to **stderr**, so
`xtg proxy list --json | jq .` always works.

---

## Architecture

```
xtg/
├── cli/            Typer application, output helpers, one module per command
│   ├── main.py     app assembly, global options, error boundary
│   ├── output.py   Rich tables, panels, JSON, error rendering
│   ├── context.py  lazy per-invocation state (settings, db, proxies)
│   └── commands/
├── core/           config, logging, errors/exit codes, small utilities
├── database/       SQLAlchemy models, engine management, query helpers
├── proxy/          independent proxy subsystem (parser/loader/checker/manager)
├── telegram/       MTProto boundary (client, auth state, collector contract)
└── export/         export format registry and writer contract
```

Design rules that the code follows:

* **Layering.** `core` knows nothing about Telegram, `proxy` knows nothing
  about Telegram. The only bridge is `xtg/telegram/client.py`, which converts a
  `ProxyEntry` into Telethon's connection arguments.
* **Lazy subsystems.** A command only builds what it needs (`status` never
  opens a Telegram connection), and the database engine is always disposed.
* **Idempotent writes.** `upsert_*` helpers plus unique constraints make
  re-running a collection safe.
* **Graph ready.** `entities` and `relations` use stable string node references
  (`chat:@channel`, `entity:email:a@b.c`) so users, chats, messages and pivots
  can be linked in a single table.
* **No silent data loss.** Every entity row keeps the original Telegram payload
  in `raw_data`.

### Database schema

| Table | Purpose |
| --- | --- |
| `users` | Telegram users and bots |
| `chats` | Channels, groups, supergroups, dialogs |
| `memberships` | User ↔ chat links with a role |
| `messages` | Messages with date, sender and media flag |
| `media` | Media attachments of a message |
| `entities` | Extracted pivots (email, domain, phone, hashtag, ...) |
| `relations` | Knowledge graph edges between node references |

---

## Development

```bash
uv run pytest           # 111 tests
uv run ruff check
uv run ruff format
```

The test suite is isolated (`XTG_HOME` points at a temporary directory), so it
never touches the real `data/` directory.

### Git conventions

One feature or one fix per commit:

```text
feat: add proxy manager
fix: improve database initialization
```

---

## Roadmap

1. **Phase 2 - Telegram layer:** Telethon client, `xtg auth login`, message /
   member / media collection, rate-limit aware downloads.
2. **Phase 3 - Analysis:** entity extraction (emails, domains, phones,
   hashtags), knowledge graph builder, JSON/CSV/Markdown/Obsidian writers.
3. **Phase 4 - Intelligence:** search engine (FTS5), cross-chat correlation,
   timeline reports, plugin architecture.

---

## Legal

This toolkit is intended for security research and OSINT work on data you are
authorised to access. Respect Telegram's terms of service and applicable law.

## License

See the repository for licensing information.