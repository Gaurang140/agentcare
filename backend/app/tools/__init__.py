"""Real DB-backed tools called by agent nodes and API routes.

Every tool takes `db: Session` first and returns a plain dict (or list of
dicts) so agents can serialize results directly. Every mutating tool calls
`write_audit` and commits its own transaction.
"""
