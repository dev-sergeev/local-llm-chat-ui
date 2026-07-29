from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from datalab_chat import __version__
from datalab_chat.application import ChatApplication
from datalab_chat.gateways import LangChainGatewayFactory
from datalab_chat.memory import SQLiteChatMemory
from datalab_chat.profiles import EnvProfileCatalog
from datalab_chat.web import create_server


LOGGER = logging.getLogger("datalab_chat")
EXTERNAL_LOGGER_PREFIXES = (
    "gigachat",
    "httpcore",
    "httpx",
    "langchain",
    "langchain_core",
    "langchain_gigachat",
    "langchain_openai",
    "langsmith",
    "openai",
    "requests",
    "urllib3",
)


class _ExternalLibraryLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not any(
            record.name == prefix or record.name.startswith(f"{prefix}.")
            for prefix in EXTERNAL_LOGGER_PREFIXES
        )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).expanduser().resolve()
    env_file = Path(args.env_file).expanduser().resolve()

    try:
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(data_dir, 0o700)
    except OSError:
        print("Не удалось создать локальный каталог данных.", file=sys.stderr)
        return 2

    _configure_logging(data_dir)
    application: ChatApplication | None = None
    server = None
    try:
        application = ChatApplication(
            EnvProfileCatalog(env_file),
            SQLiteChatMemory(data_dir / "chat.db"),
            LangChainGatewayFactory(),
        )
        static_dir = Path(__file__).with_name("static")
        server = create_server(
            application,
            static_dir=static_dir,
            host=args.host,
            port=args.port,
        )
        actual_port = server.server_address[1]
        display_host = "127.0.0.1" if args.host == "localhost" else args.host
        url = f"http://{display_host}:{actual_port}"
        print(f"DataLab Risk Chat запущен: {url}", flush=True)
        LOGGER.info("Service started on localhost port %s", actual_port)
        if not args.no_browser:
            timer = threading.Timer(0.45, _open_browser_safely, args=(url,))
            timer.daemon = True
            timer.start()

        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
        return 0
    except OSError as exc:
        LOGGER.error("Startup failed: %s", type(exc).__name__)
        print(
            f"Не удалось запустить localhost:{args.port}. Проверьте, свободен ли порт.",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        LOGGER.error("Startup failed: %s", type(exc).__name__)
        print(
            "Не удалось подготовить локальный сервис. Проверьте .data и .env.",
            file=sys.stderr,
        )
        return 2
    finally:
        if server is not None:
            try:
                server.server_close()
            except Exception:
                pass
        if application is not None:
            try:
                application.shutdown()
            except Exception:
                pass
        LOGGER.info("Service stopped")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datalab-risk-chat",
        description="Локальный UI для GigaChat и OpenAI-compatible моделей.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--host",
        choices=("127.0.0.1", "localhost"),
        default="127.0.0.1",
        help="loopback-адрес (по умолчанию 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=_environment_port(),
        help="локальный порт (по умолчанию 8765)",
    )
    parser.add_argument(
        "--data-dir",
        default=".data",
        help="каталог SQLite и логов (по умолчанию .data)",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="локальный файл профилей (по умолчанию .env)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="не открывать браузер автоматически",
    )
    return parser


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("порт должен быть числом") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("порт должен быть от 0 до 65535")
    return port


def _environment_port() -> int:
    value = os.environ.get("DATALAB_PORT", "8765")
    try:
        return _port(value)
    except argparse.ArgumentTypeError:
        return 8765


def _configure_logging(data_dir: Path) -> None:
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        log_path = data_dir / "app.log"
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=1_000_000,
            backupCount=2,
            encoding="utf-8",
        )
        os.chmod(log_path, 0o600)
        handlers.append(file_handler)
    except OSError:
        pass
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(_ExternalLibraryLogFilter())
    logger.handlers.clear()
    logger.handlers.extend(handlers)


def _open_browser_safely(url: str) -> None:
    try:
        webbrowser.open(url, new=1, autoraise=True)
    except Exception:
        LOGGER.info("Browser was not opened automatically")


def _raise_keyboard_interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    raise SystemExit(main())
