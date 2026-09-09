"""Research study command line (S7).

Separate module entry point for offline-first research work::

    python -m pdf_craft_tool.research <subcommand> --help

Subcommands keep annotation, inference and scoring roles separate: no
subcommand performs two roles, none mutates production queues or services,
and none starts training, downloads, cluster allocation or public release.
Only stdlib (``argparse``) plus :mod:`pdf_craft_tool.research` imports.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from . import adapters, calibration, inventory, metrics, pcex, report
from . import runners, schema, splits
from .export import build_bundle


def _read_json(path, *, what: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise schema.ContractError(
            f"cannot load {what} {path}: {exc}") from exc


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def _write_json(path, payload) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# inventory — corpus audit over configured read-only sources.
# ---------------------------------------------------------------------------

def cmd_inventory(args) -> int:
    try:
        config = _read_json(args.config, what="inventory config")
        if not isinstance(config, dict):
            raise schema.ContractError("inventory config must be a JSON object")
        sources = inventory.InventorySources(
            queue_db=config.get("queue_db"),
            catalogue_db=config.get("catalogue_db"),
            dedupe_report=config.get("dedupe_report"),
            metadata_csv=config.get("metadata_csv"),
            jobs_dir=config.get("jobs_dir"),
            data_root=config.get("data_root"),
        )
        result = inventory.build_inventory(sources)
    except (schema.ContractError, OSError, ValueError) as exc:
        return _fail(f"invalid inventory config: {exc}")
    _write_json(args.out, dataclasses.asdict(result))
    print(f"inventory written to {args.out}")
    return 0


# ---------------------------------------------------------------------------
# sample — grouping + probability sample to a frozen manifest.
# ---------------------------------------------------------------------------

def cmd_sample(args) -> int:
    try:
        study = _read_json(args.config, what="study config")
        if not isinstance(study, dict):
            raise schema.ContractError("study config must be a JSON object")
        seed = int(study.get("sampling", {}).get("seed", 0))
        allocation = dict(study.get("split_allocation", {}))
        if not allocation:
            raise schema.ContractError(
                "study config needs a non-empty 'split_allocation'")
        spec = _read_json(args.input, what="sampling input")
        if not isinstance(spec, dict):
            raise schema.ContractError("sampling input must be a JSON object")
        if "families" in spec:
            families = []
            for entry in spec["families"]:
                families.append(splits.Family(
                    family_id=entry["family_id"],
                    members=tuple(entry.get("members", [])),
                    content_hashes=tuple(entry.get("content_hashes", [])),
                    processed_members=tuple(
                        entry.get("processed_members",
                                  entry.get("members", []))),
                    needs_confirmation=tuple(
                        entry.get("needs_confirmation", [])),
                ))
            split_of = dict(spec.get("split_of", {}))
        else:
            documents = [splits.Document(
                document_id=item["document_id"],
                content_sha256=item["content_sha256"],
                work_id=item.get("work_id", ""),
                edition_id=item.get("edition_id", ""),
                processed=bool(item.get("processed", False)),
                metadata=dict(item.get("metadata", {})),
            ) for item in spec.get("documents", [])]
            edges = [splits.GroupEdge(
                a=item["a"], b=item["b"], relation=item["relation"],
                method=item.get("method", ""),
                score=float(item.get("score", 0.0)),
                confirmed=bool(item.get("confirmed", False)),
            ) for item in spec.get("edges", [])]
            families = splits.group_families(documents, edges)
            split_of = splits.assign_splits(families, allocation, seed=seed)
        result = splits.probability_sample(
            families=families,
            split_of=split_of,
            pages_available=dict(spec.get("pages_available", {})),
            pages_per_family=int(spec.get("pages_per_family", 1)),
            seed=seed,
            stratum_of=dict(spec.get("stratum_of", {})),
        )
        splits.write_sample_manifest(result, Path(args.out))
    except (schema.ContractError, ValueError, KeyError, OSError) as exc:
        return _fail(f"invalid sampling config or input: {exc}")
    print(f"sample manifest written to {args.out} "
          f"({len(result.pages)} pages)")
    return 0


# ---------------------------------------------------------------------------
# ocr-pages — assemble the B0 inference input from existing OCR artifacts.
# ---------------------------------------------------------------------------

def _jobs_dir() -> Path:
    return (Path(__file__).resolve().parents[2]
            / "pdf-craft-output" / "cluster" / "jobs")


def cmd_ocr_pages(args) -> int:
    """Write ``ocr_pages.json`` (page_id / image_ref / ocr_text) for a study.

    Reads the already-computed raw OCR from each page's ``.pcex`` artifact --
    no model call, no execution. This is only the assembly of the B0
    (unchanged Tesseract) inference input; scoring it needs gold.
    """
    try:
        root = Path(args.study_root)
        provenance = _read_json(
            args.provenance or root / "pilot_provenance.json",
            what="pilot provenance")
        jobs_dir = Path(args.jobs_dir) if args.jobs_dir else _jobs_dir()
        pages_out: list[dict] = []
        for family in provenance.get("families", []):
            job_id = family["job_id"]
            matches = sorted((jobs_dir / job_id).glob("work/ocr/*/raw.pcex"))
            if not matches:
                raise schema.ContractError(
                    f"no raw.pcex under job {job_id}")
            page_numbers = [page["page_number"] for page in family["pages"]]
            texts = pcex.read_page_text(matches[0], page_numbers)
            for page in family["pages"]:
                pages_out.append({
                    "page_id": page["page_id"],
                    "image_ref": page["image_sha256"],
                    "ocr_text": texts[page["page_number"]],
                })
        if not pages_out:
            raise schema.ContractError("provenance lists no pages")
    except (schema.ContractError, OSError, ValueError, KeyError) as exc:
        return _fail(f"cannot assemble OCR pages: {exc}")
    out = Path(args.out) if args.out else root / "ocr_pages.json"
    _write_json(out, pages_out)
    empty = sum(1 for page in pages_out if not page["ocr_text"].strip())
    print(f"ocr-pages: wrote {len(pages_out)} pages to {out} "
          f"({empty} with empty OCR text)")
    return 0


# ---------------------------------------------------------------------------
# validate — reload every manifest/record in a study root.
# ---------------------------------------------------------------------------

def cmd_validate(args) -> int:
    root = Path(args.study_root)
    if not root.is_dir():
        return _fail(f"study root {root} is not a directory")
    manifests = predictions = gold_pages = 0
    try:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            name = path.name.lower()
            if name.endswith(".json") and "manifest" in name:
                schema.Manifest.load(path)
                manifests += 1
            elif name == "predictions.jsonl" or (
                    name.endswith(".jsonl") and "predict" in name):
                for lineno, line in enumerate(
                        path.read_text(encoding="utf-8").splitlines(),
                        start=1):
                    if line.strip():
                        schema.Prediction.from_dict(json.loads(line))
                        predictions += 1
            elif name == "gold.json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    for page_id, text in payload.items():
                        if not page_id or not isinstance(text, str) \
                                or not text.strip():
                            raise schema.ContractError(
                                f"gold.json entry {page_id!r} needs "
                                f"non-empty text")
                        gold_pages += 1
                elif isinstance(payload, list):
                    for entry in payload:
                        schema.GoldPage.from_dict(entry)
                        gold_pages += 1
                else:
                    raise schema.ContractError(
                        "gold.json must be a page_id->text map or a "
                        "GoldPage list")
        summary = {"manifests": manifests, "predictions": predictions,
                   "gold_pages": gold_pages, "ok": True}
    except (schema.ContractError, OSError, ValueError) as exc:
        return _fail(f"study validation failed: {exc}")
    if args.out:
        _write_json(args.out, summary)
    print(f"validate: OK "
          f"(manifests={manifests} predictions={predictions} "
          f"gold_pages={gold_pages})")
    return 0


# ---------------------------------------------------------------------------
# annotate — print the loopback annotation-server launch command.
# ---------------------------------------------------------------------------

def cmd_annotate(args) -> int:
    root = Path(args.study_root)
    db = Path(args.db) if args.db else root / "annotation.sqlite3"
    command = (
        f"{sys.executable} -m pdf_craft_tool.research.annotation_server "
        f"--db {db} --host 127.0.0.1 --port {args.port}")
    print(command)
    print("Start the annotation website with the command above; "
          "this subcommand does not start inference or scoring.")
    return 0


# ---------------------------------------------------------------------------
# run — frozen baselines, dry by default.
# ---------------------------------------------------------------------------

def cmd_run(args) -> int:
    try:
        manifest = schema.Manifest.load(args.manifest)
        pages_path = Path(args.pages) if args.pages else None
        if pages_path is not None:
            raw_pages = _read_json(pages_path, what="pages file")
            pages = list(raw_pages)
        else:
            pages = [{"page_id": record["page_id"],
                      "image_ref": record.get("image_sha256", ""),
                      "ocr_text": record.get("ocr_text", "কখগ")}
                     for record in manifest.records]
        if args.config:
            specs = adapters.load_adapter_specs(args.config)
        else:
            specs = adapters.load_adapter_specs()
        instances = {spec.adapter_id: adapters.make_adapter(spec)
                     for spec in specs}
        plan = runners.RunPlan(
            study_id=Path(args.study_root).name,
            adapter_ids=tuple(spec.adapter_id for spec in specs),
            sample_manifest=str(args.manifest),
            budget={"max_model_calls": 10000, "max_wall_seconds": 600},
        )
        result = runners.run_baselines(
            plan, pages=pages, adapters=instances,
            allow_execution=bool(args.allow_execution))
    except (schema.ContractError, adapters.AdapterUnavailable,
            OSError, ValueError) as exc:
        return _fail(f"invalid run config or manifest: {exc}")
    out = Path(args.out) if args.out else \
        Path(args.study_root) / "predictions.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for record in result["predictions"]:
            handle.write(json.dumps(record, ensure_ascii=False,
                                    sort_keys=True) + "\n")
    mode = "execution allowed" if args.allow_execution \
        else "dry run (allow_execution=False)"
    print(f"run: {mode}; wrote {len(result['predictions'])} predictions "
          f"to {out}")
    return 0


# ---------------------------------------------------------------------------
# fit-gate — fit on train, threshold on calibration; refuse test.
# ---------------------------------------------------------------------------

def cmd_fit_gate(args) -> int:
    try:
        try:
            lines = Path(args.input).read_text(
                encoding="utf-8").splitlines()
        except OSError as exc:
            raise schema.ContractError(
                f"cannot load labelled candidates {args.input}: "
                f"{exc}") from exc
        items = []
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError as exc:
                raise schema.ContractError(
                    f"bad labelled-candidate JSONL: {exc}") from exc
            items.append(calibration.LabelledCandidate(
                candidate_id=payload["candidate_id"],
                family_id=payload["family_id"],
                split=payload["split"],
                features=dict(payload["features"]),
                outcome=payload["outcome"],
            ))
        if any(item.split == calibration.TEST_SPLIT for item in items):
            raise schema.ContractError(
                "held-out (test) candidates must never enter gate fitting "
                "or threshold selection (leakage guard)")
        classifier = calibration.GateClassifier().fit(items)
        threshold = classifier.choose_threshold(
            [item for item in items
             if item.split == calibration.CALIBRATION_SPLIT])
    except schema.ContractError as exc:
        message = str(exc)
        if "leak" not in message.lower():
            message += " (leakage guard)"
        return _fail(f"fit-gate refused: {message}")
    out = Path(args.out) if args.out else \
        Path(args.study_root) / "gate_policy.json"
    _write_json(out, {"threshold": threshold,
                      "classifier": classifier.to_dict()})
    print(f"fit-gate: threshold={threshold:.4f} written to {out}")
    return 0


# ---------------------------------------------------------------------------
# evaluate — score saved predictions against gold.
# ---------------------------------------------------------------------------

def _load_gold(gold_path: Path) -> dict:
    payload = _read_json(gold_path, what="gold file")
    if isinstance(payload, dict):
        return {str(page): str(text) for page, text in payload.items()}
    if isinstance(payload, list):
        gold = {}
        for entry in payload:
            page = schema.GoldPage.from_dict(entry)
            gold[page.page_id] = "".join(
                line.text for index in page.reading_order
                for line in page.lines if line.line_index == index)
        return gold
    raise schema.ContractError("gold file must be a page_id->text map or "
                               "a GoldPage list")


def _family_of(study_root: Path) -> dict:
    mapping = {}
    for path in sorted(study_root.rglob("*")):
        if path.is_file() and path.name.lower().endswith(".json") \
                and "manifest" in path.name.lower():
            try:
                manifest = schema.Manifest.load(path)
            except schema.ContractError:
                continue
            for record in manifest.records:
                if isinstance(record, dict) and "page_id" in record:
                    mapping.setdefault(
                        record["page_id"],
                        record.get("overlap_group", "default"))
    return mapping


def cmd_evaluate(args) -> int:
    try:
        root = Path(args.study_root)
        predictions_path = Path(args.predictions) if args.predictions \
            else root / "predictions.jsonl"
        gold_path = Path(args.gold) if args.gold else root / "gold.json"
        try:
            pred_lines = predictions_path.read_text(
                encoding="utf-8").splitlines()
        except OSError as exc:
            raise schema.ContractError(
                f"cannot load predictions {predictions_path}: "
                f"{exc}") from exc
        gold = _load_gold(gold_path)
        families = _family_of(root)
        rows = []
        per_arm_family: dict[tuple[str, str], list[float]] = {}
        decisions: dict[str, dict[str, int]] = {}
        per_arm_states: dict[str, dict[str, int]] = {}
        statuses: dict[str, str] = {}
        for line in pred_lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except ValueError as exc:
                raise schema.ContractError(
                    f"bad prediction JSONL: {exc}") from exc
            prediction = schema.Prediction.from_dict(raw)
            reference = gold.get(prediction.page_id, "")
            if not reference or not reference.strip():
                raise schema.ContractError(
                    f"no gold text for page {prediction.page_id}; "
                    f"predictions without gold are never scored")
            scored = metrics.score_prediction(prediction, reference)
            arm = prediction.system_id
            family = families.get(prediction.page_id, "default")
            cer = scored["character"]["cer"]
            per_arm_family.setdefault((arm, family), []).append(cer)
            decisions.setdefault(arm, {})
            if prediction.failure_state == "ok":
                status = "ok"
            elif prediction.failure_state == "unsupported":
                status = "unsupported"
            else:
                status = prediction.failure_state  # empty/truncated/parse_error
            per_arm_states.setdefault(arm, {"attempted": 0, "ok": 0,
                                            "failed": 0, "unsupported": 0})
            st = per_arm_states[arm]
            st["attempted"] += 1
            st["ok" if status == "ok" else
               ("unsupported" if status == "unsupported" else "failed")] += 1
            statuses[arm] = ("failed" if st["failed"] else
                             ("unsupported" if st["unsupported"] and not st["ok"]
                              else "ok"))
            # This subcommand scores the TRANSCRIPTION track only: it has no
            # correction candidates/decisions, so it never emits accepted /
            # edit_class. Coverage and the harm endpoints stay unmeasured
            # (None) rather than a fabricated zero.
            rows.append({
                "arm_id": arm,
                "family": family,
                "reference": reference,
                "hypothesis": prediction.parsed_text,
                "status": status,
                "correction_track": False,
            })
        arm_results = []
        for arm in sorted(decisions):
            per_family = {family: sum(values) / len(values)
                          for (arm_id, family), values in
                          sorted(per_arm_family.items())
                          if arm_id == arm}
            st = per_arm_states.get(arm, {"attempted": 0, "ok": 0,
                                          "failed": 0, "unsupported": 0})
            arm_results.append(report.ArmResult(
                arm_id=arm, status=statuses.get(arm, "ok"),
                per_family_cer=per_family, coverage=None,
                accepted=0, beneficial=0, neutral=0, harmful=0,
                exact_correction_precision=None,
                pages_attempted=st["attempted"], pages_ok=st["ok"],
                pages_failed=st["failed"], pages_unsupported=st["unsupported"]))
        table = report.baseline_table(arm_results)
    except (schema.ContractError, OSError, ValueError) as exc:
        return _fail(f"invalid evaluation inputs: {exc}")
    records_out = Path(args.records_out) if args.records_out \
        else root / "records.jsonl"
    evaluation_out = Path(args.out) if args.out else root / "evaluation.json"
    records_out.parent.mkdir(parents=True, exist_ok=True)
    with records_out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False,
                                    sort_keys=True) + "\n")
    _write_json(evaluation_out, {
        "header": {"study_id": root.name,
                   "metric_version": metrics.METRIC_VERSION,
                   "expected_records": len(rows), "n_records": len(rows)},
        "baseline": table,
    })
    print(f"evaluate: scored {len(rows)} predictions -> {evaluation_out}")
    return 0


# ---------------------------------------------------------------------------
# report — rebuild markdown + json tables offline from saved records.
# ---------------------------------------------------------------------------

def cmd_report(args) -> int:
    try:
        root = Path(args.study_root) if args.study_root else None
        records_path = Path(args.records) if args.records \
            else root / "records.jsonl"
        if args.config:
            config_path = Path(args.config)
        else:
            try:
                rows = [json.loads(line) for line in
                        records_path.read_text(
                            encoding="utf-8").splitlines() if line.strip()]
            except (OSError, ValueError) as exc:
                raise schema.ContractError(
                    f"cannot load records {records_path}: {exc}") from exc
            arms = sorted({row.get("arm_id", "") for row in rows
                           if row.get("arm_id")})
            if not arms:
                raise schema.ContractError(
                    f"records {records_path} name no arms")
            config_path = (root / "report_config.json") if root \
                else Path("report_config.json")
            _write_json(config_path, {
                "study_id": root.name if root else "study",
                "arms": arms,
                "expected_records": len(rows),
                "total_pages": len(rows),
            })
        rebuilt = report.rebuild_from_records(records_path,
                                              config=config_path)
        n_records = rebuilt.get("header", {}).get("n_records", 0)
        report.reconcile(rebuilt, expected_records=n_records)
    except (schema.ContractError, OSError, ValueError) as exc:
        return _fail(f"invalid report inputs: {exc}")
    out = Path(args.out) if args.out else (
        root / "report.md" if root else Path("report.md"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.render_markdown(rebuilt), encoding="utf-8")
    sidecar = out.with_suffix(".json")
    if sidecar == out:
        sidecar = out.with_name(out.name + ".json")
    _write_json(sidecar, rebuilt)
    print(f"report: wrote {out} and {sidecar} "
          f"({n_records} records, totals reconciled)")
    return 0


# ---------------------------------------------------------------------------
# export — reproducible release bundle.
# ---------------------------------------------------------------------------

def cmd_export(args) -> int:
    try:
        decisions = _read_json(args.decisions, what="release decisions file")
        if not isinstance(decisions, dict):
            raise schema.ContractError(
                "release decisions file must be a JSON object")
        result = build_bundle(study_root=args.study_root,
                              out_dir=args.out_dir, kind=args.kind,
                              release_decisions=decisions)
    except (schema.ContractError, OSError, ValueError) as exc:
        message = str(exc)
        if isinstance(exc, RuntimeError) and not isinstance(
                exc, schema.ContractError):
            pass  # ExportRefused already carries an actionable message.
        return _fail(f"export refused: {message}")
    print(f"export: {args.kind} bundle -> {result['bundle_manifest']} "
          f"({len(result['items'])} items, "
          f"{len(result['excluded'])} excluded)")
    return 0


# ---------------------------------------------------------------------------
# Parser.
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pdf_craft_tool.research",
        description="Offline-first Bengali-OCR research study commands "
                    "(proposed; no training, downloads, cluster allocation "
                    "or public release).")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    node = sub.add_parser("inventory", help="audit the corpus (read-only)")
    node.add_argument("--config", required=True,
                      help="JSON inventory-sources config")
    node.add_argument("--out", required=True, help="output JSON report")
    node.set_defaults(func=cmd_inventory)

    node = sub.add_parser("sample", help="frozen probability sample")
    node.add_argument("--study-root", default=".",
                      help="study directory (default: .)")
    node.add_argument("--config", required=True, help="study config JSON")
    node.add_argument("--input", required=True,
                      help="sampling input JSON (families or documents)")
    node.add_argument("--out", required=True, help="output sample manifest")
    node.set_defaults(func=cmd_sample)

    node = sub.add_parser("ocr-pages", help="assemble the B0 inference input "
                                           "from existing OCR artifacts")
    node.add_argument("--study-root", required=True, help="study directory")
    node.add_argument("--provenance", default=None,
                      help="pilot provenance JSON "
                           "(default: <study-root>/pilot_provenance.json)")
    node.add_argument("--jobs-dir", default=None,
                      help="cluster jobs directory (default: the repo's)")
    node.add_argument("--out", default=None,
                      help="output pages JSON (default: <study-root>/ocr_pages.json)")
    node.set_defaults(func=cmd_ocr_pages)

    node = sub.add_parser("validate", help="revalidate a study root")
    node.add_argument("--study-root", required=True, help="study directory")
    node.add_argument("--out", default=None,
                      help="optional validation-summary JSON")
    node.set_defaults(func=cmd_validate)

    node = sub.add_parser("annotate", help="print the annotation-server "
                                          "launch command (loopback)")
    node.add_argument("--study-root", required=True, help="study directory")
    node.add_argument("--db", default=None,
                      help="annotation SQLite file "
                           "(default: <study-root>/annotation.sqlite3)")
    node.add_argument("--port", type=int, default=8767, help="TCP port")
    node.set_defaults(func=cmd_annotate)

    node = sub.add_parser("run", help="run frozen baselines "
                                     "(dry/mock unless --allow-execution)")
    node.add_argument("--study-root", required=True, help="study directory")
    node.add_argument("--manifest", required=True, help="sample manifest")
    node.add_argument("--config", default=None,
                      help="baselines JSON (default: frozen baselines.json)")
    node.add_argument("--pages", default=None,
                      help="optional pages JSONL (default: from manifest)")
    node.add_argument("--out", default=None, help="predictions JSONL")
    node.add_argument("--allow-execution", action="store_true",
                      help="allow real client execution; without it every "
                           "model-backed adapter yields unsupported/mock "
                           "predictions")
    node.set_defaults(func=cmd_run)

    node = sub.add_parser("fit-gate", help="fit the gate on train, tune the "
                                          "threshold on calibration "
                                          "(never test)")
    node.add_argument("--study-root", default=".",
                      help="study directory (default: .)")
    node.add_argument("--input", required=True,
                      help="labelled-candidate JSONL")
    node.add_argument("--out", default=None, help="frozen policy JSON")
    node.set_defaults(func=cmd_fit_gate)

    node = sub.add_parser("evaluate", help="score saved predictions "
                                          "against gold")
    node.add_argument("--study-root", required=True, help="study directory")
    node.add_argument("--predictions", default=None, help="predictions JSONL")
    node.add_argument("--gold", default=None, help="gold JSON")
    node.add_argument("--out", default=None, help="evaluation JSON")
    node.add_argument("--records-out", default=None,
                      help="scored-records JSONL for offline rebuilds")
    node.set_defaults(func=cmd_evaluate)

    node = sub.add_parser("report", help="rebuild markdown + json tables "
                                        "offline from saved records")
    node.add_argument("--study-root", default=None, help="study directory")
    node.add_argument("--records", default=None, help="scored-records JSONL")
    node.add_argument("--config", default=None, help="study config JSON")
    node.add_argument("--out", default=None, help="output markdown report")
    node.set_defaults(func=cmd_report)

    node = sub.add_parser("export", help="build a reproducible release bundle")
    node.add_argument("--study-root", required=True, help="study directory")
    node.add_argument("--out-dir", required=True, help="bundle directory")
    node.add_argument("--kind", required=True, choices=("inference", "gold"),
                      help="bundle kind")
    node.add_argument("--decisions", required=True,
                      help="JSON release-decisions map")
    node.set_defaults(func=cmd_export)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (schema.ContractError, OSError, ValueError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
