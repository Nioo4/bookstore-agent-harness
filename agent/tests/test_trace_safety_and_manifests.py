from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.trace.errors import TraceError
from app.trace.manifests import VersionRegistry
from app.trace.safety import TraceSafetyProjector
from app.trace.types import ManifestReference


@pytest.mark.parametrize("isbn", ["9780000000217", "978-0-00-000021-7", "0-306-40615-2", "080442957X"])
def test_valid_explicit_isbn_is_not_mistaken_for_phone(isbn):
    value = {"content": "查询 ISBN " + isbn, "isbn": isbn}
    projected = TraceSafetyProjector().json(value)
    assert projected.value == value
    assert projected.replay_safe


def test_isbn_exception_never_exempts_unlabelled_numbers_or_secrets():
    value = {
        "content": "ISBN 9780000000217 电话 13812345678 邮箱 test@example.test 密码：secret-value",
        "phone": "9780000000217",
        "password": "ISBN 9780000000217",
        "isbn": "13812345678",
        "note": "9780000000217",
    }
    projected = TraceSafetyProjector().json(value)
    assert "ISBN 9780000000217" in projected.value["content"]
    for secret in ("13812345678", "test@example.test", "secret-value"):
        assert secret not in str(projected.value)
    assert all(projected.value[key] == "[REDACTED]" for key in ("phone", "password", "isbn", "note"))
    assert not projected.replay_safe


def test_chinese_book_titles_are_not_mistaken_for_addresses_but_real_addresses_are_redacted():
    projector = TraceSafetyProjector()

    title = projector.text("《元数据缺省样本》")
    address = projector.text("收货地址：北京市朝阳区建国路88号")

    assert title.value == "《元数据缺省样本》"
    assert not title.changed
    assert "北京市朝阳区建国路88号" not in address.value
    assert address.changed


def test_invalid_isbn_and_password_label_are_still_redacted():
    for text in ("ISBN 9780000000218", "password=ISBN9780000000217", "ISBN 13812345678"):
        projection = TraceSafetyProjector().json({"content": text})
        assert "[REDACTED]" in projection.value["content"]
        assert not projection.replay_safe


@pytest.mark.parametrize("sentinel", [
    "password=synthetic-demo-only", "access_token=synthetic-not-a-token",
    "confirmation_token:synthetic-confirmation-only", "密码：synthetic-password-only",
    "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiIxIn0.synthetic-signature-only",
])
def test_free_text_private_values_are_redacted_before_trace_persistence(sentinel):
    projection = TraceSafetyProjector().json({"content":"查询书籍；" + sentinel})
    assert sentinel not in str(projection.value)
    assert projection.changed and not projection.replay_safe


def test_trace_safety_redacts_structured_secrets_pii_and_nested_values() -> None:
    projected = TraceSafetyProjector().json(
        {
            "password": "never-store",
            "nested": {
                "Authorization": "Bearer secret-token-value",
                "text": "contact alice@example.test or 13812345678 from 10.0.0.8",
            },
            "safe": {"book_id": 42, "price": 8800},
        }
    )
    assert projected.changed is True
    assert projected.truncated is False
    assert projected.replay_safe is False
    rendered = str(projected.value)
    for forbidden in ("never-store", "secret-token-value", "alice@example.test", "13812345678", "10.0.0.8"):
        assert forbidden not in rendered
    assert projected.value["safe"] == {"book_id": 42, "price": 8800}


def test_trace_safety_uses_deterministic_truncation_marker_and_hash() -> None:
    projector = TraceSafetyProjector(max_json_bytes=48, max_final_answer_bytes=16)
    source = {"content": "x" * 100}
    first = projector.json(source)
    second = projector.json(source)
    assert first == second
    assert first.truncated is True
    assert first.value == {
        "truncated": True,
        "original_bytes": first.original_bytes,
        "content_hash": first.content_hash,
    }
    answer = projector.text("书" * 20)
    assert answer.truncated is True
    assert hashlib.sha256(("书" * 20).encode()).hexdigest() in str(answer.value)


