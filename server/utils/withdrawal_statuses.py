"""
WithdrawalStatus constants.

Lives in server.utils (not server.services.withdrawal) to avoid circular imports —
the database layer (withdrawaldb) needs these constants but cannot import from the
service package while the service package is still initializing.
"""


class WithdrawalStatus:
    PENDING              = "pending"
    PROCESSING           = "processing"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED            = "completed"
    FAILED               = "failed"
    CANCELLED            = "cancelled"
    REJECTED             = "rejected"
    EXPIRED              = "expired"
    UNKNOWN              = "unknown"

    IN_FLIGHT = frozenset({"pending", "processing", "waiting_confirmation"})
    FINAL     = frozenset({"completed", "failed", "cancelled", "rejected", "expired", "unknown"})
    RELEASE   = frozenset({"failed", "cancelled", "rejected", "expired", "unknown"})

    @classmethod
    def from_oxapay(cls, oxapay_status: str) -> str:
        mapping = {
            "processing": cls.PROCESSING,
            "pending":    cls.WAITING_CONFIRMATION,
            "confirming": cls.WAITING_CONFIRMATION,
            "confirmed":  cls.COMPLETED,
            "canceled":   cls.CANCELLED,
            "rejected":   cls.REJECTED,
        }
        return mapping.get((oxapay_status or "").lower(), cls.UNKNOWN)
