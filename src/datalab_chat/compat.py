from __future__ import annotations

try:
    from enum import StrEnum
except ImportError:  # Python 3.10
    from enum import Enum

    class StrEnum(str, Enum):
        """Backport the small part of enum.StrEnum used by the application."""

        def __str__(self) -> str:
            return str(self.value)


__all__ = ["StrEnum"]
