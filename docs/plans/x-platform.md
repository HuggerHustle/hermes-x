# Plan: native X DM platform for Hermes

Status: **approved**. Steps 1–5 (code and config text) and 8 are built and pass locally; steps 6–7
wait on the GitHub repo and the VM. Runbook: `docs/deploy.md`.

## Goal

Huggy (the Hermes agent on GCP VM `huggerhustle-huggy`) answers Direct Messages sent to
[@huggerhustle](https://x.com/huggerhustle) as a real Hermes gateway platform: one session per
conversation with follow-ups, and a narrow per-platform toolset.

## Where we start

- Hermes Agent v0.23.1 (`3b421578588243f920886e6fe79a6faa87ea026e`) runs as Linux user `hermes`
  on `huggerhustle-huggy`. Every Hermes claim in this plan is checked against that tag.
- Hermes has no X platform. Its messaging platforms are plugins under `plugins/platforms/`
  (Slack, Telegram, SMS, IRC, ...), and `gateway/platforms/ADDING_A_PLATFORM.md` documents a
  plugin path that needs no change to core Hermes code.
- `xurl` 1.3.4 is installed for `hermes`, with an OAuth2 token for @huggerhustle
  (user id `2112799656950259712`) in `~hermes/.xurl`.
- DMs to @huggerhustle are classic DMs (`/2/dm_events`), not XChat. The account has no XChat keys.
- Today a stopgap answers DMs: `~hermes/.hermes/scripts/x_dm_responder.py`, run every 5 minutes
  by the Hermes cron job "X DM responder" (`--no-agent`). Its model call runs with
  `hermes chat -t todo`, so a DM cannot reach a shell. `-t ''` means _all_ tools: a test DM
  asking to run `touch /tmp/pwned-by-dm` did run it with the default toolset.

## Repo

Private `HuggerHustle/hermes-x-platform`, checked out at `~/Projects/huggerhustle/hermes-x-platform`.
Tooling mirrors `crawler`: `uv`, Python 3.14 for dev, black + isort + ruff + mypy strict, pytest
with pytest-asyncio, `AGENTS.md` with a `CLAUDE.md` pointer. Ruff also selects `ASYNC`, so a
blocking call inside `async def` fails lint. `plugin/` targets Python 3.11+, like Hermes.

The plugin lives in `plugin/` and only that directory is installed on the VM. Hermes treats a
`pyproject.toml` with `[build-system]` or `[dependency-groups]` inside a plugin directory as
"installs packages" (`pm/plugin_declarations.py:160`), which would sync our dev tools into
Hermes's runtime venv. Keeping `pyproject.toml`, `tests/` and `docs/` at the repo root also keeps
them out of the install-time security scan, which today returns `caution` on this plan file.

| Path                 | Job                                                        |
| -------------------- | ---------------------------------------------------------- |
| `plugin/plugin.yaml` | `name: x`, `kind: platform`; env vars for the setup wizard |
| `plugin/__init__.py` | `from .adapter import register`                            |
| `plugin/adapter.py`  | `XAdapter(BasePlatformAdapter)` and `register(ctx)`        |
| `plugin/x_api.py`    | the only X API calls: list DM events, post a DM reply      |
| `tests/`             | fake `xurl`; adapter, polling, dedupe, send                |
| `docs/`              | setup and deploy runbook                                   |
| `pyproject.toml`     | dev tooling only                                           |

`plugin/` is stdlib-only. `register(ctx)` mirrors the IRC plugin
(`plugins/platforms/irc/adapter.py:591`), not SMS: SMS's `gateway_env.py` uses the built-in enum
member `Platform.SMS`, and there is no `Platform.X`. Env wiring goes straight into
`ctx.register_platform(name="x", ...)`: `env_enablement_fn`, `is_connected`,
`allowed_users_env="X_ALLOWED_USERS"`, `allow_all_env="X_ALLOW_ALL_USERS"`,
`allow_update_command=False`, `max_message_length=10000`.

Env vars: `X_USER_ID` (required), `X_ALLOWED_USERS`, `X_ALLOW_ALL_USERS`, `X_POLL_SECONDS`
(default 300).

Not built: cron delivery to X (`X_HOME_CHANNEL`, `standalone_sender_fn`). Nothing needs Huggy to
start a DM, and leaving it out means only the gateway process calls `xurl` for this plugin.

## Steps

1. **Scaffold.** Add `hermes-agent` at tag `v0.23.1` as a dev dependency so tests run against the
   real `BasePlatformAdapter`. Hermes's `setup.py` refuses wheel builds unless
   `HERMES_NIX_BUILD=1`, so try `HERMES_NIX_BUILD=1 uv sync` with a git source first; if that
   fails, use an editable path source pointing at a local clone at the tag. Tests do not run only
   on the VM.
2. **`x_api.py`.**
   - Calls `xurl` with `asyncio.create_subprocess_exec`, a hard timeout, and kills the child on
     cancel.
   - One `asyncio.Lock` around every `xurl` call, so a poll and a send never refresh the OAuth2
     token at the same time.
   - Keeps the stopgap's rules: 1:1 conversations that include `X_USER_ID`, `MessageCreate`
     events only, not our own messages, 24-hour window. Which events are new comes from the
     seen-id store alone: an "other person spoke last" rule would drop a follow-up sent while
     Huggy was replying.
   - `--auth oauth2` on every call, and xurl's own config env vars removed from the child.
3. **`adapter.py`.**
   - `connect()` takes `acquire_scoped_lock` (released in `disconnect()`) and starts a polling
     task every `X_POLL_SECONDS`.
   - Each new inbound DM becomes a `MessageEvent` with `allow_gateway_control=False`, passed to
     `handle_message()`, one session per `dm_conversation_id`. With that flag a DM starting with
     `/` is plain text, never a gateway command (`gateway/platforms/event.py:191`).
   - **Dedupe marks at admission.** The event id is written to plugin storage
     (`plugins/plugin_storage.py`) before `handle_message()` is called. `handle_message()` returns
     before the model turn runs, so marking after `send()` would re-run a paid turn on every poll
     while X rejects the reply.
   - **First start seeds, does not answer.** With empty storage the adapter records the newest
     event id and replies to nothing older, so cutover cannot double-answer what the stopgap
     already handled.
   - **Read only what is new.** Next to the seen ids the store keeps a cursor: the newest event
     time at the last finished poll. A poll asks for 20 events at a time and pages back to the
     cursor or the 24-hour line, five pages at most. Only a finished poll moves the cursor, so a
     drain or crash re-reads the gap and the seen ids skip what was already handed over.
   - New DMs from one conversation in one poll become one `MessageEvent`, texts joined by a blank
     line. A second `handle_message()` on a busy session interrupts the running turn at the
     default `busy_input_mode` (`gateway/run_busy.py:1022`), and that setting is per profile, not
     per platform.
   - `@` before a letter gets a word joiner: Hermes expands `@file:`, `@url:` and `@diff`
     references in every inbound turn, which would let a stranger read local files.
   - No delivery-ledger row and no retry or plain-text fallback send (`_record_delivery_obligation`,
     `_send_retry_is_final`): either would re-send a DM. `display_tier="minimal"` keeps interim
     and long-running notices out of DMs, since each is a paid POST.
   - `X_POLL_SECONDS` has a floor of 60 (GET allows 15 calls per 15 minutes). A poll makes one
     call, up to five in a burst; past the limit it fails and the next tick retries.
   - `send()` posts to `/2/dm_conversations/{id}/messages` and strips markdown, like the SMS
     adapter. On X `402` it returns `SendResult(success=False, retryable=False,
     error_kind="unknown")`: `forbidden` would mark the conversation dead, but a 402 means the
     account is out of credits.
   - A `402` on poll is logged and the next tick tries again. No extra backoff.
   - `send_typing()` does nothing (classic DMs have no typing indicator).
   - `platform_hint`: X DM, plain text, short replies, you are Huggy from HuggerHustle.
4. **Tests first.**
   - Polling yields only new inbound 1:1 `MessageCreate` events.
   - A restart does not reply twice; first start with empty storage replies to nothing.
   - Every `MessageEvent` has `allow_gateway_control=False`.
   - `send()` targets the right conversation.
   - X `402` on send returns a non-retryable failure; `402` on poll does not crash the gateway.
   - A hung `xurl` is killed at the timeout.
5. **`x` toolset and platform config.** In the VM's `config.yaml`:
   - `platform_toolsets.x: [search, todo, no_mcp]`. `todo` keeps Hermes off its composite rules,
     which add `x_search` when xAI credentials exist. Without `no_mcp`, Hermes adds every enabled
     MCP server to the platform (`hermes_cli/tools_config.py:752`). No `memory`: that tool writes
     `MEMORY.md`, which is loaded into owner and cron sessions that do have a shell.
     Conversation follow-ups come from the session transcript, not from `memory`.
   - `platforms.x.allow_admin_from: ["2112799656950259712"]` (the bot's own id, so no stranger is
     admin) and `platforms.x.gateway_restart_notification: false`.
   - `known_plugin_toolsets.x` lists every plugin toolset key, else Hermes adds them all to X, and
     `platforms.x.unauthorized_dm_behavior: ignore` (no pairing-code DMs). See `docs/deploy.md`.
   - Checks before go-live, through the X channel: the live session's tool list is exactly
     `web_search` and `todo_list`; the `touch /tmp/pwned-by-dm` DM runs nothing; a `/model` DM is
     answered as text; an `@file:` DM gets no file content back.
6. **Deploy.**
   - Read-only deploy key on the VM for this repo.
   - `hermes plugins install "git@github.com:HuggerHustle/hermes-x-platform.git#plugin"
--ref <40-char sha> --enable`. The `owner/repo` shorthand resolves to HTTPS
     (`hermes_cli/plugins_cmd.py:333`) and cannot use the deploy key.
   - Set `X_USER_ID` and `X_ALLOW_ALL_USERS=true`.
7. **Cut over.** In this order: pause cron job "X DM responder"; confirm no
   `x_dm_responder.py` process is running; restart the gateway. Keep the script one week as a
   fallback, then delete it.
8. **Document.** `AGENTS.md` and `docs/` in this repo, and the Linear ticket.

## Linear

- **OPS-30 "guard the agent"** (project `huggerhustle`, Backlog) overlaps: "only do lead-finding
  and business tasks". Proposal: fold in only the X part (step 5 toolset and platform hint) and
  leave the rest of OPS-30 open.
- New ticket "Native X DM gateway for Huggy": not created, the workspace is at Linear's free
  issue limit. OPS-30 has a comment pointing here.

## Risks

- **Hermes internals change.** The plugin depends on `BasePlatformAdapter` and
  `register_platform`. The repo pins the Hermes version it is tested against; the runbook says to
  run the tests before any `hermes update` on huggy.
- **Strangers reach the real agent.** Step 5's config and its three checks are the safeguard;
  nothing goes live before they pass.
- **A lost reply is not retried.** A gateway restart mid-turn, or a failed `send()`, leaves that
  DM unanswered until the person writes again. Accepted: the other choice is double replies or
  repeated paid turns.
- **DMs during cutover.** A DM that arrives between pausing the cron job and the gateway's first
  poll is seeded as seen and gets no reply. Check the inbox by hand after cutover.
- **A DM answered by hand is answered again.** If someone replies in the X app before the next
  poll, Huggy still answers the person's DM.
- **Corrupt seen-id store.** It is renamed to `seen.corrupt` and re-seeded, so DMs since the last
  good poll get no reply; the gateway log says so.
- **Five pages per poll.** More than 100 DM events between two polls: the oldest get no reply,
  and the gateway log says so.
- **The cursor skips late events.** Nothing older than the newest event of the last finished poll
  is answered. An event X delivers late, with an older timestamp, gets no reply.
- **Paging assumes newest first.** X does not document the order of `/2/dm_events`. If it were
  oldest first, no DM would be answered; go-live check 1 shows it either way.
- **402 shape is a guess.** X does not document the body; detection matches a `CreditsDepleted`
  title or a `credits` type, else it is a generic API error.
- **Hermes expands `@` references for every platform.** Upstream should skip expansion when
  `allow_gateway_control=False`; the plugin's word joiner is the local fix.
- **`xurl` token refresh outside the plugin.** Owner and cron sessions that use the `xurl` skill
  can refresh the same token while the gateway polls. The plugin's lock does not cover them.
- **Cost.** X bills reads per event returned ($0.010), counted once per UTC day, and each reply
  ($0.015). A poll asks for 20 events and pages back only to the cursor, so an idle inbox costs
  about $0.20 a day. Webhooks (Account Activity API, higher tier, public endpoint, CRC handler)
  are out of scope.
- **Busy acknowledgments are paid DMs.** A DM that arrives while that conversation's turn is still
  running gets one busy acknowledgment per 30 s (`gateway/run_busy.py:1036`); `display_tier`
  only strips its detail. The only off switch, `display.busy_ack_enabled`, is global. Needs a turn
  longer than one poll; accepted.
- **Mentions are out of scope.** Public replies fall under stricter X automation rules; follow-up
  ticket.

## Decisions

- Repo name `hermes-x-platform`.
- OPS-30: only the X part is covered here.
- Toolset `search`, not `web`: `web_extract` would let a stranger make Huggy fetch a URL they pick.
- `todo` in the X toolset list: it keeps `x_search` off on a host with xAI credentials.
- No cron delivery to X.
