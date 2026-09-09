# Research module (proposed, offline-first)

The research module (`pdf_craft_tool/research/`) is **separate** from the
production library (`pdf_craft/`) and the production CLI
(`pdf_craft_tool/cli.py`). It never imports production queue, service or
worker code; it works offline over frozen manifests, synthetic fixtures and
saved predictions. Run it as:

```bash
python -m pdf_craft_tool.research <subcommand> --help
```

## Example subcommands (proposed)

| Subcommand | Role |
| --- | --- |
| `inventory` | corpus audit over read-only sources → JSON |
| `sample` | grouping + probability sample → frozen manifest |
| `validate` | reload every manifest/record in a study root, recheck hashes |
| `annotate` | print the loopback annotation-server launch command |
| `run` | frozen baselines; dry/mock unless `--allow-execution` is given |
| `fit-gate` | fit on train, threshold on calibration; refuses test rows |
| `evaluate` | score saved predictions against gold |
| `report` | rebuild markdown + json tables offline from saved records |
| `export` | build a checksummed release bundle with documented decisions |

Annotation, inference and scoring stay in separate subcommands: no
subcommand performs two roles, and none mutates production queues.

## Study-root layout

Generated data and results live under
`pdf-craft-output/research/<study-id>/`:

```text
pdf-craft-output/research/<study-id>/
  sample_manifest.json   frozen probability sample (immutable)
  predictions.jsonl      saved inference-only predictions
  gold.json              adjudicated gold (scoring side only)
  records.jsonl          scored records for offline rebuilds
  evaluation.json        baseline table over saved predictions
  report.md / report.json  reconciled study report
```

## Leakage rules

- Gold fields never enter inference records (`schema.assert_no_gold_fields`
  is enforced by runners, manifests and the export gate).
- Gate fitting sees `train` rows only; threshold tuning sees `calibration`
  rows only; `test` rows in either path are refused.
- Inference bundles exclude gold files, private source paths, secrets and
  hidden-test labels; every exported asset is checksummed and re-verified,
  and out-of-root asset references are refused.
- Gold bundles are allowed only for items with a documented release basis.

## Further reading

- `docs/en/RESEARCH_PROTOCOL.md` — frozen study protocol and endpoints.
- `docs/en/RESEARCH_ANNOTATION.md` — blind annotation and adjudication.

No subcommand starts training, downloads models, allocates cluster work or
publishes a release.
