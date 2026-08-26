"""Compatibility marker for the removed MarketHub partial-publication writer.

Futures partial facts, metadata, identities, cursors, and lineage are owned by
QuoteMux. MarketHub's HTTP facade delegates to ``QuoteMuxPublicReader``.
"""

from __future__ import annotations


__all__: tuple[str, ...] = ()
