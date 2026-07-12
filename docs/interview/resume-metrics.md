# Resume Metric Rules

Only use a metric when its scorecard module records the run ID, evidence level, sample count,
raw artifact path, and failed-case detail.

Recommended wording:

> Built a provenance-rich RAG and AIOps evaluation system over multi-format knowledge assets,
> reporting retrieval, answer-quality, RCA, safety, and latency metrics from reproducible
> benchmark runs, with controlled local fault experiments and explicit production boundaries.

Do not claim production accuracy, production MTTD/MTTR improvement, or stable concurrency
capacity unless the scorecard contains sufficient `production` or formal load-test samples.
