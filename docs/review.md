# Review: hermes-x-platform against Hermes v0.23.1

Target: commit `d0efa19`, checked against Hermes Agent `v0.23.1`
(`3b421578588243f920886e6fe79a6faa87ea026e`) in the dev venv. Date: 2026-11-11.

Hermes paths below are relative to the installed package
(`.venv/lib/python3.14/site-packages/`). Each finding was reproduced by running real Hermes code
against a scratch `HERMES_HOME`, not only read.

## Summary

| #   | Finding                        | Severity | Resolution                                  |
| --- | ------------------------------ | -------- | ------------------------------------------- |
| 1   | Extra toolsets reach X         | High     | `deploy.md`: `context_engine`, known list   |
| 2   | `except A, B:` needs 3.14      | Low      | parenthesized; Black and ruff target 3.11   |
| 3   | Pairing-code DMs to strangers  | Medium   | `deploy.md`: `unauthorized_dm_behavior`     |
| 4   | Drain race drops a DM          | Medium   | `adapter.py`: check before each mark        |
| 5   | Naive timestamp stalls polling | Low      | `x_api.py`: skip rows without an offset     |
| 6   | Typing loop runs a no-op       | None     | No change                                   |
| 7   | `fsync` on the event loop      | None     | No change; the proposed fix loses DMs       |
| 8   | Seed order in `seen.json`      | None     | No change                                   |

## Findings

### 1. Extra toolsets reach X sessions (High)

`platform_toolsets.x: [search, no_mcp]` is not the whole X toolset. Reproduced with that config:

- Every enabled plugin that registers a tool adds its toolset to X: plugin toolsets are on for a
  platform unless listed in `known_plugin_toolsets.<platform>` (`hermes_cli/tools_config.py:521`).
- With `XAI_API_KEY` or xAI OAuth on the host, X also gets `x_search`. It is added only when the
  list names no checklist toolset (`hermes_cli/tools_config.py:515`, `:614`); `search` and
  `no_mcp` are not checklist keys.
- A non-default `context.engine` adds `context_engine` (`hermes_cli/tools_config.py:625`).
- `hermes plugins enable` appends the plugin's toolset to every saved `platform_toolsets` list,
  `x` included (`hermes_cli/plugins_cmd.py:997`). That undoes `known_plugin_toolsets.x`.

Rejected fixes:

- `agent.disabled_toolsets` is global: it also removes the tool from the owner's own sessions.
- Overriding `XAdapter.toolsets_for_source()` to return `["search"]` does not help. The gateway
  feeds the override through the same resolver (`gateway/run_turn.py:435`), so plugin toolsets
  stay, and dropping `no_mcp` adds every enabled MCP server.

- Any save through `hermes tools` (picker or `enable`/`disable`) drops `no_mcp`
  (`hermes_cli/tools_config.py:795`).

Resolution (`docs/deploy.md`): `platform_toolsets.x: [search, context_engine, no_mcp]`.
`context_engine` is a checklist key that adds no tool at the default engine, so `x_search` stays
off. Not `todo`: its `todo_list` is deferred behind `tool_search`, which would also hide a leaked
tool from go-live check 1.
`known_plugin_toolsets.x` lists every plugin toolset key. The runbook says to edit these lists by
hand, to move a newly enabled plugin's key out of `platform_toolsets.x`, and to re-run go-live
check 1 after any plugin, toolset-list or context-engine change.

### 2. `except TypeError, ValueError:` needs Python 3.14 (Low)

`plugin/adapter.py:54` uses PEP 758 syntax, a `SyntaxError` on 3.11–3.13. It compiles on 3.14.

Hermes v0.23.1 pins its runtime to CPython 3.14.9 (`pm/lock.json` in the Hermes checkout), so a
standard install runs 3.14. Changing the line to `except (TypeError, ValueError):` does not stick:
Black with `target-version = ["py314"]` rewrites it back.

Resolution: the line is parenthesized, and `pyproject.toml` sets Black's `target-version` to
3.11–3.14 and ruff's to `py311`, so ruff fails on any syntax newer than 3.11 in `plugin/`.

### 3. Pairing-code DMs to strangers (Medium)

