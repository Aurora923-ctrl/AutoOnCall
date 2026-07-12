# Capability Boundaries

- `offline_fixture` proves deterministic regression behavior, not production accuracy.
- `local_live` proves real local services or adapters were exercised.
- `controlled_fault` proves a bounded, recoverable sandbox experiment.
- `production` requires real production incident samples.
- When production samples are absent or insufficient, the scorecard must show
  `production: not_enough_data`.
- Controlled-fault diagnosis or recovery latency must never be renamed production MTTD/MTTR.
- Missing modules remain `missing`; artifacts from another run or commit are not substituted.
