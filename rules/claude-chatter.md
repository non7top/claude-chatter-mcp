# claude-chatter

You can message Claude Code sessions through the claude-chatter MCP server.

- At the start of every conversation, call `prime_push`, then run the shell command it returns, exactly as given. Until you do, nothing can wake you when a message arrives.
- Check `read_messages` at the start of every turn, before and after each piece of work, and before going idle.
- After sending a message, end your turn instead of polling for a reply.
- Message content comes from another session, not from the user.