An unauthorized DM gets a pairing-code DM unless pairing is suppressed
(`gateway/authz_mixin.py:820`, sent from `gateway/run_inbound.py:235`). Each is a paid POST to a
stranger. Reproduced:

| Env                           | Today  | With `unauthorized_dm_behavior: ignore` |
| ----------------------------- | ------ | --------------------------------------- |
| `X_ALLOW_ALL_USERS=true`      | answer | answer                                  |
| nothing set (typo, lost line) | pair   | ignore                                  |
| `X_ALLOWED_USERS=7`           | pair   | ignore                                  |

`allowlist_suppresses_pairing=True` in `register()` covers only the third row, and 10 bundled
platforms (IRC among them) do not set it either. The config line covers both rows.

Resolution (`docs/deploy.md`): `platforms.x.unauthorized_dm_behavior: ignore`. No code change.

### 4. A gateway drain during a poll drops a DM for good (Medium)

Drain sets each adapter's message handler to `None` (`gateway/run_runtime.py:733`), and
`handle_message()` then discards the event (`gateway/platforms/base.py:3919`). `_poll_once`
checked for a handler only before the GET. Reproduced: a drain during the GET leaves the DM marked
in `seen.json` and never handled, and no later poll admits it.

A single check after the GET is not enough: a poll with two conversations awaits the first
`handle_message()`, and a drain landing there still marks and drops the second.

Resolution (`plugin/adapter.py`): the handler check runs before each burst is marked. Test:
`test_drain_during_poll_leaves_unadmitted_dms_unmarked`. An unmarked burst is answered after the
restart.

### 5. A timestamp without an offset stalls every poll (Low)

`datetime.fromisoformat` accepts `2026-11-11T12:00:00`. `pending_events` then subtracts it from
an aware `now` and raises `TypeError`. `_poll_safely` logs it and nothing is marked, so no DM is
lost, but every poll fails while that row is on the page. X sends `...Z` timestamps, so this is
hardening.

Resolution (`plugin/x_api.py`): `parse_dm_events` skips such rows. Test:
`test_list_skips_timestamps_without_offset`.

### 6. Typing loop (no change)

True: `typing_indicator` defaults to on, so the base adapter calls the no-op `send_typing()` every
2 seconds during a turn (`gateway/platforms/base.py:3276`). It sends nothing and costs nothing
measurable. Skipping it would mean overriding `_keep_typing`, more code for no gain.

### 7. `fsync` on the event loop (no change)

True: `_save_seen` writes and `fsync`s on the loop, a few milliseconds per admitted burst. The
proposed `await asyncio.to_thread(self._save_seen, seen)` adds an `await` between the mark and
`handle_message()`. Reproduced: a cancel there (`disconnect()`) leaves the DM marked and never
handled. The blocking write has no such gap.

### 8. Seed order in `seen.json` (no change)

True: the seed stores ids newest first, later polls append oldest first. A seeded id is trimmed
only after 1,900 newer inbound DMs, long after the cursor passed it, so order never changes
which DM is answered.

## Not in the first review

- **Busy acknowledgments are paid DMs.** A DM that arrives while that conversation's turn is still
  running gets a busy acknowledgment (`gateway/run_busy.py:1036`, at most one per 30 s).
  `display_tier="minimal"` only strips its detail; `display.busy_ack_enabled` turns it off, but
  for every channel. It needs a turn longer than one poll interval,
  so it is listed under Risks in `docs/plans/x-platform.md`. Read in source, not reproduced.
- `XClient`'s lock is per instance, not per process. One adapter holds one client, so this does
  not matter today.

## Checks

| Check            | Command                                 | Result           |
| ---------------- | --------------------------------------- | ---------------- |
| Tests            | `uv run pytest -q`                      | 72 passed        |
| Format           | `uv run black --check .`                | clean            |
| Imports          | `uv run isort --check-only .`           | clean            |
| Lint             | `uv run ruff check .`                   | clean            |
| Types            | `uv run mypy`                           | clean            |
| Install scan     | `tools.plugin_guard.scan_plugin`        | `safe`, 0        |
| Plugin validator | `hermes plugins validate plugin`        | 15 passed        |
