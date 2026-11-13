"""
Wake an idle Antigravity (agy) conversation from another process.

Kept free of imports on purpose: the PreInvocation hook runs before every model
invocation and must stay cheap.
"""
