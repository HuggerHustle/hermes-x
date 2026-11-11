# hermes-x-platform — working in this repo

A Hermes Agent gateway platform plugin: Huggy answers classic X Direct Messages sent to
@huggerhustle. It runs inside the Hermes gateway on the GCP VM `huggerhustle-huggy`.

| For                                 | Read                       |
| ----------------------------------- | -------------------------- |
| what it does, the shape, why        | `docs/plans/x-platform.md` |
| VM config, install, cutover, checks | `docs/deploy.md`           |

Linear: project `huggerhustle`. OPS-30 ("guard the agent") covers the guard on Huggy's other
channels.

## Layout

| Path                 | Owns                                                           |
| -------------------- | -------------------------------------------------------------- |
| `plugin/`            | the only directory installed on the VM; stdlib + Hermes only   |
| `plugin/adapter.py`  | `XAdapter`: polling, seen-id store, send, and `register(ctx)`  |
| `plugin/x_api.py`    | every `xurl` call, response parsing, the DM filter             |
| `plugin/plugin.yaml` | manifest: `name: x`, `kind: platform`, env vars                |
| `tests/fake_xurl.py` | stand-in `xurl`, run as a real subprocess                      |
| `pyproject.toml`     | dev tooling only; it must never move into `plugin/`            |

## Commands

| Command                                            | Does                       |
| -------------------------------------------------- | -------------------------- |
| `uv sync`                                          | dev env, Hermes v0.23.1    |
| `uv run pytest -q`                                 | all tests                  |
| `uv run ruff check plugin tests`                   | lint (includes `ASYNC`)    |
| `uv run black plugin tests && uv run isort plugin tests` | format               |
| `uv run mypy`                                      | types, strict              |

`uv sync` builds Hermes from the git tag `v0.23.1`; `[tool.uv.extra-build-variables]` sets
`HERMES_NIX_BUILD=1`, which Hermes's `setup.py` needs to allow the build.

## Rules

- Hermes facts come from the `v0.23.1` source, the version on the VM. Before any `hermes update`
  on huggy, bump the tag in `pyproject.toml` and run the tests against it.
- `plugin/` imports only the standard library and Hermes. Hermes installs it as-is; a
  `pyproject.toml` there would make Hermes install packages into its own venv.
- Every inbound `MessageEvent` has `allow_gateway_control=False`, and `@` before a letter gets a
  word joiner, so a stranger's DM can never run a gateway command or pull in `@file:` content.
- Env is read only through Hermes's `_shared` helpers (`extra_or_secret`, `get_scoped_secret`),
  never `os.getenv`.
- An event id is marked seen before `handle_message()`. A DM is never re-sent: a lost reply beats
  a double reply or a repeated paid turn.
- Every `xurl` call goes through `XClient._run`: one lock, a hard timeout, the child killed on
  timeout or cancel.
