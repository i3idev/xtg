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

The repository is at the end of **Phase 2**:

| Area | State |
| --- | --- |
| CLI framework, global options, help, exit codes | done |
| Configuration (`.env` + `XTG_*` environment) | done |
| Logging (stderr + rotating file) | done |
| SQLite schema (7 tables, SQLAlchemy 2.0) | done |
| Proxy subsystem (load, parse, check, rotate) | done |
| Telethon client, proxy bridge, connection lifecycle | done |
| `xtg auth login/logout/status` (phone, code, 2FA) | done |
| `xtg collect` (messages, members, media metadata) | done |
| `status`, `db`, `proxy`, `info`, `search` | working against the local dataset |
| `export`, `analyze graph`, media downloads | declared, implemented in Phase 3 |

Not-yet-implemented commands answer with a clear message and exit code `9`
instead of pretending to work.

---

## Installation

The project lives in the `projects` uv workspace (`../pyproject.toml` declares
`telegram-osint` as a member), so `uv` manages the environment for you:

```bash
cd telegram-osint
uv sync                # resolve, create the lock file and install dependencies
uv run xtg --help      # or: uv run python -m xtg --help
```

`uv sync` needs network access at least once: it creates `uv.lock` and installs
`telethon`. Without a network the CLI still works for every command that does
not open a Telegram connection, because Telethon is imported lazily
(`xtg.telegram.client.create_client`); such an install reports a clear
configuration error and exit code `3` on the first `collect`/`auth login`.

### uv workflow

| Command | What it does |
| --- | --- |
| `uv sync` | Install the project and dev group, create/update `uv.lock` |
| `uv sync --frozen` | Install exactly what `uv.lock` pins, never re-resolve |
| `uv add "telethon>=1.34"` | Add a runtime dependency and update the lock file |
| `uv add --dev pyright` | Add a development dependency |
| `uv run xtg status` | Run a command inside the project environment |
| `uv run --no-sync pytest` | Run a tool without touching the lock/sync step |
| `uv lock` | Re-resolve dependencies only |
| `uv run python -c "import xtg"` | Drop into the same interpreter the CLI uses |

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
| `XTG_SESSION_DIR` | `data/sessions` | Session directory, relative to `XTG_HOME` |
| `XTG_EXPORT_DIR` | `data/exports` | Export directory, relative to `XTG_HOME` |
| `XTG_LOG_DIR` | `data/logs` | Log directory, relative to `XTG_HOME` |
| `XTG_PROXY_ENABLED` | `true` | Route connections through the proxy list |
| `XTG_PROXY_FILE` | bundled list | Alternative proxy list file |
| `XTG_LOG_LEVEL` | `INFO` | Default log level |
| `XTG_LOG_TO_FILE` | `true` | Also write `data/logs/xtg.log` |
| `XTG_REQUEST_DELAY` | `0.5` | Delay between API calls (flood protection) |
| `XTG_MAX_RETRIES` | `3` | Retries for transient failures |
| `XTG_CONNECT_TIMEOUT` | `10.0` | Socket timeout in seconds |

`.env` is git-ignored; secrets never reach the logs or `xtg status` output.

---

## Telegram setup

1. **Get API credentials.** Sign in at <https://my.telegram.org> and open
   *API development tools*. Create an application; you get an `api_id` (a
   number) and an `api_hash` (32 hex characters).
2. **Write them down.**

   ```bash
   cp .env.example .env
   $EDITOR .env      # XTG_API_ID=1234567, XTG_API_HASH=0123456789abcdef...
   ```

3. **Log in.**

   ```bash
   uv run xtg auth login
   ```

   The flow asks for the phone number (international format), the login code
   Telegram sends to your client, and - only when two-factor authentication is
   enabled - the cloud password. The password is used once and never written to
   disk. The session is stored as `<home>/data/sessions/<session name>` and is
   tightened to owner-only permissions (`chmod 600`) right after the login.

   The session file *is* the account: anyone who can read it can use it. Keep
   `data/` private and out of backups you do not control.

