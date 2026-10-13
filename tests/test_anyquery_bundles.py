"""A saved question runs again for new subjects: same fields and sources, no new plan."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from gecko.anyquery.bundles import plan_from_bundle, reuse_problem
from gecko.anyquery.mongo import MongoStore
from gecko.anyquery.service import call_anyquery_tool
from gecko.anyquery.types import Bundle, InputBinding, PlanRequest
from tests.anyquery_fake_mongo import FakeMongoClient
from tests.test_anyquery_surface import ACCOUNT, OTHER, QUERY, _call, _deps


def _save(deps: Any, plan_id: str, name: str) -> dict:
    # _call's own first parameter is called ``name``, so save_bundle goes direct.
    return call_anyquery_tool(
        deps, ACCOUNT, "save_bundle", {"plan_id": plan_id, "name": name}
    )


class NoPlanner:
    """A bundle must never reach the model planner."""

    kind = "fake"
    model = None

    def draft(self, request: PlanRequest, candidates: Any) -> Any:
        raise AssertionError(f"the planner was asked again: {request.query}")


def _saved(tmp_path: Path, name: str = "lead-contacts") -> tuple[Any, dict]:
    deps = _deps(tmp_path)
    planned = _call(deps, "plan_query", query=QUERY)
    saved = _save(deps, planned["plan"]["plan_id"], name)
    assert saved["bundle"]["name"] == name and saved["replaced"] is False
    return replace(deps, planner=NoPlanner()), planned


def test_a_bundle_plans_new_subjects_with_the_same_fields_and_no_model(
    tmp_path: Path,
) -> None:
    deps, first = _saved(tmp_path)
    out = _call(
        deps, "plan_query", bundle="lead-contacts", subjects=["initech", "hooli"]
    )
    plan = out["plan"]
    assert plan["plan_id"] != first["plan"]["plan_id"]
    assert plan["subjects"] == ["initech", "hooli"]
    assert plan["fields"] == first["plan"]["fields"]
    assert plan["query"] == QUERY
    assert out["asked_before"] is False and out["spent"] == "nothing"
    assert out["quote"]["total_usdc_micro"] > 0


def test_the_same_bundle_and_subjects_again_reuse_that_plan(tmp_path: Path) -> None:
    deps, _first = _saved(tmp_path)
    one = _call(deps, "plan_query", bundle="lead-contacts", subjects=["initech"])
    two = _call(deps, "plan_query", bundle="lead-contacts", subjects=["initech"])
    assert two["plan"]["plan_id"] == one["plan"]["plan_id"]
    assert two["asked_before"] is True


def test_bundles_lists_what_was_saved_and_a_save_can_replace(tmp_path: Path) -> None:
    deps, first = _saved(tmp_path)
    listed = _call(deps, "bundles")["bundles"]
    assert [b["name"] for b in listed] == ["lead-contacts"]
    assert listed[0]["fields"] == ["funding", "headcount"]
    again = _save(deps, first["plan"]["plan_id"], "lead-contacts")
    assert again["replaced"] is True


def test_refusals_spend_nothing_and_say_why(tmp_path: Path) -> None:
    deps, first = _saved(tmp_path)
    assert _call(deps, "plan_query", bundle="nope", subjects=["x"])["code"] == (
        "bundle_not_found"
    )
    assert _call(deps, "plan_query", bundle="lead-contacts")["code"] == "bad_arguments"
    assert (
        _call(deps, "plan_query", bundle="lead-contacts", query=QUERY, subjects=["x"])[
            "code"
        ]
        == "bad_arguments"
    )
    plan_id = first["plan"]["plan_id"]
    assert _save(deps, plan_id, "Bad Name")["code"] == ("bad_arguments")
    assert _save(deps, "plan-none", "x")["code"] == ("plan_not_found")


def test_a_bundle_belongs_to_the_account_that_saved_it(tmp_path: Path) -> None:
    deps, _first = _saved(tmp_path)
    out = _call(deps, "plan_query", OTHER, bundle="lead-contacts", subjects=["x"])
    assert out["code"] == "bundle_not_found"
    assert _call(deps, "bundles", OTHER)["bundles"] == []


def test_a_plan_with_a_subject_inside_a_literal_input_is_not_reusable(
    tmp_path: Path,
) -> None:
    deps = _deps(tmp_path)
    planned = _call(deps, "plan_query", query=QUERY)
    plan = deps.store.plan(ACCOUNT, planned["plan"]["plan_id"])
    assert plan is not None and reuse_problem(plan) is None
    field = plan.fields[0]
    pinned = replace(
        field.sources[0],
        inputs=(InputBinding("company", "literal", f"{plan.subjects[0]} inc"),),
    )
    stuck = replace(plan, fields=(replace(field, sources=(pinned,)), *plan.fields[1:]))
    deps.store.save_plan(stuck)
    out = _save(deps, stuck.plan_id, "stuck")
    assert out["code"] == "not_reusable"


def test_plan_from_bundle_keeps_the_customer_and_makes_a_new_id(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    planned = _call(deps, "plan_query", query=QUERY)
    template = deps.store.plan(ACCOUNT, planned["plan"]["plan_id"])
    assert template is not None
    made = plan_from_bundle(template, ("x",), name="b", now="2026-10-12T00:00:00Z")
    assert made.customer == ACCOUNT and made.plan_id != template.plan_id
    assert made.fields == template.fields and made.subjects == ("x",)


def test_the_mongo_store_keeps_bundles_per_customer() -> None:
    store = MongoStore(FakeMongoClient())
    bundle = Bundle(ACCOUNT, "lead-contacts", "plan-1", QUERY, "2026-10-12T00:00:00Z")
    store.save_bundle(bundle)
    assert store.bundle(ACCOUNT, "lead-contacts") == bundle
    assert store.bundles(ACCOUNT) == [bundle]
    assert store.bundle(OTHER, "lead-contacts") is None
