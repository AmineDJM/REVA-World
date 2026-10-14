"""REVA world kernel: stable, dependency-free semantic and provenance contracts.

The kernel imports only the standard library and Pydantic. It must not import
database, HTTP, model or LLM code (enforced by import-linter).
"""
