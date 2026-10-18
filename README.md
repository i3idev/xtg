# eXtractorT

**eXtractorT** - Social Media Intelligence & Extraction Toolkit

A CLI-first toolkit to collect, search, analyse and export data from Telegram
and Reddit. No GUI, no daemon: everything is driven from the terminal, the way
`git`, `curl` or `nmap` are.

```
xtg <command> [arguments] [options]
```

---

## Status

The repository is at the end of **Phase 3**:

| Area | State |
| --- | --- |
| CLI framework, global options, help, exit codes | done |
| Configuration (`.env` + `XTG_*` environment) | done |
| Logging (stderr + rotating file) | done |
| SQLite schema (11 tables, SQLAlchemy 2.0) | done |
| Proxy subsystem (load, parse, check, rotate) | done |
| Global proxy pool (sing-box backed, subscription sources) | done |
| Telethon client, proxy bridge, connection lifecycle | done |
| `xtg auth login/logout/status` (phone, code, 2FA) | done |
| `xtg collect` (messages, members, media metadata) | done |
| `xtg info messages TARGET` (discussion graph, export edges) | done |
| Export (JSON/CSV/Markdown/Obsidian) | done |
| Source layer (`BaseSource` / `BaseCollector`) | done |
| `xtg reddit status/collect` (PRAW, posts, comments) | done |
| `status`, `db`, `proxy`, `info`, `search` | working against the local dataset |
| `analyze graph`, media downloads | declared, implemented in Phase 4 |

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
`telethon` and `praw`. Without a network the CLI still works for every command
that does not open a platform connection, because both clients are imported
lazily (`xtg.telegram.client.create_client`, `xtg.sources.reddit.client.create_reddit`);
such an install reports a clear configuration error and exit code `3` on the
first `collect`, `auth login` or `reddit collect`.

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
| `REDDIT_CLIENT_ID` | - | Reddit application client id |
| `REDDIT_CLIENT_SECRET` | - | Reddit application client secret |
| `REDDIT_USER_AGENT` | - | Descriptive user agent Reddit requires |
| `REDDIT_USERNAME` | - | Optional account name (with the password) |
| `REDDIT_PASSWORD` | - | Optional account password (with the name) |
| `REDDIT_LISTING` | `new` | Default subreddit listing |

Reddit variables are accepted with or without the `XTG_` prefix, because
PRAW's own documentation uses the bare names:
`REDDIT_CLIENT_ID` and `XTG_REDDIT_CLIENT_ID` are equivalent.

`.env` is git-ignored; secrets never reach the logs or `xtg status` output.

---

## Reddit setup

1. **Create a script application.** Open <https://www.reddit.com/prefs/apps>,
   choose *create another app...*, pick the **script** type. Reddit shows a
   client id (under the app name) and a client secret.
2. **Write a descriptive user agent.** Reddit throttles generic agents, so use
   something that names the tool and the operator:

   ```bash
   REDDIT_USER_AGENT=xtg/0.1.0 by u/your-reddit-name
   ```

3. **Add the values to `.env`.**

   ```bash
   REDDIT_CLIENT_ID=your-client-id
   REDDIT_CLIENT_SECRET=your-client-secret
   REDDIT_USER_AGENT=xtg/0.1.0 by u/your-reddit-name
   ```

   `REDDIT_USERNAME` / `REDDIT_PASSWORD` are optional and only needed for
   endpoints that require an account; set both or neither. They are never
   logged and never shown by `xtg reddit status`.

4. **Verify and collect.**

   ```bash
   uv run xtg reddit status
   uv run xtg reddit collect linux --posts
   ```

PRAW also works without an account (read-only, "installed" app type). The
collector only reads public data: listings, submission metadata and comments.
**No media is downloaded** - Reddit previews and images are deliberately left
alone; only the link target and its domain are stored, which is the useful part
for OSINT.

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

`xtg proxy` has two subsystems. The **global pool** is the single source of
truth for every network consumer; the **legacy per-command list** still backs
`xtg collect` / `xtg auth` / `xtg forward` when the global pool is disabled.

