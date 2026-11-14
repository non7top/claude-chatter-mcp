# Antigravity (agy) notes

Constraints of the Antigravity CLI (`agy`) that shaped this bridge, the approaches that were tried and dropped, and what is still open. Sources are the code docstrings and commit history; anything not checked against a running `agy` is marked.

## Constraints

- **An MCP server cannot wake `agy`.** The only way to start a turn is `agy agentapi send-message`, which needs `ANTIGRAVITY_LS_ADDRESS` and `ANTIGRAVITY_CSRF_TOKEN`. `agy` puts those (and `ANTIGRAVITY_CONVERSATION_ID`) only into its agent's shell tool, never into MCP servers or hook processes (the `antigravity-wakeup` package).
- **A hook cannot prime push by itself**, for the same reason: it has no token. The `PreInvocation` hook injects an `ephemeralMessage` that tells the agent to call `enable_push` and run the returned command in its own shell.
- **Conversation titles are not exposed** to MCP servers, the shell environment, the status line payload or `agy agentapi get-conversation-metadata`. Requested upstream: google-antigravity/antigravity-cli#1415. Until then the bridge names a session `<project basename>-<2 random alphanumerics>`, stored per project path under `~/.claude/chatter-session-names/`; the project path is the `agy` process's working directory.
- **Other MCP hosts send different parameter names** (and extra arguments such as `session_id`), so tool calls map known aliases onto the real arguments and drop unknown ones.

## How push works

1. The agent calls `enable_push` and runs the returned `bridge-mcp --prime` command from its shell.
2. `--prime` reads its own `ANTIGRAVITY_*` environment and sends it to its bridge over a `0600` control socket (`<bridge socket>.ctl`). The address must be loopback; the token is held in memory only and reaches the `agy` child through its environment, never argv, logs or disk.
3. Each buffered inbound message makes the bridge run `agy agentapi send-message` with a fixed notice. No sender-written text is injected. Sends are debounced (1 s default) and time out after 20 s.
4. A marker `~/.claude/chatter-primed/<conversation>.json` records the bridge pid. The hook skips conversations whose bridge is alive, and after a failed attempt it waits 10 minutes before asking again.
5. The prime command is run through a stable symlink, `~/.claude/chatter-bin/bridge-mcp`, so it can be allow-listed in `agy`.

Push is best effort. The maintainer reports it works but loosely, and it does not remove the need to call `read_messages` regularly. Whether `agy` always honours the hook's injected step has not been verified here.

## Tried and dropped

- **A persistent auto-spawned daemon.** It was added so the session survived `agy` restarting the stdio connection, then reverted. Live testing kept surfacing bugs from the same root cause: zombie pids fooling liveness checks, asyncio's subprocess transport killing the daemon on the wrapper's shutdown, and Ctrl+C not reaching a detached process. Only two lifecycles are kept: attached to a TTY (`--standalone`) or attached to the host over stdio. A session's identity still survives a host restart through the stored per-project name.
- **A hardcoded default session name.** Every unnamed bridge in every workspace derived the same socket path and took it from the others. Names are now per project path.

## Open

- Whether `agy` always honours the injected `ephemeralMessage` step (see above).
- Using the real conversation title once #1415 lands.
