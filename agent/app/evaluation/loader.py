from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import yaml

from app.evaluation.types import CaseFixture, EvalCaseV1, EvalSuiteManifest, FakeModelScript
from app.evaluation.script_v2 import FakeModelScriptV2
from app.trace.errors import TraceError


class LoadedSuite:
    def __init__(
        self,
        root: Path,
        manifest: EvalSuiteManifest,
        cases: list[EvalCaseV1],
        dataset_hash: bytes,
        files: dict[str, bytes] | None = None,
    ) -> None:
        self.root = root
        self.manifest = manifest
        self.cases = cases
        self.dataset_hash = dataset_hash
        self._files = files

    def file_bytes(self, relative: str) -> bytes:
        if self._files is not None:
            if relative not in self._files:
                raise TraceError("EVAL_DATASET_INVALID", "suite reference is not covered by the manifest")
            return self._files[relative]
        return _inside(self.root, relative).read_bytes()

    def fixture(self, case: EvalCaseV1) -> CaseFixture:
        fixture = CaseFixture.model_validate_json(self.file_bytes(case.fixture_ref))
        if self.manifest.suite_version == "v2":
            if fixture.transaction_assertions or fixture.recovery is not None:
                raise ValueError("v2 fixtures cannot predeclare transactions or simulate recovery")
            if any(call.tool_name in {"create_order", "prepare_order", "create_inventory_watch"} for call in fixture.calls):
                raise ValueError("v2 fixtures cannot replace production write implementations")
        return fixture

    def fake_model(self, case: EvalCaseV1) -> FakeModelScript | FakeModelScriptV2:
        parser = FakeModelScript if self.manifest.suite_version == "v1" else FakeModelScriptV2
        return parser.model_validate_json(self.file_bytes(case.fake_model_ref))


def load_suite(manifest_path: Path) -> LoadedSuite:
    try:
        resolved_manifest = manifest_path.resolve(strict=True)
        root = resolved_manifest.parent
        manifest = EvalSuiteManifest.model_validate(
            yaml.safe_load(resolved_manifest.read_text(encoding="utf-8"))
        )
        hashes: list[bytes] = []
        files: dict[str, bytes] = {}
        for item in manifest.files:
            path = _inside(root, item.path)
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != item.sha256:
                raise ValueError(f"evaluation file hash mismatch: {item.path}")
            hashes.append(bytes.fromhex(digest))
            files[item.path] = raw
        if "cases.jsonl" not in files:
            raise ValueError("cases.jsonl must be hashed in the manifest")
        cases = [
            EvalCaseV1.model_validate_json(line)
            for line in files["cases.jsonl"].decode("utf-8").splitlines()
            if line.strip()
        ]
        ids = [case.case_id for case in cases]
        if ids != manifest.case_ids or len(set(ids)) != 40:
            raise ValueError("case IDs do not match the manifest")
        counts = Counter(case.category for case in cases)
        if dict(counts) != manifest.category_counts:
            raise ValueError("case category counts do not match the manifest")
        for case in cases:
            if case.fixture_ref not in files or case.fake_model_ref not in files:
                raise ValueError("every suite reference must be hashed in the manifest")
        dataset_hash = hashlib.sha256(b"".join(hashes)).digest()
        suite = LoadedSuite(root, manifest, cases, dataset_hash, files)
        if manifest.suite_version == "v2":
            required = {f"{prefix}-{number:03}": category for prefix, category, count in (
                ("query", "SIMPLE_QUERY", 8), ("recommendation", "MULTI_CONSTRAINT_RECOMMENDATION", 10),
                ("memory", "LONG_TERM_MEMORY", 5), ("order", "ORDER_CONFIRMATION_CONCURRENCY", 5),
                ("injection", "PROMPT_INJECTION", 5), ("recovery", "PERSISTENT_TASK_RECOVERY", 4),
                ("failure", "DEPENDENCY_DEGRADATION", 3)) for number in range(1, count+1)}
            if {case.case_id: case.category for case in cases} != required:
                raise ValueError("v2 case IDs/categories must match the approved forty scenarios")
            for case in cases:
                if "task_expected" not in case.tags or (("critical_transaction" in case.tags) != case.case_id.startswith("order-")):
                    raise ValueError("v2 metric denominator tags do not match the approved scenarios")
                suite.fixture(case)
                suite.fake_model(case)
        return suite
    except TraceError:
        raise
    except Exception:
        raise TraceError("EVAL_DATASET_INVALID", "evaluation suite validation failed") from None


def _inside(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute():
        raise ValueError("evaluation paths must be relative")
    resolved = (root / path).resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("evaluation path escapes suite root")
    return resolved