4. **Verify.**

   ```bash
   uv run xtg auth status      # state, session file, size, permissions
   ```

   `xtg status` shows the same authentication summary next to the database and
   proxy state.

`xtg auth login --phone <number> --code <code>` skips the prompts for scripted
use; `--password` exists but is discouraged because the value lands in the shell
history. `xtg auth logout` deletes every session file (including the SQLite
`-journal` companion) and asks for confirmation.

If `XTG_PROXY_ENABLED=true`, the connection is routed through the selected proxy
from the bundled list before the MTProto handshake, so both Telegram and proxy
problems surface as exit code `6` with the proxy in the error hint.

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
xtg auth login         # interactive MTProto login (phone, code, 2FA password)
xtg auth logout        # delete the local session files
```

`--json` is available on `status` and is the scripting entry point
(`session_exists`, `session_size_bytes`, `session_mode`, `state`).

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

### `xtg collect`

```bash
xtg collect @channel --messages                 # message history
xtg collect @channel --members --limit 500      # participants and their roles
xtg collect @channel --media                    # media metadata only
xtg collect https://t.me/channel --all           # everything available
xtg collect -1001234567890 --messages --json    # numeric id, machine readable
```

| Option | Meaning |
| --- | --- |
| `--messages` | Walk the message history |
| `--members` | Walk the participant list and store the role (admin/member/banned) |
| `--media` | Store media metadata (type, file name, size, mime type) |
| `--all` | All of the above |
| `-n, --limit` | Maximum items per section (default `500`) |
| `--download` | Not available yet: warns and stores metadata instead (Phase 3) |
| `--json` | Summary as JSON instead of a table |

How it behaves:

* the reference (`@username`, numeric id, `t.me` link) is validated before any
  connection is opened, so a typo fails fast with exit code `2`;
* every run starts at the newest message and pages backwards, so new messages
  are never missed;
* writes go through the idempotent upsert helpers, so an interrupted or
  repeated run never duplicates rows - re-running reports the messages it
  skipped;
* messages are committed in batches, so a crash keeps what was collected;
* `XTG_REQUEST_DELAY` seconds are waited between API pages (flood protection);
* media is metadata only, `media.path` stays `NULL` until Phase 3 downloads
  files.

### `xtg analyze` / `xtg export` (Phase 3)

`xtg analyze stats`, `xtg analyze entities email`, `xtg analyze graph` and
`xtg export json|csv|markdown|obsidian` are declared; the writers and the
graph builder arrive with the next phases. `xtg export --formats` shows which
formats are implemented.

---

## Example usage

A complete session, from an empty installation to a searchable dataset:

```bash
uv sync                                  # dependencies + lock file
cp .env.example .env                      # fill XTG_API_ID / XTG_API_HASH
uv run xtg status                         # paths, database, proxy, auth state

uv run xtg auth login                     # phone -> code -> (2FA) password
uv run xtg auth status                    # state: authorized

uv run xtg db init                        # create data/database.db
uv run xtg collect @telegram --all -n 2000
uv run xtg db stats --json | jq .tables

uv run xtg search "crypto wallet" --chat @telegram --limit 50
uv run xtg info chat @telegram
```

Collecting from a second chat keeps the earlier data untouched:

```bash
uv run xtg collect https://t.me/another_channel --messages --json
# {"chat": "another_channel", "messages": 480, "duplicated": 0, ...}
```

Scripting pattern - every command returns a documented exit code:

```bash
uv run xtg collect @channel --messages --json > report.json || case $? in
    2) echo "bad reference" ;;
    5) echo "run 'xtg auth login' first" ;;
    6) echo "network or proxy problem" ;;
    8) echo "channel does not exist" ;;
    10) echo "collection failed" ;;
