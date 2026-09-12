"""Backward-compatible import surface for platform variant batch creation."""
from __future__ import annotations

from .variants import VariantBatchInput as BatchInput, VariantTarget


def derive_batch(workspace, source_id: str, req: BatchInput):
    return workspace.variants.create_many(source_id, req)
