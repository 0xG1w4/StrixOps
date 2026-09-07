"""Outward contract layer: everything the Strix platform observes.

This package knows nothing about agents or the LLM stack. It owns the four
surfaces the platform consumes:

* run-directory naming and placement (``runname``)
* the ``events.jsonl`` stream (``events``)
* run artifacts on disk (``artifacts``)
* the operator-hints inbox (``hints``)

Every behavior here is pinned by the contract tests in ``tests/contract/``,
which replay our output through frozen copies of the platform's read-side
oracles. Change nothing in this package without updating those tests
deliberately.
"""
