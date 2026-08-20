"""Microsoft Fabric / OneLake integration.

Fabric Spark is CPU-only, so this package covers the hand-off rather than
training: reading Lakehouse Delta tables, and exporting JSONL into OneLake
`Files/` where an Azure ML `OneLakeDatastore` can pick it up (that datastore
supports `Files/` only, not `Tables/`).
"""
