# artifacts/

Generated, machine-local data that should **not** be version-controlled:
serialised datasets (`*.pkl` replay caches), model dumps, exported reports/CSVs.

Everything in here is gitignored except this README (`artifacts/*` +
`!artifacts/README.md`). Regenerate contents locally; never commit them.

Existing generated files still live next to their code (e.g.
`dashboard/replay_data_5y*.pkl`) — those were untracked from git but not yet
physically moved here. Relocating them into `artifacts/` is part of the planned
folder reorg (post IBKR live-verification), since the paths are referenced in
code (`replay.py`).
