"""Repositories — thin SQL data-access, one module per concern.

Each repo wraps the shared sqlite connection (Database.conn) and holds only
queries. Atomicity across the read-modify-write in the send decision is provided
by the pipeline's asyncio.Lock, not by these classes.
"""