esac
```

Working with a proxy list of your own:

```bash
uv run xtg proxy check P1 P2 --json        # TCP reachability
XTG_PROXY_ENABLED=false uv run xtg collect @channel --messages   # direct connection
```

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
| 10 | collection run failed |
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
├── telegram/       MTProto boundary (client, auth, collector, serializers)
└── export/         export format registry and writer contract
```

Design rules that the code follows:

* **Layering.** `core` knows nothing about Telegram, `proxy` knows nothing
  about Telegram. The only bridge is `xtg/telegram/client.py`, which converts a
  `ProxyEntry` into Telethon's connection arguments.
* **Lazy subsystems.** A command only builds what it needs (`status` never
  opens a Telegram connection), and the database engine is always disposed.
* **Lazy Telethon import.** `xtg.telegram` imports without Telethon installed;
  only `create_client()` needs it, so the missing dependency is reported at the
  exact command that needs it.
* **Idempotent writes.** `upsert_*` helpers plus unique constraints make
  re-running a collection safe.
* **Restart-safe collection.** Paging starts at the newest message, batches are
  committed as they arrive, and duplicates are counted instead of stored.
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
uv sync                       # once, with network
uv run pytest                 # 193 tests
uv run ruff check
uv run ruff format --check
uv run pyright                # type checking (see below)
```

The test suite is isolated (`XTG_HOME` points at a temporary directory and the
cached settings are reloaded per test), so it never touches the real `data/`
directory. Every Telegram test runs against the fakes in `tests/fakes.py`, so
**no test needs the network or a Telegram account**.

### Type checking

[Pyright](https://microsoft.github.io/pyright/) is configured in
`pyproject.toml` (`[tool.pyright]`, `typeCheckingMode = "basic"`) and is a dev
dependency. It was chosen over mypy because the code uses PEP 695 syntax
(`def handle_errors[**P, R]`) and because the Telethon boundary is deliberately
duck-typed: `basic` mode reports real mistakes without forcing annotations on
protocol objects that are typed as `Any` on purpose. The configuration only
establishes the foundation - the codebase is not yet free of pyright findings,
so treat the first `uv run pyright` after `uv sync` as a baseline to work down
from, not a gate.

### Dependency status

| Dependency | Kind | State |
| --- | --- | --- |
| `telethon>=1.34` | runtime | declared, **not installed in the offline sandbox** |
| `pyright>=1.1.380` | dev | declared, **not installed in the offline sandbox** |
| `pytest`, `pytest-asyncio`, `ruff` | dev | installed |
| `loguru`, `pydantic`, `pydantic-settings`, `python-dotenv`, `rich`, `sqlalchemy`, `typer` | runtime | installed |

There are no optional extras (`[project.optional-dependencies]`): every feature
group only needs the runtime set above. Telethon cannot be installed in the
current sandbox because DNS resolution fails
(`Failed to fetch https://pypi.org/... Temporary failure in name resolution`),
and no `uv.lock` is committed for the same reason. Run `uv sync` once with
network access to materialise both; until then the offline commands, the whole
test suite and the type checker configuration remain usable.

### Git conventions

One feature or one fix per commit:

```text
feat: add proxy manager
fix: improve database initialization
```

---

## Roadmap

1. **Phase 2 - Telegram layer:** done (client, `auth login`, message / member /
   media-metadata collection).
2. **Phase 2.5 - Stabilisation:** done (split commits, dependency review,
   pyright foundation, documentation).
3. **Phase 3 - Reddit connector:** authenticated Reddit ingestion reusing the
   collector and upsert helpers.
4. **Phase 4 - Analysis:** entity extraction (emails, domains, phones,
   hashtags), knowledge graph builder, JSON/CSV/Markdown/Obsidian writers.
5. **Phase 5 - Intelligence:** search engine (FTS5), cross-source correlation,
   timeline reports, plugin architecture.

---

## Legal

This toolkit is intended for security research and OSINT work on data you are
authorised to access. Respect Telegram's terms of service and applicable law.

## License

See the repository for licensing information.