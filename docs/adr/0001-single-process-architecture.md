# Use a single Python process with prebuilt browser assets

Local LLM Chat UI uses one standard-library Python HTTP process, SQLite, a generated `.env` profile block and prebuilt vanilla-JavaScript assets. This keeps deployment and operation simple by avoiding a runtime Node.js process and a web framework, while the application and LLM seams preserve testability and future streaming support.
