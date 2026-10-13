"""An input's catalog description reaches the planner, and an array input goes out as a list.

ContactOut's people/enrich returns no email or phone unless ``include`` names them; the
only place that says so is the description of that input. Before this, the registry kept
input names only, the planner never saw the opt-in, and a paid call came back empty.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from gecko.anyquery.calls import (
    call_for,
    distinct_calls,
    plan_targets,
    share_list_inputs,
)
from gecko.anyquery.candidates import orthogonal_candidates
from gecko.anyquery.model_planner import _candidate_block
from gecko.anyquery.types import (
    InputBinding,
    PlannedField,
    PlannedSource,
    QueryPlan,
)
from gecko.servicemap.readers import orthogonal_sightings
from gecko.servicemap.snapshot import _details_record

SUBJECT = "https://www.linkedin.com/in/someone"


def _record(method: str = "POST") -> dict[str, Any]:
    params = [
        {
            "name": "linkedin_url",
            "type": "string",
            "required": False,
            "description": "",
        },
        {
            "name": "include",
            "type": "array",
            "required": False,
            "description": "work_email, personal_email, phone",
        },
    ]
    return {
        "api": "contactout",
        "details": {
            "endpoint": {
                "path": "/v1/people/enrich",
                "method": method,
                "description": "Enrich a person using multiple data points.",
                "price": 0.55,
                "hasDynamicPricing": False,
                "pathParams": [],
                "queryParams": params if method == "GET" else [],
                "bodyParams": params if method == "POST" else [],
                "responseParams": [
                    {"name": "profile.work_email"},
                    {"name": "profile.phone.0"},
                ],
            }
        },
    }


def _via_registry(record: dict[str, Any]) -> Any:
    [(sighting, _draft)] = orthogonal_sightings(
        [record], now="2026-10-12T00:00:00Z", found_url="file:details"
    )
    [candidate] = orthogonal_candidates(
        [_details_record(sighting, ["profile.work_email", "profile.phone.0"])]
    )
    return candidate


def test_the_registry_keeps_an_inputs_type_and_description() -> None:
    candidate = _via_registry(_record())
    assert candidate.input_notes == (
        "include (array): work_email, personal_email, phone",
    )
    assert candidate.list_inputs == frozenset({"include"})


def test_the_planner_is_shown_the_note() -> None:
    block = _candidate_block(_via_registry(_record()), {"email"})
    assert "input note: include (array): work_email, personal_email, phone" in block


def _plan(method: str, list_inputs: tuple[str, ...]) -> tuple[QueryPlan, PlannedSource]:
    source = PlannedSource(
        rank=0,
        candidate_id=f"orthogonal:contactout:{method}:/v1/people/enrich",
        catalog="orthogonal",
        api="contactout",
        path="/v1/people/enrich",
        price_cents=55,
        field_path="profile.work_email",
        inputs=(
            InputBinding("linkedin_url", "subject"),
            InputBinding("include", "literal", "work_email, personal_email,phone"),
        ),
        storage="unknown",
        list_inputs=list_inputs,
    )
    plan = QueryPlan(
        plan_id="plan-x",
        customer="acct",
        query="work email",
        subjects=(SUBJECT,),
        fields=(PlannedField("work_email", "Work email", "text", (source,)),),
        unplanned=(),
        planner="model",
        planner_model=None,
        catalog_sha="sha",
        created_at="2026-10-12T00:00:00Z",
    )
    return plan, source


def _targets(plan: QueryPlan) -> dict[tuple[str, str], Any]:
    return {(t.api, t.path): t for t in plan_targets(plan)}


def test_an_array_literal_goes_out_as_a_list_in_the_body() -> None:
    plan, source = _plan("POST", ("include",))
    spec = call_for(source, SUBJECT, _targets(plan))
    assert spec.body == {
        "include": ["work_email", "personal_email", "phone"],
        "linkedin_url": SUBJECT,
    }


def test_two_fields_on_one_endpoint_ask_for_both_values_in_one_call() -> None:
    # Live 12 Oct: the planner bound include=work_email for the email field and
    # include=phone for the phone field, and the quote counted ContactOut twice per lead.
    def source(value: str) -> PlannedSource:
        _plan_x, base = _plan("POST", ("include",))
        return replace(
            base,
            inputs=(
                InputBinding("linkedin_url", "subject"),
                InputBinding("include", "literal", value),
            ),
        )

    fields = share_list_inputs(
        (
            PlannedField("work_email", "Work email", "text", (source("work_email"),)),
            PlannedField("mobile_phone", "Mobile", "text", (source("phone"),)),
        )
    )
    literals = {f.sources[0].inputs[1].literal for f in fields}
    assert literals == {"phone, work_email"}
    plan, _source = _plan("POST", ("include",))
    assert len(distinct_calls(replace(plan, fields=fields))) == 1


def test_a_plain_literal_and_a_query_string_stay_text() -> None:
    plan, source = _plan("POST", ())
    assert call_for(source, SUBJECT, _targets(plan)).body["include"] == (  # type: ignore[index]
        "work_email, personal_email,phone"
    )
    plan, source = _plan("GET", ("include",))
    spec = call_for(source, SUBJECT, _targets(plan))
    assert spec.query["include"] == "work_email, personal_email,phone"
