"""Bottle sizes written the way people write them ("750ml", "1.5L", "70cl", "1.75 liters")."""

import re
from decimal import Decimal

_ML_PER_UNIT = {
    "ml": Decimal(1), "milliliter": Decimal(1), "millilitre": Decimal(1),
    "cl": Decimal(10),
    "l": Decimal(1000), "ltr": Decimal(1000), "liter": Decimal(1000), "litre": Decimal(1000),
    "oz": Decimal("29.5735"),
}
SIZE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(milliliters?|millilitres?|ml|cl|liters?|litres?|ltr|l|oz)\b", re.IGNORECASE
)


def to_ml(amount: str, unit: str) -> int:
    unit = unit.lower().rstrip("s") if unit.lower() not in ("ml", "cl", "oz") else unit.lower()
    return int((Decimal(amount) * _ML_PER_UNIT[unit]).to_integral_value())


def find_sizes_ml(text: str) -> list[int]:
    """Every size mentioned in `text`, in millilitres, in order of appearance."""
    return [to_ml(amount, unit) for amount, unit in SIZE_PATTERN.findall(text)]


def strip_sizes(text: str) -> str:
    return SIZE_PATTERN.sub(" ", text)


def format_size(size_ml: int) -> str:
    return f"{Decimal(size_ml) / 1000:g}L" if size_ml >= 1000 else f"{size_ml}ml"
