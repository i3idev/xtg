"""Performance benchmarks for the collection pipeline.

Run every scenario::

    uv run xtg bench

Run one scenario, or shrink the workload::

    uv run xtg bench --scenario A --rows 500

Save a baseline and compare against it later::

    uv run xtg bench --save baseline.json
    uv run xtg bench --compare baseline.json
"""
