"""Backend tooling for the Phase 5D/5E PostgreSQL import rehearsal.

Modules here are import-safe: importing this package must not connect to any
database, motor, or network resource. Database drivers are imported lazily by
the code that actually needs them.
"""
