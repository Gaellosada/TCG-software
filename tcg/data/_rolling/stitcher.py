"""ContinuousSeriesBuilder — the main entry point for building continuous futures."""

from __future__ import annotations

import numpy as np

from tcg.types.market import (
    AdjustmentMethod,
    ContinuousRollConfig,
    ContinuousSeries,
    ContractPriceData,
    PriceSeries,
)

from tcg.data._rolling.adjustment import adjust_difference, adjust_ratio
from tcg.data._rolling.calendar import compute_roll_dates, trim_overlaps


class ContinuousSeriesBuilder:
    """Builds a continuous futures series from individual contract data."""

    def build(
        self,
        contracts: list[ContractPriceData],
        config: ContinuousRollConfig,
        collection: str = "",
    ) -> ContinuousSeries:
        """Build a continuous series from ordered contracts.

        Parameters
        ----------
        contracts
            Individual contract price data, sorted by expiration (ascending).
        config
            Roll strategy and adjustment method configuration.
        collection
            Logical collection name for the output series.

        Returns
        -------
        ContinuousSeries with stitched prices and roll metadata.
        """
        # Filter out contracts with no data
        contracts = [c for c in contracts if len(c.prices) > 0]

        # Contracts must be sorted by expiration for correct roll sequencing
        for i in range(len(contracts) - 1):
            if contracts[i].expiration > contracts[i + 1].expiration:
                raise ValueError(
                    f"Contracts not sorted by expiration: "
                    f"{contracts[i].contract_id} ({contracts[i].expiration}) > "
                    f"{contracts[i + 1].contract_id} ({contracts[i + 1].expiration})"
                )

        if not contracts:
            return ContinuousSeries(
                collection=collection,
                roll_config=config,
                prices=PriceSeries.empty(),
                roll_dates=(),
                contracts=(),
            )

        if len(contracts) == 1:
            cleaned = trim_overlaps(contracts, [])
            if not cleaned:
                return ContinuousSeries(
                    collection=collection,
                    roll_config=config,
                    prices=PriceSeries.empty(),
                    roll_dates=(),
                    contracts=(),
                )
            return ContinuousSeries(
                collection=collection,
                roll_config=config,
                prices=cleaned[0].prices,
                roll_dates=(),
                contracts=(cleaned[0].contract_id,),
            )

        # 1. Compute roll dates
        roll_schedule = compute_roll_dates(
            contracts, config.strategy, config.roll_offset_days
        )

        # 2. Trim overlaps (also strips zero-close rows)
        trimmed = trim_overlaps(contracts, roll_schedule)

        if not trimmed:
            return ContinuousSeries(
                collection=collection,
                roll_config=config,
                prices=PriceSeries.empty(),
                roll_dates=(),
                contracts=(),
            )

        # 3. Concatenate (dedup may subsume some contracts entirely)
        raw_series, actual_roll_dates, surviving_idx = self._concatenate(trimmed)

        # Only contracts that survived dedup are relevant for adjustment
        surviving = [trimmed[i] for i in surviving_idx]

        # 4. Apply adjustment (surviving contracts aligned with actual_roll_dates)
        match config.adjustment:
            case AdjustmentMethod.RATIO:
                adjusted = adjust_ratio(
                    raw_series, actual_roll_dates, surviving
                )
            case AdjustmentMethod.DIFFERENCE:
                adjusted = adjust_difference(
                    raw_series, actual_roll_dates, surviving
                )
            case _:
                adjusted = raw_series

        # 5. Return ContinuousSeries
        return ContinuousSeries(
            collection=collection,
            roll_config=config,
            prices=adjusted,
            roll_dates=tuple(actual_roll_dates),
            contracts=tuple(c.contract_id for c in surviving),
        )

    def _concatenate(
        self,
        trimmed: list[ContractPriceData],
    ) -> tuple[PriceSeries, list[int], list[int]]:
        """Concatenate trimmed contracts into a single PriceSeries.

        Deduplicates dates: if two contracts have data on the same date,
        the later contract's data is kept. This means some contracts may be
        entirely subsumed by a later contract and contribute no rows.

        Returns
        -------
        (concatenated_series, roll_dates, surviving_indices) where:
        - roll_dates are the first date of each new contract segment
        - surviving_indices are the original indices into `trimmed` of
          contracts that actually contribute data (len(roll_dates) == len(surviving) - 1)
        """
        if len(trimmed) == 1:
            return trimmed[0].prices, [], [0]

        all_dates: list[np.ndarray] = []
        all_open: list[np.ndarray] = []
        all_high: list[np.ndarray] = []
        all_low: list[np.ndarray] = []
        all_close: list[np.ndarray] = []
        all_volume: list[np.ndarray] = []
        # Track which contract each row belongs to (for dedup: keep later)
        all_contract_idx: list[np.ndarray] = []

        for i, contract in enumerate(trimmed):
            ps = contract.prices
            n = len(ps)
            all_dates.append(ps.dates)
            all_open.append(ps.open)
            all_high.append(ps.high)
            all_low.append(ps.low)
            all_close.append(ps.close)
            all_volume.append(ps.volume)
            all_contract_idx.append(np.full(n, i, dtype=np.int64))

        cat_dates = np.concatenate(all_dates)
        cat_open = np.concatenate(all_open)
        cat_high = np.concatenate(all_high)
        cat_low = np.concatenate(all_low)
        cat_close = np.concatenate(all_close)
        cat_volume = np.concatenate(all_volume)
        cat_idx = np.concatenate(all_contract_idx)

        # Deduplicate: for duplicate dates, keep the row from the later contract
        # Sort by (date, contract_idx) so later contract comes last
        sort_order = np.lexsort((cat_idx, cat_dates))
        cat_dates = cat_dates[sort_order]
        cat_open = cat_open[sort_order]
        cat_high = cat_high[sort_order]
        cat_low = cat_low[sort_order]
        cat_close = cat_close[sort_order]
        cat_volume = cat_volume[sort_order]
        cat_idx = cat_idx[sort_order]

        # For duplicate dates, keep the LAST occurrence (later contract)
        # np.unique with return_index gives first occurrence; we want last.
        # Reverse, unique, reverse back.
        _, unique_idx = np.unique(cat_dates[::-1], return_index=True)
        # Convert reversed indices back to forward indices
        keep = len(cat_dates) - 1 - unique_idx
        keep = np.sort(keep)

        final_dates = cat_dates[keep]
        final_open = cat_open[keep]
        final_high = cat_high[keep]
        final_low = cat_low[keep]
        final_close = cat_close[keep]
        final_volume = cat_volume[keep]
        final_idx = cat_idx[keep]

        # Compute roll dates and surviving contract indices
        roll_dates: list[int] = []
        surviving: list[int] = [int(final_idx[0])]
        for j in range(1, len(final_idx)):
            if final_idx[j] != final_idx[j - 1]:
                roll_dates.append(int(final_dates[j]))
                surviving.append(int(final_idx[j]))

        series = PriceSeries(
            dates=final_dates,
            open=final_open,
            high=final_high,
            low=final_low,
            close=final_close,
            volume=final_volume,
        )

        return series, roll_dates, surviving
