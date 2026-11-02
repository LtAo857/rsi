# RSI development

Runtime and tests use Python standard library only, Python >= 3.10.
The `agent/` and `evolution/` trees seed mutable generations. The `kernel/`
tree owns scoring, source manifests, activation and rollback and must never be
included in candidate editing scope. Changes to kernel code require lifecycle tests.

Preserve unrelated experiments. Do not run paid model experiments unless requested.
Use `python -m unittest discover -s tests -v` to verify the offline lifecycle.
Offline demo patches are scripted fixtures: never describe their scores as evidence
of autonomous discovery. Keep runtime limitations explicit in README.
