"""
Pydantic models for the Countries API.
"""

from pydantic import BaseModel, Field


class CountryOut(BaseModel):
    """A single country as returned by the public API."""

    name: str = Field(..., description="Country name with flag emoji")
    country_rank: int = Field(..., description="Display order — lower rank shown first")
    dial_code: str = Field(..., description="International dialling code")
    code: str = Field(..., description="ISO 3166-1 alpha-2 country code (uppercase)")
    price: float = Field(..., description="Current purchase price in USD")
    stock: int = Field(..., description="Unsold OTP session accounts available")

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "Uzbekistan 🇺🇿",
                "country_rank": 40,
                "dial_code": "+998",
                "code": "UZ",
                "price": 0.80,
                "stock": 6450,
            }
        }
    }
