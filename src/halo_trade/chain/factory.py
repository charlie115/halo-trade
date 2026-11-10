"""Choose the chain backend from settings."""
from __future__ import annotations

from ..config import Settings
from .base import ChainBackend


def make_chain(settings: Settings) -> ChainBackend:
    """Imports stay inside the branches so mock mode never loads the Injective SDK."""
    if settings.chain_backend == "mock":
        from .mock import MockChain

        return MockChain()

    if settings.chain_backend == "injective":
        from .injective import InjectiveChain, load_grantee_key
        from .reference import ReferenceCandles, symbol_for

        if settings.candle_source not in ("", "binance"):
            raise ValueError(f"unknown candle source {settings.candle_source!r}")
        return InjectiveChain(
            network=settings.network,
            grantee_private_key=load_grantee_key(settings.grantee_key_path),
            market_ticker=settings.market_ticker,
            tx_timeout_blocks=settings.tx_timeout_blocks,
            collar=settings.risk.collar,
            allow_mainnet=settings.allow_mainnet,
            candle_network=settings.candle_network or settings.network,
            reference_candles=ReferenceCandles(symbol_for(settings.market_ticker)) if settings.candle_source else None,
            chain_rps=settings.chain_rps,
            indexer_rps=settings.indexer_rps,
        )

    raise ValueError(f"unknown chain backend {settings.chain_backend!r}")
