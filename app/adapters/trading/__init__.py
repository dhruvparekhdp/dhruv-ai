"""Read-only trading-v1 clients for the crypto and NSE engines.

Both speak GET /integration/v1 and the shared envelope. Neither can submit,
cancel, or amend an order.
"""

from app.adapters.trading.crypto import build_crypto_adapter
from app.adapters.trading.nse import build_nse_adapter

__all__ = ["build_crypto_adapter", "build_nse_adapter"]
