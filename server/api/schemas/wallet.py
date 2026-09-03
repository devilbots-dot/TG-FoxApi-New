"""Pydantic models for the Wallet API."""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from server.utils.database.userdb import SUPPORTED_NETWORKS


class WalletBalanceOut(BaseModel):
    balance: Decimal = Field(..., description="Current spendable balance (USD)", examples=["250.40"])
    currency: str = Field(default="USD")

    model_config = {"json_schema_extra": {"example": {"balance": "250.40", "currency": "USD"}}}


class WalletAddressOut(BaseModel):
    trc20: Optional[str] = Field(None, description="USDT TRC20 withdrawal address")
    bep20: Optional[str] = Field(None, description="USDT BEP20 withdrawal address")


class SetAddressIn(BaseModel):
    network: str = Field(..., description="TRC20 or BEP20")
    address: str = Field(..., min_length=10, description="Your wallet address for this network")

    @field_validator("network")
    @classmethod
    def validate_network(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in SUPPORTED_NETWORKS:
            raise ValueError(f"Unsupported network '{v}'. Must be one of: {', '.join(SUPPORTED_NETWORKS)}")
        return v

    @field_validator("address")
    @classmethod
    def strip_address(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Address cannot be empty.")
        return v


class WithdrawIn(BaseModel):
    amount: float = Field(..., description="Amount in USD to withdraw", ge=5)
    network: str = Field(..., description="TRC20 or BEP20 — must have address saved")

    @field_validator("network")
    @classmethod
    def validate_network(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in SUPPORTED_NETWORKS:
            raise ValueError(f"Unsupported network '{v}'. Must be one of: {', '.join(SUPPORTED_NETWORKS)}")
        return v

    @field_validator("amount")
    @classmethod
    def positive_amount(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Amount must be greater than 0.")
        return round(v, 4)


class WalletOverviewOut(BaseModel):
    balance: Decimal
    currency: str = "USD"
    addresses: WalletAddressOut
    deposit_methods: list[str]
    withdraw_networks: list[str]
    min_deposit: float
    min_withdrawal: float
