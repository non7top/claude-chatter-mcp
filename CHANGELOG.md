# Changelog

## [0.5.0](https://github.com/non7top/claude-chatter-mcp/compare/v0.4.0...v0.5.0) (2026-11-13)


### Features

* add a persistent log file alongside stderr ([e2c7e8a](https://github.com/non7top/claude-chatter-mcp/commit/e2c7e8a1f57e41bbf4a54f810648190479557eae))
* add list_jobs and --jobs, drop peer tokens from list_sessions output ([896aefc](https://github.com/non7top/claude-chatter-mcp/commit/896aefcc6e093be1c56e66a5769a8921d1bb57b3))
* add read_messages tool with unread/read tracking (inbox model) ([129019a](https://github.com/non7top/claude-chatter-mcp/commit/129019a66b9ec7ce78a00cd6855537e1e2de35ec))
* Antigravity plugin with push priming ([f21fa7c](https://github.com/non7top/claude-chatter-mcp/commit/f21fa7cdcd6c2b1563f4cf1be37a6d9196c629a1))
* default session name is bridge-&lt;cwd basename&gt;, with generic CHATTER_* env vars ([449eccb](https://github.com/non7top/claude-chatter-mcp/commit/449eccb0d9f8e7bf374edb850f09307fc8a9ffc1))
* default session name is bridge-&lt;cwd basename&gt;, with generic CHATTER_* env vars ([95553cd](https://github.com/non7top/claude-chatter-mcp/commit/95553cdeafc039c194f13323c6621ecff96e95b4))
* enable persistent background daemon spawn in stdio mode to preserve session across MCP host restarts ([7652fa3](https://github.com/non7top/claude-chatter-mcp/commit/7652fa3c502353d248ec73faaf17277415182ee1))
* **inbox:** persist messages across bridge restarts using SQLite ([e83fd59](https://github.com/non7top/claude-chatter-mcp/commit/e83fd59ec38e401b523367ba8385e3fb574e51b0))
* **inbox:** persist messages and read state across restarts using SQLite ([978dab3](https://github.com/non7top/claude-chatter-mcp/commit/978dab3ff7728233b6c3d126ff2a9771e73a0eae))
* list_jobs and --jobs for background jobs ([0707ab7](https://github.com/non7top/claude-chatter-mcp/commit/0707ab7733f27d2e81fed0b631a7f85296adbe89))
* migrate MCP layer to FastMCP, rename project to claude-chatter-mcp, add MIT license ([26b0bca](https://github.com/non7top/claude-chatter-mcp/commit/26b0bca063518635d979ad1a59b0cb4bb2142d75))
* note async-mailbox semantics in the inbound ACK ([307ad44](https://github.com/non7top/claude-chatter-mcp/commit/307ad4428150da0087f1ab245e2b8b20f0300b8a))
* nudge agy to prime push via read_messages hint, and stop it polling in a loop ([f1e43b7](https://github.com/non7top/claude-chatter-mcp/commit/f1e43b73e384c26f09d34dedc3969a177f7a7ff8))
* optional push into an agy conversation via a one-time --prime from its shell ([d503c6f](https://github.com/non7top/claude-chatter-mcp/commit/d503c6f934b0209996f580bbf71b962b03af0dfc))
* package the repo as an Antigravity plugin with a PreInvocation prime hook ([5d04663](https://github.com/non7top/claude-chatter-mcp/commit/5d04663a00dbb815b99a2ecf13546abccd8e9036))
* persist a renamed session's identity across restarts ([c619244](https://github.com/non7top/claude-chatter-mcp/commit/c6192441df7caaea97c4a303349f5cb3c7a3f7ff))
* prime_push tells the agent to run the returned command ([85c886d](https://github.com/non7top/claude-chatter-mcp/commit/85c886d91b23c749ea3e698e1c23429a74b77ec2))
* run the plugin via uvx, add chatter skill, rename prime_push to enable_push, prime via ephemeralMessage ([7e260d5](https://github.com/non7top/claude-chatter-mcp/commit/7e260d539fbb03f1ca421d2bbbe11e5a1eb50a73))
* **storage:** track stable session identity across renames and handle edge cases ([fcb3370](https://github.com/non7top/claude-chatter-mcp/commit/fcb3370701e69143e8a0892d68391f75f8f63ec3))


### Bug Fixes

* capture the MCP session on the first message, not the first tool call ([a15c11d](https://github.com/non7top/claude-chatter-mcp/commit/a15c11dd5423f1421bf4a96f8292234c3597debc))
* **cli:** eliminate side effects in read-only modes and skip reload wait ([1947775](https://github.com/non7top/claude-chatter-mcp/commit/194777595576f07cfa585c83a90e78f44bb39317))
* delegate rename_session to the owning persistent daemon ([79b57d7](https://github.com/non7top/claude-chatter-mcp/commit/79b57d711db8db94411e67bcdaa646921c42df73))
* don't let a zombie daemon PID block self-healing reattachment ([4c85ee4](https://github.com/non7top/claude-chatter-mcp/commit/4c85ee4f86f76142160cc65759e0cea86c6bbda3))
* don't treat the uv-run venv bin as on the agent shell's PATH ([be05cca](https://github.com/non7top/claude-chatter-mcp/commit/be05ccae370c95d00ba13cff0495527f39203825))
* enable_push accepts and ignores a session_id argument ([a39bea9](https://github.com/non7top/claude-chatter-mcp/commit/a39bea9cdcfd23b2142d4c3c251e96df85a6f183))
* give the prime command a stable path so it can be allow-listed ([234d6b2](https://github.com/non7top/claude-chatter-mcp/commit/234d6b2d80ff6a2135cd3fd14e114bfdadd5ad3a))
* guard stdio write_response against broken host pipe ([40dedf8](https://github.com/non7top/claude-chatter-mcp/commit/40dedf87c7b6aab446e24ca798b5c2ebb8dceeca))
* handle SIGTERM for a clean exit in the default MCP stdio mode ([6cfd33e](https://github.com/non7top/claude-chatter-mcp/commit/6cfd33ed2b6b5aca4f164286c529e8bc2ce96527))
* **inbox:** composite primary key, bare cli resolution and consistent ordering ([97975e9](https://github.com/non7top/claude-chatter-mcp/commit/97975e9850112e3738a8d025be284d7a2beb90d8))
* keep a strong reference to the tools/list_changed notification task ([4b22c7a](https://github.com/non7top/claude-chatter-mcp/commit/4b22c7a17410cf809bda7a60f0c4c0ebe159e5ce))
* make the agy-side bridge daemon persistent across MCP host restarts ([f9593f0](https://github.com/non7top/claude-chatter-mcp/commit/f9593f068d8b4e1a7ad6d29fa80d79624f3881de))
* map other hosts' parameter names onto tool arguments and drop unknown ones ([86a4351](https://github.com/non7top/claude-chatter-mcp/commit/86a43514cd8e347fec2ad8fd7368ef2db8d13cf9))
* match the real Claude Code cross-session wire format on send ([9bf5454](https://github.com/non7top/claude-chatter-mcp/commit/9bf5454aac5e9020a1605263a3ca08877b03e1b6))
* persist a random-suffixed session name per project path, resolved from the agy process ([f80c9a9](https://github.com/non7top/claude-chatter-mcp/commit/f80c9a971c1809bb8acff61410e80982b76aa305))
* **protocol:** allocate fresh session id for concurrent clashing sessions ([95acd3d](https://github.com/non7top/claude-chatter-mcp/commit/95acd3db0c36c342bbfc89e9b1003bfb44db459d))
* register real machine-id/PID-namespace/procStart in session descriptors ([e2d9a12](https://github.com/non7top/claude-chatter-mcp/commit/e2d9a1293f621bd3faadeb65f90864caca9a0388))
* remove transcript-scraping response extraction, bump Docker base to 3.14 ([6ae3333](https://github.com/non7top/claude-chatter-mcp/commit/6ae33335c5ceb79e02fd4851342f627a090405f8))
* restrict socket poke backs to background sessions to prevent interactive TTY crashes ([dfa8888](https://github.com/non7top/claude-chatter-mcp/commit/dfa88887f9aaeccc39ed029ff1036021988449b1))
* run socket listener directly in stdio process to ensure stdout host notifications poke Antigravity ([e92b485](https://github.com/non7top/claude-chatter-mcp/commit/e92b4853992675e72c255a313edc790a4cd1ecbf))
* shorten the plugin's MCP server name to avoid claude-chatter_claude-chatter tool names ([2933b5a](https://github.com/non7top/claude-chatter-mcp/commit/2933b5a5a0ac20448cdb19b746ab7dd911ee1a0f))
* shorter prime command, and find agy's executable after a self-update ([44c376b](https://github.com/non7top/claude-chatter-mcp/commit/44c376b3ad98301142f26cbab7e782981c9e812d))
* stop acking inbound frames so Claude Code's SendMessage reports success ([eb70d89](https://github.com/non7top/claude-chatter-mcp/commit/eb70d89cfb764a506fa09bdd90bf173f5605d368))
* stop acking inbound frames so Claude Code's SendMessage reports success ([c219b12](https://github.com/non7top/claude-chatter-mcp/commit/c219b1274d7c54db28d02c2d5da2355a89e34486))
* stop saying nothing can wake the host now that push exists ([728ad26](https://github.com/non7top/claude-chatter-mcp/commit/728ad2611e2a92226523c4de493f906b32c96ddb))
* stop saying nothing can wake the host now that push exists ([30fab8d](https://github.com/non7top/claude-chatter-mcp/commit/30fab8de7d74cf36b42a7a1a196663df754e2aab))
* **storage:** add read-only store mode and resolve live bridge descriptors ([479daca](https://github.com/non7top/claude-chatter-mcp/commit/479daca16257b25a26148be78160c77af5bd02d6))
* **storage:** address peer review items A-F on session identity and migration ([8ca6657](https://github.com/non7top/claude-chatter-mcp/commit/8ca665784de7c520c3283e21d952cfea1695c827))
* **storage:** test real WAL DB for read-only inspect and document empty sidecars ([2f8fdd3](https://github.com/non7top/claude-chatter-mcp/commit/2f8fdd3232cc4eb114aa87c646e21893dd852aad))
* strip the hardcoded auto-reply Pong scaffold ([39beaa9](https://github.com/non7top/claude-chatter-mcp/commit/39beaa99f87578988dc7b07eeac9102ee4ec59eb))


### Reverts

* remove auto-spawned persistent daemon architecture ([1b9edf2](https://github.com/non7top/claude-chatter-mcp/commit/1b9edf2fbc784731646bee3440f95dc93885e5bf))
