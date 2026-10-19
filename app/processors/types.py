"""Shared shapes for processors. Processors work on local files only: no DB, queue or storage."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ProgressCallback = Callable[[int], None]  # 0-100, monotonic


@dataclass(frozen=True)
class ProcessorInput:
    path: Path
    content_type: str
    workdir: Path


@dataclass(frozen=True)
class ProducedFile:
    """One output file, which becomes a ``Variant``."""

    name: str
    path: Path
    content_type: str
    width: int | None = None
    height: int | None = None
    duration: float | None = None


@dataclass
class ProcessingResult:
    outputs: list[ProducedFile] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def noop_progress(_: int) -> None:
    return None
