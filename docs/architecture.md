# Architecture and test seams

DataLab Risk Chat runs as one local Python process. A standard-library HTTP adapter serves a prebuilt vanilla-JavaScript UI and translates JSON requests into the typed interface of the application module. The application module owns all ordering, validation and failure semantics.

The agreed test seams are:

1. `ProfileCatalog` for durable model-profile behavior in a temporary `.env` file.
2. `SQLiteChatMemory` for transactional branch, snapshot and recovery behavior against a temporary real SQLite file.
3. `GenerationCoordinator` for deadline, retry, cancellation and chunk-boundary behavior with a deterministic LLM adapter.
4. `ChatApplication` for ordered user intents spanning profiles, memory and generation scheduling.
5. The localhost HTTP interface for request validation, secret redaction and end-to-end user actions.
6. The browser modules for safe Markdown rendering and interaction state.

Tests replace only the true external LLM seam. SQLite and the filesystem are exercised through their public modules with temporary real files. Tests never inspect private implementation state when the same result is observable through an interface.

## Module shape

- The **application module** exposes user-intent methods and hides transactions, branch selection, profile resolution, FIFO activation and generation scheduling.
- The **profile module** exposes safe profile summaries and an internal resolved connection; it hides parsing, atomic `.env` replacement and file permissions.
- The **memory module** exposes transactional conversation operations, durable queued messages, generation status and an idempotent recovery operation; it hides SQLite schema, recursive branch queries and migrations. A queued message enters the branch only when the previous turn is terminal, so its LLM context includes the preceding assistant answer.
- The **generation module** exposes scheduling, cancellation and bounded execution (four generation tasks and four provider calls by default); it hides retries, the ten-minute deadline, background threads and response persistence.
- GigaChat and OpenAI-compatible **adapters** satisfy the same LLM interface at the true external seam. Its optional chunk callback already crosses the bounded execution queue, while current request–response adapters emit one complete chunk only after strict validation. A deterministic adapter occupies that seam in tests.
- The **web module** is deliberately shallow glue. It owns HTTP concerns only and has no business decisions.