def test_version_registry_validates_paths_hashes_and_named_versions(tmp_path: Path) -> None:
    (tmp_path / "prompts/v1").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "pricing").mkdir()
    prompt = tmp_path / "prompts/v1/router.md"
    prompt.write_text("safe router", encoding="utf-8")
    digest = hashlib.sha256(prompt.read_bytes()).hexdigest()
    (tmp_path / "prompts/manifest.yaml").write_text(
        "schema_version: v1\nprompt_sets:\n  - version: v1\n    prompts:\n"
        f"      - role: router\n        path: prompts/v1/router.md\n        sha256: {digest}\n",
        encoding="utf-8",
    )
    (tmp_path / "config/model-profiles.yaml").write_text(
        "schema_version: v1\nprofiles:\n  - name: default\n    router: r\n    agent: a\n    verifier: v\n",
        encoding="utf-8",
    )
    (tmp_path / "config/tool-registry-versions.json").write_text(
        '{"schema_version":"v1","versions":{"test":"' + "a" * 64 + '"}}',
        encoding="utf-8",
    )
    (tmp_path / "pricing/pricing-v1.json").write_text(
        '{"schema_version":"v1","pricing_version":"p1","effective_at":"2026-01-01T00:00:00Z","models":{}}',
        encoding="utf-8",
    )
    registry = VersionRegistry.load(
        root=tmp_path,
        prompt_manifest_path=tmp_path / "prompts/manifest.yaml",
        model_manifest_path=tmp_path / "config/model-profiles.yaml",
        pricing_manifest_path=tmp_path / "pricing/pricing-v1.json",
    )
    assert registry.model_profile("default").agent == "a"
    assert registry.prompt_text("v1", "router") == "safe router"
    with pytest.raises(TraceError) as profile_error:
        registry.model_profile("raw-provider-id")
    assert profile_error.value.code == "MODEL_PROFILE_NOT_ALLOWED"

    prompt.write_text("mutated", encoding="utf-8")
    with pytest.raises(TraceError) as hash_error:
        VersionRegistry.load(
            root=tmp_path,
            prompt_manifest_path=tmp_path / "prompts/manifest.yaml",
            model_manifest_path=tmp_path / "config/model-profiles.yaml",
            pricing_manifest_path=tmp_path / "pricing/pricing-v1.json",
        )
    assert "hash" in str(hash_error.value)


def test_version_registry_rejects_missing_database_manifest_artifacts(tmp_path: Path) -> None:
    (tmp_path / "prompts/v1").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "pricing").mkdir()
    prompt = tmp_path / "prompts/v1/router.md"
    prompt.write_text("safe router", encoding="utf-8")
    digest = hashlib.sha256(prompt.read_bytes()).hexdigest()
    (tmp_path / "prompts/manifest.yaml").write_text(
        "schema_version: v1\nprompt_sets:\n  - version: v1\n    prompts:\n"
        f"      - role: router\n        path: prompts/v1/router.md\n        sha256: {digest}\n",
        encoding="utf-8",
    )
    (tmp_path / "config/model-profiles.yaml").write_text(
        "schema_version: v1\nprofiles:\n  - name: default\n    router: r\n    agent: a\n    verifier: v\n",
        encoding="utf-8",
    )
    (tmp_path / "config/tool-registry-versions.json").write_text(
        '{"schema_version":"v1","versions":{"test":"' + "a" * 64 + '"}}',
        encoding="utf-8",
    )
    (tmp_path / "pricing/pricing-v1.json").write_text(
        '{"schema_version":"v1","pricing_version":"p1","effective_at":"2026-01-01T00:00:00Z","models":{}}',
        encoding="utf-8",
    )
    registry = VersionRegistry.load(
        root=tmp_path,
        prompt_manifest_path=tmp_path / "prompts/manifest.yaml",
        model_manifest_path=tmp_path / "config/model-profiles.yaml",
        pricing_manifest_path=tmp_path / "pricing/pricing-v1.json",
    )
    current = ManifestReference(
        model_profile="default",
        model_manifest_hash=registry.model_manifest_hash,
        prompt_set_version="v1",
        prompt_manifest_hash=registry.prompt_manifest_hash,
        tool_registry_version="a" * 64,
        pricing_version="p1",
    )
    registry.assert_database_references([current])

    with pytest.raises(TraceError) as missing:
        registry.assert_database_references(
            [current.model_copy(update={"prompt_manifest_hash": b"x" * 32})]
        )
    assert missing.value.code == "TRACE_DEPENDENCY_UNAVAILABLE"


def test_referenced_pricing_versions_are_immutable() -> None:
    root = Path(__file__).parents[1] / "pricing"
    lines = (root / "BASELINE.sha256").read_text(encoding="utf-8").splitlines()
    assert lines
    for line in lines:
        expected, name = line.split()
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
