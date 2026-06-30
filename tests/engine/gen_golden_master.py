"""Generate the golden-master fixture from the CURRENT (unmodified) engine.

Run this BEFORE editing any engine code:

    python tests/engine/gen_golden_master.py

It evaluates every signal in ``_golden_corpus.build_corpus()`` with the engine
*as it stands now* and freezes ``(index, [positions...])`` per signal to
``tests/engine/golden_master_cnf.npz``. The regression test
(``test_golden_master_cnf.py``) reloads this and asserts byte-identical output
from the modified engine. Sequencing is load-bearing: the baseline MUST come
from the unmodified engine, so this script is committed and the fixture is
regenerated only intentionally.
"""

from __future__ import annotations

import asyncio
import pathlib

import numpy as np

from tcg.engine.signal_exec import evaluate_signal

from _golden_corpus import build_corpus  # noqa: E402  (sibling module)

FIXTURE = pathlib.Path(__file__).with_name("golden_master_cnf.npz")


async def _run() -> dict[str, np.ndarray]:
    corpus = build_corpus()
    flat: dict[str, np.ndarray] = {}
    for name, (signal, fetcher) in corpus.items():
        result = await evaluate_signal(signal, {}, fetcher)
        flat[f"{name}::index"] = result.index
        # Stable, deterministic ordering of positions by input_id.
        for p in result.positions:
            flat[f"{name}::pos::{p.input_id}"] = p.values
            flat[f"{name}::pnl::{p.input_id}"] = p.realized_pnl
    return flat


def main() -> None:
    flat = asyncio.run(_run())
    np.savez(FIXTURE, **flat)
    keys = sorted(flat.keys())
    print(f"wrote {FIXTURE} with {len(keys)} arrays")
    for k in keys:
        arr = flat[k]
        n_nan = int(np.isnan(arr).sum()) if arr.dtype.kind == "f" else 0
        print(f"  {k:48s} shape={arr.shape} dtype={arr.dtype} nan={n_nan}")


if __name__ == "__main__":
    main()
