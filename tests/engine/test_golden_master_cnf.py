"""G1 golden-master regression: zero-link blocks are BYTE-IDENTICAL.

The fixture ``golden_master_cnf.npz`` was frozen from the engine BEFORE the
temporal-composition change (see ``gen_golden_master.py``). This test rebuilds
the identical corpus via ``_golden_corpus.build_corpus`` and asserts the
(now-modified) engine produces exactly the same ``index``/``positions`` arrays
(``np.array_equal`` incl. NaN, dtype, shape). Any drift fails G1.

To regenerate intentionally (only when the corpus itself changes, NOT to paper
over a behavior change): ``python tests/engine/gen_golden_master.py``.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from tcg.engine.signal_exec import evaluate_signal

from _golden_corpus import build_corpus  # noqa: E402  (sibling module on sys.path)

FIXTURE = pathlib.Path(__file__).with_name("golden_master_cnf.npz")


@pytest.mark.asyncio
async def test_zero_link_corpus_byte_identical():
    assert FIXTURE.exists(), (
        f"missing golden-master fixture {FIXTURE}; run "
        f"`python tests/engine/gen_golden_master.py` against the unmodified engine"
    )
    frozen = np.load(FIXTURE)
    corpus = build_corpus()
    assert corpus, "empty corpus"

    checked = 0
    for name, (signal, fetcher) in corpus.items():
        result = await evaluate_signal(signal, {}, fetcher)

        idx_key = f"{name}::index"
        assert idx_key in frozen, f"fixture missing {idx_key}"
        exp_index = frozen[idx_key]
        assert result.index.dtype == exp_index.dtype, f"{name}: index dtype drift"
        assert result.index.shape == exp_index.shape, f"{name}: index shape drift"
        assert np.array_equal(result.index, exp_index), f"{name}: index values drift"
        checked += 1

        for p in result.positions:
            pos_key = f"{name}::pos::{p.input_id}"
            pnl_key = f"{name}::pnl::{p.input_id}"
            assert pos_key in frozen, f"fixture missing {pos_key}"
            exp_pos = frozen[pos_key]
            exp_pnl = frozen[pnl_key]
            assert p.values.dtype == exp_pos.dtype, f"{pos_key}: dtype drift"
            assert p.values.shape == exp_pos.shape, f"{pos_key}: shape drift"
            assert np.array_equal(p.values, exp_pos, equal_nan=True), (
                f"{pos_key}: position values drift"
            )
            assert np.array_equal(p.realized_pnl, exp_pnl, equal_nan=True), (
                f"{pnl_key}: realized_pnl drift"
            )
            checked += 1

    # Sanity: the corpus must actually exercise a broad set of signals
    # (>= 13 indices + their positions). Guards against a silently-emptied corpus.
    assert checked >= 28, f"corpus too small ({checked} index+position arrays checked)"