```bash
# Global pool (sing-box backed, lives in ~/.config/xtg/proxy/)
xtg proxy global status            # enabled, active proxy, pool size, --json
xtg proxy global enable            # route Telegram through the pool
xtg proxy global disable
xtg proxy global list              # every proxy in the pool
xtg proxy global add ss://...      # add a proxy (auto id, or --name P1)
xtg proxy global remove P1         # remove one, or P1=>P456 for a range
xtg proxy global clear             # empty the pool
xtg proxy global use P1            # set the active proxy
xtg proxy global test P1           # test one proxy via sing-box
xtg proxy global test              # test every proxy
xtg proxy global refresh <url>     # replace the pool from a subscription
xtg proxy global source add mysub <url>   # remember a subscription URL
xtg proxy global source list               # list remembered sources
xtg proxy global source refresh mysub     # refresh from a remembered source
xtg proxy global source remove mysub
xtg proxy global source clear

# Legacy per-command list (XTG_PROXY_FILE)
xtg proxy telegram list
xtg proxy telegram check [P1 P2]
xtg proxy telegram show P3
```

The global pool lives in `~/.config/xtg/proxy/`:

| File | Content |
| --- | --- |
| `state.json` | `enabled`, `active_proxy_id`, `last_refresh`, `core` |
| `list_proxy.json` | `{"proxys": {"P1": "uri", ...}}` |
| `sources.json` | `{"mysub": "https://...", ...}` |

On first load the pool seeds from the legacy list file for backward
compatibility, so an existing `xtg/proxy/list_proxy.json` is picked up
automatically. `sources.json` remembers named subscription URLs so a refresh
does not have to be retyped.

Subscription files are parsed as one proxy per line. A line that is itself an
`http(s)://` URL **with a path** is treated as a nested subscription manifest
(Hiddify profile files list other subscription URLs) and is followed
recursively, so a single `xtg proxy global source refresh` pulls every proxy
from a profile tree instead of storing the manifest URLs as proxies. A bare
`https://user:pass@host:port` with no path is a proxy endpoint and is kept.

The list file (`xtg/proxy/list_proxy.json` by default, or `XTG_PROXY_FILE`)
accepts:

```json
{
  "proxys": {
    "P1": "socks5://host:port",
    "P2": "socks5://user:pass@host:port",
    "P3": "tg://proxy?server=host.tld&port=443&secret=base64secret"
  }
}
```

Accepted entry formats: `socks5://host:port`, `http://host:port`,
`host:port:user:pass`, `host:port` and the Telegram MTProto URI
`tg://proxy?server=…&port=…&secret=…`. Entries without a scheme are treated as
SOCKS5. MTProto entries keep their `server`, `port` and `secret` separately and
the secret is masked everywhere except the canonical `url` used to write the
list file back. Invalid entries are reported by `xtg proxy telegram list` instead of
aborting the run.

