"""Deterministic, configured model pricing. No price lives anywhere else in the code.

A price applies from `effective_from` until a later entry for the same provider/model.
Costs are computed at call time and persisted in audit events, so adding a new price
never rewrites the cost of past calls. Missing price or unknown usage means unknown cost.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opspilot.domain import Usage

MILLION = Decimal(1_000_000)


class ModelPricing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    model: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    input_cost_per_million: Decimal = Field(ge=0)
    output_cost_per_million: Decimal = Field(ge=0)
    effective_from: datetime

    @model_validator(mode="after")
    def aware(self) -> "ModelPricing":
        if self.effective_from.tzinfo is None:
            raise ValueError("effective_from must include a timezone")
        return self


class PricingTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prices: tuple[ModelPricing, ...] = ()

    @model_validator(mode="after")
    def unique(self) -> "PricingTable":
        keys = [(p.provider, p.model, p.effective_from) for p in self.prices]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate provider/model/effective_from")
        return self

    def price(self, provider: str, model: str, at: datetime) -> ModelPricing | None:
        candidates = [
            p
            for p in self.prices
            if p.provider == provider and p.model == model and p.effective_from <= at
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda entry: entry.effective_from)

    def cost(self, provider: str, model: str, usage: Usage, at: datetime) -> Decimal | None:
        """USD cost, or None when the price or either token count is unknown."""
        price = self.price(provider, model, at)
        if price is None or usage.input_tokens is None or usage.output_tokens is None:
            return None
        return (
            Decimal(usage.input_tokens) * price.input_cost_per_million
            + Decimal(usage.output_tokens) * price.output_cost_per_million
        ) / MILLION
