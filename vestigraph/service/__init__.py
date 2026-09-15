"""Vestigraph local service: catalog, jobs, capture supervision.

Framework-neutral: nothing here imports FastAPI. The web adapter (``vestigraph.web``)
and any future agent adapter (e.g. an MCP server) call the same
``Application`` methods and are bound by the same leases and job idempotency.
"""