`xtg proxy telegram check` connects with TCP for the classical schemes. MTProto
proxies are checked by speaking their transport (`xtg/proxy/mtproto.py`, a
byte-compatible port of Telethon's `MTProxyIO`):

* the 64 byte obfuscated2 initiation is sent, with AES-256-CTR keys derived as
  `sha256(header[8:40] + secret)` - the secret is part of the key schedule, so a
  proxy configured with a different secret cannot decrypt the message that
  follows;
* a real `req_pq_multi` message is sent behind it, framed for the abridged
  codec. No Telegram account, session or authorization key is involved;
* an MTProto proxy **never answers a handshake** - it waits for a message it can
  forward - so staying connected is the success case, and being hung up on is
  the failure. That is the same criterion Telethon uses after connecting.

Statuses are `alive`, `dead` (nothing listening, or the proxy hung up),
`invalid` (cannot work as configured, e.g. a missing secret), `unsupported`
(unknown scheme, a fake-TLS/`ee` secret, or `pycryptodome` not installed) and
`error`. Use `xtg proxy telegram check --json` or the per-entry detail line for the
protocol level reason.

`xtg proxy global test` validates every pool entry through a real `sing-box`
process: a temporary mixed inbound, the proxy as the outbound, and an HTTP
request to `https://www.gstatic.com/generate_204`. Dead proxies report
`alive:false` instead of aborting, and MTProto entries surface a graceful
"Unsupported proxy type for sing-box: mtproto" because the global pool is
outbound-only.

To probe a real endpoint without touching the list file:

```bash
uv run python -m xtg.proxy "tg://proxy?server=host.tld&port=443&secret=..."
XTG_MTPROXY_URI="tg://proxy?..." uv run python -m xtg.proxy --settle 5
```

Or run the integration test, which skips itself unless a URI is provided:

```bash
XTG_MTPROXY_URI="tg://proxy?..." uv run pytest -m integration
```

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
xtg info messages TARGET            # discussion graph + reply edges
```

`xtg info messages TARGET` walks the discussion tree of a channel: it stores
each thread root as a comment, every reply as an edge, and re-exports the graph
in JSON, CSV, Markdown or Obsidian format. Edges are linked strictly by
`(telegram_chat_id, telegram_message_id)` - never by timestamp, text, author or
position - so an interrupted collection can be resumed safely.

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

### `xtg reddit`

```bash
xtg reddit status                          # credentials, PRAW state, row counts
xtg reddit collect linux                   # default: the submissions of r/linux
xtg reddit collect cybersecurity --posts   # submissions only
xtg reddit collect linux --all             # subreddit + posts + comments
xtg reddit collect linux --comments -n 50  # comments only
```

| Option | Meaning |
| --- | --- |
| `--posts` | Store the submissions of the listing |
| `--comments` | Store the comments of the newest stored submissions |
| `--all` | Subreddit metadata, posts and comments |
| `-n, --limit` | Maximum items per section (default `100`) |
| `-s, --listing` | `new` (default), `hot`, `top`, `controversial`, `rising` |
| `--full` | Ignore the resume cursor and re-walk the listing |
| `--json` | Summary as JSON instead of a table |

Behaviour:

* `r/linux`, `/r/Linux/` and `linux` are all accepted;
* repeated runs **resume**: Reddit's own `after` cursor limits the request to
  submissions newer than the newest stored one, so incremental collection is
  cheap. `--full` re-walks the listing to refresh scores and comment counts,
  and the already stored rows are updated instead of duplicated;
* comments are walked depth first; Reddit's `MoreComments` placeholders are
  skipped because expanding them costs extra requests and pulls content nobody
  asked for;
* every relationship is also written to the shared `entities` / `relations`
  tables (`reddit:subreddit:r/linux --contains--> reddit:post:t3_abc`,
  `reddit:user:alice --authored--> reddit:post:t3_abc`), and the link domain
  plus the title hashtags are registered as pivots with
  `source = "reddit"`;
* Reddit rate limits (`TooManyRequests`), missing subreddits (`NotFound`) and
  private ones are mapped to the documented exit codes.

### `xtg analyze` / `xtg export` (Phase 4)

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

Reddit works the same way, in the same database:

```bash
uv run xtg reddit collect linux --all -n 200
uv run xtg db stats --json | jq '.tables | {"reddit_posts", "reddit_comments"}'
uv run xtg reddit collect linux --full      # refresh scores and comment counts
```

Cross-source intelligence - Telegram and Reddit rows share one graph:

```bash
sqlite3 data/database.db \
  "SELECT source, target FROM relations WHERE source LIKE 'reddit:%' LIMIT 5;"
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
│   ├── context.py  lazy per-invocation state (settings, db, proxies, sources)
│   └── commands/
├── core/           config, logging, errors/exit codes, small utilities
├── database/       SQLAlchemy models, engine management, query helpers
├── proxy/          independent proxy subsystem (parser/loader/checker/manager)
│   ├── core/       global pool manager + sing-box runner + config builder
│   ├── sources/    subscription fetcher and named-source manager
│   └── manager.py  legacy per-command proxy list
├── sources/        platform connectors behind one contract
│   ├── base.py     SourceReport, BaseSource, BaseCollector
│   ├── registry.py lazy source lookup by name
│   ├── telegram/   adapter over the async Telethon collector
│   └── reddit/     PRAW client, serializers, collector
├── telegram/       MTProto boundary (client, auth, collector, serializers)
└── export/         export format registry and writer contract
```

### Source layer

Every platform implements the same four verbs - `connect()`, `parse()`,
`save()`, `collect()` - so commands, exports and the future analysis pass never
branch on the platform:

| Layer | Responsibility |
| --- | --- |
| `BaseSource` | Platform life cycle (build the client, close it), used as a context manager per command |
| `BaseCollector` | Shared engine: resolve the target, run the sections, pace the calls, count duplicates, isolate per-item errors |
| `RedditCollector` | The base engine plus Reddit specific sections (`subreddit`, `posts`, `comments`) |
| `TelegramCollectorAdapter` | Presents the existing async Telethon collector through the same contract |

Adding a platform means: create `xtg/sources/<name>/` with a
`BaseCollector` subclass, register it in `xtg/sources/registry.py`, add its
models and queries, and (optionally) a CLI command. The Telegram collector was
*not* rewritten - it was adapted.

Design rules that the code follows:

* **Layering.** `core` knows nothing about Telegram or Reddit, `proxy` knows
  nothing about either. The bridges are `xtg/telegram/client.py` (a `ProxyEntry`
  becomes Telethon's connection arguments) and `xtg/sources/reddit/client.py`.
* **Lazy subsystems.** A command only builds what it needs (`status` never
  opens a platform connection), sources are imported on demand, and the
  database engine is always disposed.
* **Lazy platform imports.** `xtg.telegram` and `xtg.sources.reddit` import
  without Telethon/PRAW installed; only the client factories need them, so a
  missing dependency is reported at the exact command that needs it.
* **Idempotent writes.** `upsert_*` helpers plus unique constraints make
  re-running a collection safe on every source.
* **Restart-safe collection.** Listings are paged, batches are committed as they
  arrive, and duplicates are counted instead of stored.
* **One database.** No second SQLite file: the Reddit tables live next to the
  Telegram ones, and `Entity` / `Relation` are source agnostic, so both
  platforms end up in a single knowledge graph.
* **Graph ready.** `entities` and `relations` use stable string node references
  (`chat:@channel`, `entity:email:a@b.c`, `reddit:post:t3_abc`) so users,
  chats, messages and pivots can be linked in one table.
* **No silent data loss.** Every entity row keeps the original platform payload
  in `raw_data`.

### Database schema

| Table | Purpose |
| --- | --- |
| `users` | Telegram users and bots |
| `chats` | Channels, groups, supergroups, dialogs |
| `memberships` | User ↔ chat links with a role |
| `messages` | Messages with date, sender and media flag |
| `media` | Media attachments of a message |
| `entities` | Extracted pivots (email, domain, phone, hashtag, ...) with `source` |
| `relations` | Knowledge graph edges between node references |
| `reddit_users` | Reddit accounts (`t2_`) with karma and account flags |
| `reddit_subreddits` | Subreddits (`t5_`) with subscribers and description |
| `reddit_posts` | Submissions (`t3_`) with title, body, score and link |
| `reddit_comments` | Comments (`t1_`) with depth, score and parent |

Schema changes are applied by recreating the tables; `xtg db reset` takes a
backup first:

```bash
uv run xtg db reset      # backs up, drops and recreates the schema
```

`entities` gained the `source` column and the generic `first_seen_ref` in this
phase: `first_seen_id` stays a foreign key into `messages` (Telegram), while
other sources record their provenance as a node reference such as
`reddit:post:t3_abc`.

---

## Development

```bash
uv sync                       # once, with network
uv run pytest                 # 720 tests
uv run ruff check
uv run ruff format --check
uv run pyright                # type checking (see below)
```

The test suite is isolated (`XTG_HOME` points at a temporary directory and the
cached settings are reloaded per test), so it never touches the real `data/`
directory. Every Telegram *and* Reddit test runs against the doubles in
`tests/fakes.py` and `tests/fakes_reddit.py`, so **no test needs the network, a
Telegram account or a Reddit account**.

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
| `praw>=7.7` | runtime | declared, **not installed in the offline sandbox** |
| `telethon>=1.34` | runtime | declared, **not installed in the offline sandbox** |
| `pyright>=1.1.380` | dev | declared, **not installed in the offline sandbox** |
| `pytest`, `pytest-asyncio`, `ruff` | dev | installed |
| `loguru`, `pydantic`, `pydantic-settings`, `python-dotenv`, `rich`, `sqlalchemy`, `typer` | runtime | installed |

There are no optional extras (`[project.optional-dependencies]`): every feature
group only needs the runtime set above. Telethon and PRAW cannot be installed
in the current sandbox because DNS resolution fails
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
3. **Phase 3 - Source layer + Reddit connector:** done (`BaseSource` /
   `BaseCollector`, Telegram behind the contract, `xtg reddit status/collect`).
4. **Phase 4 - Analysis:** entity extraction (emails, domains, phones,
   hashtags) over every source, knowledge graph builder, JSON/CSV/Markdown/
   Obsidian writers.
5. **Phase 5 - Intelligence:** search engine (FTS5), cross-source correlation,
   timeline reports, plugin architecture.

---

## Legal

This toolkit is intended for security research and OSINT work on data you are
authorised to access. Respect Telegram's terms of service and applicable law.

## License

See the repository for licensing information.