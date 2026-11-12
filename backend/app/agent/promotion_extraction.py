"""Turn a supplier promotion written in plain English into structured fields.

The model only TRANSCRIBES what the text says (numbers as written, sizes as written, names as
written) and lists what is ambiguous. It decides nothing: matching the supplier and products
to the catalogue, converting sizes and money, and checking the terms all happen in
services/promotions.py, and the merchant confirms the result before anything uses it.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.llm_client import LLMError, StructuredLLMClient

MAX_PROMOTION_TEXT_CHARS = 2000

PromotionKind = Literal["REBATE", "BUNDLE_PRICE", "PERCENT_OFF", "OTHER"]


class ExtractedPromotion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=120)
    kind: PromotionKind
    supplier_name: str | None = Field(max_length=120)
    brand: str | None = Field(max_length=60)
    required_cases: int | None = Field(ge=1, le=1000)
    units_per_case: int | None = Field(ge=1, le=100)
    amount_usd: str | None = Field(max_length=20)
    min_distinct_flavors: int | None = Field(ge=1, le=100)
    max_bundles: int | None = Field(ge=1, le=100)
    sizes: list[str] = Field(max_length=10)
    valid_from: date | None
    valid_until: date | None
    ambiguities: list[str] = Field(max_length=10)


def _nullable(kind: str) -> dict:
    return {"type": [kind, "null"]}


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title", "kind", "supplier_name", "brand", "required_cases", "units_per_case",
        "amount_usd", "min_distinct_flavors", "max_bundles", "sizes", "valid_from",
        "valid_until", "ambiguities",
    ],
    "properties": {
        "title": {"type": "string", "description": "Short name, e.g. 'Svedka 10-case mix & match rebate'"},
        "kind": {"type": "string", "enum": ["REBATE", "BUNDLE_PRICE", "PERCENT_OFF", "OTHER"]},
        "supplier_name": {**_nullable("string"), "description": "As written; null if not named"},
        "brand": {**_nullable("string"), "description": "Brand the promotion applies to"},
        "required_cases": {**_nullable("integer"), "description": "Cases that must be bought"},
        "units_per_case": {**_nullable("integer"), "description": "Only if the text states it"},
        "amount_usd": {**_nullable("string"),
                       "description": "The rebate or price in dollars exactly as written, digits "
                                      "only, e.g. '500' or '500.00'"},
        "min_distinct_flavors": {**_nullable("integer"),
                                 "description": "Minimum number of different flavors required"},
        "max_bundles": {**_nullable("integer"),
                        "description": "Most times one order can earn the rebate, only if the "
                                       "text states a limit (e.g. 'limit 2 per store')"},
        "sizes": {"type": "array", "items": {"type": "string"},
                  "description": "Bottle sizes as written, e.g. ['750ml', '1.5L']"},
        "valid_from": {**_nullable("string"), "description": "YYYY-MM-DD, null if not stated"},
        "valid_until": {**_nullable("string"), "description": "YYYY-MM-DD, null if not stated"},
        "ambiguities": {"type": "array", "items": {"type": "string"},
                        "description": "Each thing the merchant should confirm, one short "
                                       "question each"},
    },
}


def _system_prompt(as_of: date) -> str:
    return f"""You extract the terms of ONE supplier promotion for a liquor store's buying
system. Today is {as_of.isoformat()}.

Rules:
- Transcribe only what the text says. Never invent a number, a size, a supplier or a date.
  Use null (or an empty list) when the text does not say.
- kind: REBATE if the supplier pays money back after a qualifying purchase ("rebate", "$X back",
  "for $X" in a rebate offer); BUNDLE_PRICE if the bundle simply costs a fixed price;
  PERCENT_OFF for a percentage discount; otherwise OTHER.
- "mix and match with any 3 different flavors" means min_distinct_flavors = 3.
- max_bundles: a stated limit on how many times the rebate can be earned ("limit 2 per
  account" = 2); null when the text states no limit.
- Copy sizes exactly as written ("750ml", "1.5L"); do not convert them.
- List in ambiguities anything a buyer would need to confirm (e.g. whether "$500" is a price or
  a rebate, whether flavors must come in every size, a missing end date)."""


def extract_promotion(llm: StructuredLLMClient, text: str, as_of: date) -> ExtractedPromotion:
    """Raises LLMError (provider problem or a reply that does not fit the schema)."""
    data = llm.complete_json(
        [{"role": "system", "content": _system_prompt(as_of)}, {"role": "user", "content": text}],
        name="supplier_promotion", schema=SCHEMA,
    )
    try:
        return ExtractedPromotion.model_validate(data)
    except ValidationError as exc:
        raise LLMError(
            "LLM_BAD_RESPONSE", "The AI service sent promotion terms we could not read.",
            detail=f"{exc.error_count()} validation error(s)",
        ) from exc
