"""Reference-DB service layer.

Houses the seed loader, validation helpers, and (eventually) cached
lookup of jurisdiction master data. Keep this folder thin — these are
not domain services, they are the wiring between the reference DB and
the rest of the app.
"""
