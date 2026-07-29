# Use a single Python process with prebuilt browser assets

DataLab Risk Chat uses one standard-library Python HTTP process, SQLite, a generated `.env` profile block and prebuilt vanilla-JavaScript assets. This deliberately rejects a runtime Node.js process and a web framework: the closed-network deployment values an auditable offline bundle and one-command recovery more than framework convenience, while the application and LLM seams preserve testability and future streaming support.
