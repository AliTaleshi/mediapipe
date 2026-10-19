import random
from collections.abc import Callable


def backoff_delay(
    attempt: int, base: float, cap: float, rand: Callable[[], float] = random.random
) -> float:
    """Exponential backoff with jitter: between half and the full exponential delay."""
    exp = min(cap, base * 2 ** max(attempt - 1, 0))
    return float(exp / 2 + rand() * exp / 2)
