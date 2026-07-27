"""Shared validated scalar types used by agent output schemas."""

from __future__ import annotations

import math
from typing import Annotated

from pydantic import AfterValidator


def _probability(value: float) -> float:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("must be a finite probability between 0 and 1")
    return value


Probability = Annotated[float, AfterValidator(_probability)]
