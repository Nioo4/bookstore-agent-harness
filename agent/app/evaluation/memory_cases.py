"""Five fixed multi-round memory cases; no model-provided success assertions."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace

from app.evaluation.catalog import BOOKS, ControlledCatalog, ControlledKnowledge
from app.evaluation.memory_store import ComponentMemoryRepository
from app.evaluation.oracle import RecommendationRequirements, recommendation_failures
from app.evaluation.production import MODEL, NoBusinessWrites, ProductionChatDriver
from app.evaluation.read_cases import READ_SCENARIOS, ReadScenario, scene_script, scripted_response
from app.evaluation.script_v2 import FakeModelScriptV2
from app.evaluation.types import CaseFixture, EvalAuth, EvalCaseV1
from app.memory.errors import MemoryNotFound
from app.memory.extractor import MemoryExtraction, MemoryExtractor
from app.memory.service import MemoryService
from app.memory.types import MemoryCandidate
from app.models.embeddings import FakeEmbeddingProvider
from app.trace.types import sha256_json


MEMORY_IDS = tuple(f"memory-{number:03}" for number in range(1, 6))


def preference(key, value, *, operation="SET", source="EXPLICIT", confidence=1):
    return dict(memory_type="PREFERENCE", memory_key="preference." + key,
        operation=operation, value=value, source=source, confidence=confidence)


ZH = preference("languages", {"items":["zh-CN"]})
TOPIC = preference("topics", {"items":["distributed"]})
BUDGET = preference("budget.CNY", {"currency":"CNY", "max_amount":4000})


@dataclass(frozen=True)
class MemoryRound:
    scene: ReadScenario
    candidates: tuple = ()
    recommendation: bool = False
    guest: bool = False
    catalog_failure: bool = False
    extraction_failure: bool = False


def memory_rounds(case_id):
    """A fixed, auditable scenario list, not a JSON-command interpreter."""
    def query(request, *candidates, **kwargs):
        return MemoryRound(ReadScenario(request, {"book_id":1}, (1,), tool="get_book"), candidates, **kwargs)

    recommend = MemoryRound(replace(READ_SCENARIOS["recommendation-001"], request="请推荐两本书"), recommendation=True)
    if case_id == "memory-001":
        return [query("以后偏好中文分布式，每本预算40元", ZH, TOPIC, BUDGET), recommend]
    if case_id == "memory-002":
        return [query("我偏好阅读，请查询B1", preference(key, {"items":["en" if key == "languages" else "java"]},
            source="INFERRED", confidence=confidence)) for key, confidence in (
                ("languages", .849), ("languages", .85), ("topics", .849), ("topics", .85))]
    if case_id == "memory-003":
        return [query(request, preference("topics", {"items":items}, operation=operation))
            for request, operation, items in (
                ("我的主题偏好设为distributed", "SET", ["distributed"]),
                ("我的主题偏好加入java，并去重", "ADD", ["java", "java"]),
                ("我的主题偏好去掉distributed", "REMOVE", ["distributed"]),
                ("我的主题偏好去掉java", "REMOVE", ["java"]))]
    if case_id == "memory-004":
        episode = dict(memory_type="EPISODE", memory_key="episode.AUTO", operation="SET",
            value={"summary":"读过分布式入门，想继续了解一致性", "book_ids":[1]}, source="EXPLICIT", confidence=1)
        return [query("我偏好中文书，我读过分布式入门", ZH, episode), recommend, recommend]
    if case_id == "memory-005":
        # All values below are deliberately synthetic, never environment credentials.
        inputs = ("password=synthetic-demo-only", "access_token=synthetic-not-a-token",
            "reader@example.test", "订单状态placed")
        rounds = [query(f"我偏好中文书；不要保存这些临时内容：{value}", ZH,
            preference("topics", {"items":[value]})) for value in inputs]
        rounds.extend([
            query("我偏好英文书，查询B1", catalog_failure=True),
            query("我偏好英文书，查询B1", guest=True),
            query("我偏好Java书，查询B1", extraction_failure=True),
        ])
        return rounds
    raise ValueError("unknown fixed memory scenario")


def memory_assets(case_id):
    """Asset authoring only; runtime consumes the separate hashed response file."""
    rounds, responses = memory_rounds(case_id), []
    for index, step in enumerate(rounds, 1):
        scope = f"turn-{index}"
        scripted = list(scene_script(step.scene, recommendation=step.recommendation, round_scope=scope).responses)
        if step.catalog_failure:
            scripted = scripted[:3] + [scripted[2].model_copy(update={"ordinal":2})]
        responses.extend(scripted)
        if step.extraction_failure:
            responses.append(dict(caller="memory_extractor", call_scope=scope + "/memory_extractor", ordinal=1,
                response_schema_sha256=sha256_json(MemoryExtraction.model_json_schema()).hex(),
                outcome="ERROR", response=None, error_code="DEPENDENCY_UNAVAILABLE"))
        elif step.candidates:
            responses.append(scripted_response("memory_extractor", MemoryExtraction,
                {"candidates":list(step.candidates)}, round_scope=scope))
    case = EvalCaseV1(case_id=case_id, category="LONG_TERM_MEMORY", tags=["task_expected"],
        user_request=rounds[0].scene.request, auth={"authenticated":True, "email_verified":True},
        memory_seed=[ZH] if case_id == "memory-002" else [], fixture_ref=f"fixtures/{case_id}.json",
        fake_model_ref=f"scripts/{case_id}.json", expected=dict(final_status="COMPLETED",
            required_tools=["get_book"], forbidden_tools=["prepare_order", "create_order", "create_inventory_watch"],
            required_facts=[], required_constraints=[], allowed_error_codes=[]))
    return case, CaseFixture(schema_version="v1", calls=[]), FakeModelScriptV2(schema_version="v2", responses=responses)


class ObservedMemoryRepository(ComponentMemoryRepository):
    def __init__(self, namespace):
        super().__init__(namespace)
        self.applied_requests = []

    async def apply(self, user_id, candidate, **kwargs):
        self.applied_requests.append((user_id, candidate.model_copy(deep=True), dict(kwargs)))
        return await super().apply(user_id, candidate, **kwargs)


class StaleVectorResults:
    """Only retrieval is controlled; production ACTIVE/owner filtering still runs."""
    def __init__(self):
        self.ids = []
        self.calls = []

    async def query(self, vector, *, user_id, limit):
        self.calls.append((user_id, tuple(self.ids)))
        return list(self.ids)[:limit]


def read_count(result):
    """Evaluate the safe trace shape without depending on raw memory identifiers."""
    return sum(
        len(event.data.get("memory_ids", []))
        for event in result.events
        if event.event == "memory.trace" and event.data.get("operation") == "READ"
    )


def extraction_errors(result):
    return [event.data["code"] for event in result.events if event.event == "memory.trace"
        and event.data.get("operation") == "WRITE" and event.data.get("code")]


async def run_memory_case(case, script, fixture, prompt_loader, *, trace_factory=None):
    repository = ObservedMemoryRepository(case.case_id)
    vector = StaleVectorResults()
    memory = MemoryService(repository, vector_store=vector, embeddings=FakeEmbeddingProvider([[1., 0.]]),
        embedding_model="component-only")
    if case.memory_seed:
        await memory.apply_candidates(1, [MemoryCandidate.model_validate(item) for item in case.memory_seed],
            conversation_id="seed", message_id="seed")
    catalog, orders = ControlledCatalog(), NoBusinessWrites()
    driver = ProductionChatDriver(script, catalog=catalog, orders=orders, knowledge=ControlledKnowledge([]),
        prompt_loader=prompt_loader, memory_service=memory,
        memory_extractor_factory=lambda provider: MemoryExtractor(provider, MODEL), trace_factory=trace_factory)
    failures, results, saved_ids, deleted_ids = [], [], set(), set()
    rounds = memory_rounds(case.case_id)
    for index, step in enumerate(rounds):
        if case.case_id == "memory-004" and index in {1, 2}:
            active = await memory.list(1, None, 20)
            if index == 1:
                saved_ids = {record.memory_id for record in active}
                vector.ids = [record.memory_id for record in active if record.memory_type.value == "EPISODE"]
                if len(saved_ids) != 2 or len(vector.ids) != 1:
                    failures.append("MEMORY_SEED_NOT_EXTRACTED")
                for record in active:
                    if await repository.active_by_ids(2, [record.memory_id]):
                        failures.append("CROSS_USER_MEMORY_READ")
                    try:
                        await memory.delete_one(2, record.memory_id)
                    except MemoryNotFound:
                        pass
                    else:
                        failures.append("CROSS_USER_MEMORY_DELETE")
                episode_id = vector.ids[0] if vector.ids else "missing"
                if vector.ids:
                    await memory.delete_one(1, episode_id)
                    deleted_ids.add(episode_id)
            else:
                await memory.delete_all(1)
                deleted_ids.update(saved_ids)
            if deleted_ids.intersection(record.memory_id for record in await memory.list(1, None, 20)):
                failures.append("DELETED_MEMORY_LISTED")
        before_rows = deepcopy(repository.rows)
        before_calls = len(driver.queue.requests)
        catalog.fail = step.catalog_failure
        result = await driver.round(case.user_request if index == 0 else step.scene.request,
            auth=EvalAuth(authenticated=False, email_verified=False) if step.guest else case.auth,
            scope=f"turn-{index+1}", conversation_id=f"v2-{case.case_id}-C{index+1}")
        results.append(result)
        calls = driver.queue.requests[before_calls:]
        if step.catalog_failure:
            if result.final_status != "FAILED" or not any(e.event == "run.failed" and e.data.get("code") == "TOOL_TIMEOUT" for e in result.events):
                failures.append("FAILURE_NOT_OBSERVED")
        else:
            constraints = step.scene.constraints
            failures.extend(recommendation_failures(result, BOOKS, RecommendationRequirements(
                allowed_ids=frozenset(step.scene.ids), count=len(step.scene.ids),
                max_price=constraints.get("max_price"), language=constraints.get("language"),
                category=next(iter(constraints.get("categories", [])), None))))
        if step.guest or step.catalog_failure:
            if repository.rows != before_rows or any(call[0] == "memory_extractor" for call in calls):
                failures.append("INELIGIBLE_RUN_EXTRACTED_MEMORY")
        elif step.candidates or step.extraction_failure:
            if sum(call[0] == "memory_extractor" for call in calls) != 1:
                failures.append("NEW_MESSAGE_EXTRACTION_COUNT_MISMATCH")
        active = await memory.list(1, None, 20)
        by_key = {record.memory_key: record for record in active}
        if len(active) != len(by_key):
            failures.append("DUPLICATE_ACTIVE_MEMORY_VERSION")
        if case.case_id == "memory-001":
            required = {"preference.languages":{"items":["zh-CN"]},
                "preference.topics":{"items":["distributed"]}, "preference.budget.CNY":{"currency":"CNY", "max_amount":4000}}
            if {key:record.value for key,record in by_key.items()} != required:
                failures.append("PREFERENCE_EXTRACTION_MISMATCH")
            if index == 0:
                saved_ids = {record.memory_id for record in active}
            elif read_count(result) < len(saved_ids):
                failures.append("CROSS_CONVERSATION_MEMORY_NOT_USED")
        elif case.case_id == "memory-002":
            language = by_key.get("preference.languages")
            if language is None or language.value != {"items":["zh-CN"]} or language.source.value != "EXPLICIT":
                failures.append("INFERRED_OVERWROTE_EXPLICIT")
            topic = by_key.get("preference.topics")
            if index < 3 and topic is not None:
                failures.append("BELOW_THRESHOLD_MEMORY_SAVED")
            if index == 3 and (topic is None or topic.value != {"items":["java"]} or topic.confidence != .85 or topic.source.value != "INFERRED"):
                failures.append("THRESHOLD_MEMORY_NOT_SAVED")
            if index in {0, 2} and not extraction_errors(result):
                failures.append("REJECTION_NOT_OBSERVABLE")
        elif case.case_id == "memory-003":
            expected = (["distributed"], ["distributed", "java"], ["java"], None)[index]
            topic = by_key.get("preference.topics")
            if (topic.value["items"] if topic else None) != expected:
                failures.append("MEMORY_COLLECTION_MERGE_MISMATCH")
            if repository.applied_requests:
                user_id, candidate, request = repository.applied_requests[-1]
                version_count = len(repository.rows)
                rows = deepcopy(repository.rows)
                await memory.apply_candidates(user_id, [candidate], conversation_id=request["conversation_id"], message_id=request["message_id"])
                if len(repository.rows) != version_count or rows != repository.rows:
                    failures.append("MEMORY_EXTRACTION_REPLAY_ADDED_VERSION")
            else:
                failures.append("MEMORY_WRITE_NOT_EXECUTED")
        elif case.case_id == "memory-004" and index:
            active_ids = {record.memory_id for record in active}
            if index == 1 and read_count(result) != len(active_ids):
                failures.append("ACTIVE_MEMORY_NOT_RETRIEVED")
            if index == 2 and (active or read_count(result)):
                failures.append("DELETE_ALL_NOT_EFFECTIVE")
            if not vector.ids or not vector.calls or vector.calls[-1][1] != tuple(vector.ids):
                failures.append("STALE_VECTOR_NOT_EXERCISED")
        elif case.case_id == "memory-005":
            if {key:record.value for key,record in by_key.items()} != {"preference.languages":{"items":["zh-CN"]}}:
                failures.append("SENSITIVE_OR_DYNAMIC_MEMORY_PERSISTED")
            if (index < 4 or step.extraction_failure) and not extraction_errors(result):
                failures.append("EXTRACTION_FAILURE_NOT_OBSERVABLE")
            if any(row["memory"].memory_key != "preference.languages" or row["memory"].value != {"items":["zh-CN"]}
                   for row in repository.rows.values()):
                failures.append("SENSITIVE_MEMORY_IN_HISTORY")
    driver.finish()
    if orders.calls or set(results[-1].tools.executed).intersection(case.expected.forbidden_tools):
        failures.append("UNEXPECTED_BUSINESS_WRITE")
    # Keep every round for reports, without turning an intentionally failed earlier
    # sub-run into the last sub-run's status. Tool/model counts already accumulate.
    final = results[-1]
    final.prior_rounds = results[:-1]
    return final, failures
