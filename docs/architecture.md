# Architecture and test seams

DataLab Risk Chat runs as one local Python process. A standard-library HTTP adapter serves a prebuilt vanilla-JavaScript UI and translates JSON requests into the typed interface of the application module. The application module owns all ordering, validation and failure semantics.

The public test seams are:

1. `ProfileCatalog` for durable model-profile behavior in a temporary `.env` file.
2. `ChatApplication` for conversations, branching, generations and recovery, using real temporary SQLite and `.env` files plus a deterministic LLM adapter.
3. The localhost HTTP interface for request validation, secret redaction and end-to-end user actions.
4. The browser modules for safe Markdown rendering and interaction state.

Tests replace only the true external LLM seam. SQLite and the filesystem are exercised through their public modules with temporary real files. Tests never inspect private implementation state when the same result is observable through an interface.

## Module shape

- The **application module** exposes user-intent methods and hides transactions, branch selection, profile resolution and generation scheduling.
- The **profile module** exposes safe profile summaries and an internal resolved connection; it hides parsing, atomic `.env` replacement and file permissions.
- The **memory module** exposes conversation operations; it hides SQLite schema, recursive branch queries, migrations and crash recovery.
- The **generation module** exposes start, status and cancel; it hides retry classification, the ten-minute deadline, background threads and response persistence.
- GigaChat and OpenAI-compatible **adapters** satisfy the same LLM interface at the true external seam. A deterministic adapter occupies that seam in tests.
- The **web module** is deliberately shallow glue. It owns HTTP concerns only and has no business decisions.

