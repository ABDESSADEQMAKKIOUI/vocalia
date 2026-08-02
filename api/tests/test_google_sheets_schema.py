"""Tests for the LLM-inferred Google Sheets column schema.

Two things are under test here:

1. ``services/integrations/google/sheet_schema.py`` — the fingerprint that
   decides when the inference has to be redone, the deterministic fallback that
   keeps a sheet usable when the LLM is unavailable, and the server-side
   placement that refuses a column name it does not know instead of dropping the
   value.
2. ``services/workflow/tools/custom_tool.py::tool_to_function_schema`` — the
   single passage point that turns a ToolModel into an LLM function schema. A
   Sheets tool must get one named parameter per column of the bound sheet, and
   every other tool type must come out byte-for-byte the same as before. The
   last class in this file is that non-regression guard.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from api.services.integrations.google import sheet_schema
from api.services.integrations.google.sheet_schema import (
    SHEET_SCHEMA_VERSION,
    AmbiguousSheetColumnError,
    SheetSchema,
    SheetSchemaError,
    UnknownSheetColumnError,
    headers_fingerprint,
    infer_sheet_schema,
    ordered_row_values,
    schema_is_stale,
    schema_to_tool_parameters,
    validate_and_place,
)
from api.services.workflow.tools.custom_tool import tool_to_function_schema

_ORGANIZATION_ID = 42
_FAKE_MODEL = "gpt-4.1-test"

# A realistic bound sheet: one column per role the placement code special-cases,
# so phone/date/boolean coercion can be exercised through the public API.
_HEADERS = (
    "Nom du client",
    "Téléphone",
    "Date de rappel",
    "Rappel accepté",
    "Montant",
)

_COLUMNS = (
    {
        "index": 0,
        "letter": "A",
        "header": "Nom du client",
        "name": "nom_du_client",
        "role": "contact_name",
        "description": "Nom complet du client tel qu'il se présente.",
        "value_format": "",
    },
    {
        "index": 1,
        "letter": "B",
        "header": "Téléphone",
        "name": "telephone",
        "role": "phone",
        "description": "Numéro de téléphone du client, au format international.",
        "value_format": "+212XXXXXXXXX",
    },
    {
        "index": 2,
        "letter": "C",
        "header": "Date de rappel",
        "name": "date_de_rappel",
        "role": "datetime",
        "description": "Date du prochain rappel convenu avec le client.",
        "value_format": "DD/MM/YYYY",
    },
    {
        "index": 3,
        "letter": "D",
        "header": "Rappel accepté",
        "name": "rappel_accepte",
        "role": "consent",
        "description": "Le client a-t-il accepté d'être rappelé.",
        "value_format": "oui/non",
    },
    {
        "index": 4,
        "letter": "E",
        "header": "Montant",
        "name": "montant",
        "role": "amount",
        "description": "Montant en dirhams évoqué pendant l'appel.",
        "value_format": "",
    },
)


@dataclass
class MockToolModel:
    """Mock tool model, mirroring the shape tool_to_function_schema reads."""

    tool_uuid: str
    name: str
    description: str
    category: str
    definition: Dict[str, Any]


def _crm_schema(**overrides: Any) -> SheetSchema:
    """Rebuild the cached inference the way it comes back from the database.

    Goes through ``SheetSchema.from_dict`` on purpose: that is the exact path a
    schema stored in ``ToolModel.definition`` takes, so the fingerprint is the
    one the production code would recompute.
    """
    payload: Dict[str, Any] = {
        "version": SHEET_SCHEMA_VERSION,
        "headers": list(_HEADERS),
        "columns": [dict(column) for column in _COLUMNS],
        "sheet_name": "Leads",
        "header_row": 1,
        "source": "llm",
        "model": _FAKE_MODEL,
        "inferred_at": "2026-08-02T10:00:00+00:00",
        "degraded_reason": None,
    }
    payload.update(overrides)
    schema = SheetSchema.from_dict(payload)
    assert schema is not None, "the test fixture must rebuild into a schema"
    return schema


def _sheets_tool(config: Dict[str, Any]) -> MockToolModel:
    return MockToolModel(
        tool_uuid="sheets-uuid-1",
        name="Update Leads Sheet",
        description="Write the outcome of the call into the leads spreadsheet",
        category="integration",
        definition={"schema_version": 1, "type": "google_sheets", "config": config},
    )


class _FakeLLMService:
    """Stand-in for the organization's configured LLM service.

    Only ``run_inference`` is exercised: it is the out-of-band completion the
    real service exposes, the same one QA and variable extraction call.
    """

    def __init__(self, *, response: str | None = None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.calls: list[str] = []

    async def run_inference(self, context, system_instruction: str | None = None) -> str:
        self.calls.append(system_instruction or "")
        if self._error is not None:
            raise self._error
        return self._response or ""


def _patch_llm(monkeypatch, service: Any) -> AsyncMock:
    """Make the organization resolve to ``service`` without touching the DB."""
    resolve = AsyncMock(return_value=(service, _FAKE_MODEL))
    monkeypatch.setattr(sheet_schema, "_resolve_organization_llm", resolve)
    return resolve


class TestHeadersFingerprint:
    """The fingerprint decides when an LLM round-trip has to be paid again."""

    def test_fingerprint_is_stable_across_calls(self):
        assert headers_fingerprint(_HEADERS) == headers_fingerprint(_HEADERS)
        assert headers_fingerprint(_HEADERS) == headers_fingerprint(list(_HEADERS))

    def test_fingerprint_ignores_case_and_whitespace(self):
        """Re-capitalising a header is not a schema change."""
        noisy = [
            "  nom du   client ",
            "TÉLÉPHONE",
            "Date de rappel",
            "rappel ACCEPTÉ",
            "Montant\t",
        ]

        assert headers_fingerprint(noisy) == headers_fingerprint(_HEADERS)

    def test_fingerprint_changes_when_a_column_is_renamed(self):
        renamed = list(_HEADERS)
        renamed[0] = "Nom et prénom du client"

        assert headers_fingerprint(renamed) != headers_fingerprint(_HEADERS)

    def test_fingerprint_ignores_trailing_blank_cells(self):
        """Sheets trims trailing empty cells inconsistently between reads."""
        assert headers_fingerprint(list(_HEADERS) + ["", "  "]) == headers_fingerprint(
            _HEADERS
        )

    def test_fingerprint_keeps_interior_blank_cells(self):
        """An interior blank shifts every column after it, so it must count."""
        assert headers_fingerprint(["Nom", "", "Téléphone"]) != headers_fingerprint(
            ["Nom", "Téléphone"]
        )


class TestSchemaIsStale:
    """Re-inference happens only when the headers actually changed."""

    def test_identical_headers_are_not_stale(self):
        assert schema_is_stale(_crm_schema(), _HEADERS) is False

    def test_reformatted_headers_are_not_stale(self):
        assert schema_is_stale(_crm_schema(), [h.upper() for h in _HEADERS]) is False

    def test_stored_dict_form_is_accepted(self):
        """The cache round-trips through the database as JSON, not as a dataclass."""
        assert schema_is_stale(_crm_schema().to_dict(), _HEADERS) is False

    def test_renamed_column_is_stale(self):
        renamed = list(_HEADERS)
        renamed[2] = "Date du prochain appel"

        assert schema_is_stale(_crm_schema(), renamed) is True

    def test_added_column_is_stale(self):
        assert schema_is_stale(_crm_schema(), list(_HEADERS) + ["Source"]) is True

    def test_removed_column_is_stale(self):
        assert schema_is_stale(_crm_schema(), list(_HEADERS)[:-1]) is True

    def test_missing_schema_is_stale(self):
        assert schema_is_stale(None, _HEADERS) is True
        assert schema_is_stale({}, _HEADERS) is True

    def test_partial_cache_is_stale(self):
        """A cache holding one column cannot describe the whole header row."""
        assert schema_is_stale({"columns": [dict(_COLUMNS[0])]}, _HEADERS) is True

    def test_version_drift_is_stale(self):
        """An older cache layout is re-inferred even when the headers match."""
        outdated = _crm_schema(version=SHEET_SCHEMA_VERSION - 1)

        assert schema_is_stale(outdated, _HEADERS) is True


class TestValidateAndPlace:
    """The server places values; the model only ever names a column."""

    def test_accepts_the_exact_parameter_name(self):
        placed = validate_and_place(_crm_schema(), {"nom_du_client": "Ada Lovelace"})

        assert placed == {"A": "Ada Lovelace"}

    def test_accepts_the_real_header(self):
        placed = validate_and_place(_crm_schema(), {"Nom du client": "Ada Lovelace"})

        assert placed == {"A": "Ada Lovelace"}

    def test_accepts_a_header_with_different_case_and_spacing(self):
        placed = validate_and_place(_crm_schema(), {"  nom du   CLIENT ": "Ada"})

        assert placed == {"A": "Ada"}

    def test_accepts_the_inferred_role(self):
        placed = validate_and_place(_crm_schema(), {"contact_name": "Ada Lovelace"})

        assert placed == {"A": "Ada Lovelace"}

    def test_rejects_an_unknown_key_and_names_the_valid_columns(self):
        """A value silently dropped into a CRM is worse than a loud failure."""
        with pytest.raises(UnknownSheetColumnError) as exc_info:
            validate_and_place(_crm_schema(), {"adresse_email": "ada@example.com"})

        message = str(exc_info.value)
        assert "adresse_email" in message
        for column in _COLUMNS:
            assert column["name"] in message

    def test_rejects_an_empty_key(self):
        with pytest.raises(UnknownSheetColumnError):
            validate_and_place(_crm_schema(), {"": "Ada"})

    def test_rejects_two_keys_targeting_the_same_column(self):
        with pytest.raises(AmbiguousSheetColumnError) as exc_info:
            validate_and_place(
                _crm_schema(),
                {"telephone": "0612345678", "Téléphone": "0611111111"},
            )

        assert "Téléphone" in str(exc_info.value)

    def test_rejects_a_non_mapping(self):
        with pytest.raises(SheetSchemaError):
            validate_and_place(_crm_schema(), [("telephone", "0612345678")])

    def test_none_means_nothing_to_write(self):
        placed = validate_and_place(
            _crm_schema(), {"nom_du_client": "Ada", "montant": None}
        )

        assert placed == {"A": "Ada"}

    def test_coerces_a_national_phone_number_to_the_observed_format(self):
        """"0612345678" and "06 12 34 56 78" must land in the sheet identically."""
        schema = _crm_schema()

        assert validate_and_place(schema, {"telephone": "0612345678"}) == {
            "B": "'+212612345678"
        }
        assert validate_and_place(schema, {"telephone": "06 12 34 56 78"}) == {
            "B": "'+212612345678"
        }

    def test_phone_already_international_is_kept_and_escaped_as_text(self):
        """Without the leading apostrophe, USER_ENTERED would eat the "+"."""
        placed = validate_and_place(_crm_schema(), {"telephone": "+212 612-345-678"})

        assert placed == {"B": "'+212612345678"}

    def test_unparseable_phone_is_kept_verbatim(self):
        """Anything non-numeric is escaped, never mangled into a wrong number."""
        placed = validate_and_place(_crm_schema(), {"telephone": "à rappeler"})

        assert placed == {"B": "'à rappeler"}

    def test_coerces_a_date_into_the_layout_the_sheet_already_uses(self):
        placed = validate_and_place(_crm_schema(), {"date_de_rappel": "2026-08-12"})

        assert placed == {"C": "12/08/2026"}

    def test_ambiguous_date_is_written_through_untouched(self):
        """"12/08/2026" under an ISO format states no day/month convention."""
        iso_columns = [dict(column) for column in _COLUMNS]
        iso_columns[2]["value_format"] = "YYYY-MM-DD"
        schema = _crm_schema(columns=iso_columns)

        placed = validate_and_place(schema, {"date_de_rappel": "12/08/2026"})

        assert placed == {"C": "12/08/2026"}

    def test_coerces_booleans_onto_the_sheet_own_tokens(self):
        schema = _crm_schema()

        assert validate_and_place(schema, {"rappel_accepte": True}) == {"D": "oui"}
        assert validate_and_place(schema, {"rappel_accepte": "yes"}) == {"D": "oui"}
        assert validate_and_place(schema, {"rappel_accepte": False}) == {"D": "non"}

    def test_numbers_keep_their_type(self):
        placed = validate_and_place(_crm_schema(), {"montant": 1500.5})

        assert placed == {"E": 1500.5}

    def test_ordered_row_values_pads_to_the_header_width(self):
        schema = _crm_schema()
        placed = validate_and_place(
            schema,
            {
                "nom_du_client": "Ada Lovelace",
                "telephone": "0612345678",
                "date_de_rappel": "2026-08-12",
                "rappel_accepte": True,
            },
        )

        assert ordered_row_values(schema, placed) == [
            "Ada Lovelace",
            "'+212612345678",
            "12/08/2026",
            "oui",
            "",
        ]


class TestInferSheetSchema:
    """Inference runs once, at bind time, and never blocks the binding."""

    async def test_falls_back_when_no_llm_is_configured(self):
        schema = await infer_sheet_schema(_HEADERS, [], organization_id=None)

        assert schema.source == "fallback"
        assert schema.is_degraded is True
        assert schema.degraded_reason == "llm_not_configured"
        assert [column.header for column in schema.columns] == list(_HEADERS)
        assert [column.letter for column in schema.columns] == ["A", "B", "C", "D", "E"]
        assert {column.role for column in schema.columns} == {"other"}
        # Every column describes itself: poorer wording, still a usable sheet.
        assert [column.description for column in schema.columns] == list(_HEADERS)
        assert schema.fingerprint == headers_fingerprint(_HEADERS)

    async def test_falls_back_when_the_llm_raises(self, monkeypatch):
        service = _FakeLLMService(error=RuntimeError("provider is down"))
        resolve = _patch_llm(monkeypatch, service)

        schema = await infer_sheet_schema(
            _HEADERS, [], organization_id=_ORGANIZATION_ID, sheet_name="Leads"
        )

        # The organization is what decides which LLM runs: a sheet belongs to one.
        resolve.assert_awaited_once_with(_ORGANIZATION_ID)
        assert schema.source == "fallback"
        assert schema.degraded_reason == "llm_error"
        assert {column.role for column in schema.columns} == {"other"}
        assert [column.description for column in schema.columns] == list(_HEADERS)

    async def test_falls_back_when_the_llm_answers_unparseable_json(self, monkeypatch):
        service = _FakeLLMService(response="Je ne peux pas analyser cette feuille.")
        _patch_llm(monkeypatch, service)

        schema = await infer_sheet_schema(
            _HEADERS, [], organization_id=_ORGANIZATION_ID
        )

        assert schema.source == "fallback"
        assert schema.degraded_reason == "llm_invalid_response"
        assert {column.role for column in schema.columns} == {"other"}
        assert [column.description for column in schema.columns] == list(_HEADERS)

    async def test_falls_back_when_the_llm_answers_about_other_columns(
        self, monkeypatch
    ):
        """Valid JSON that matches no real column is not an inference."""
        service = _FakeLLMService(
            response='{"columns": [{"index": 99, "header": "Ville", "role": "address"}]}'
        )
        _patch_llm(monkeypatch, service)

        schema = await infer_sheet_schema(
            _HEADERS, [], organization_id=_ORGANIZATION_ID
        )

        assert schema.source == "fallback"
        assert schema.degraded_reason == "llm_invalid_response"
        assert {column.role for column in schema.columns} == {"other"}

    async def test_falls_back_when_the_llm_times_out(self, monkeypatch):
        class _HangingService:
            async def run_inference(self, context, system_instruction=None):
                await asyncio.sleep(5)
                return "{}"

        _patch_llm(monkeypatch, _HangingService())
        monkeypatch.setattr(sheet_schema, "_INFERENCE_TIMEOUT_SECONDS", 0.05)

        schema = await infer_sheet_schema(
            _HEADERS, [], organization_id=_ORGANIZATION_ID
        )

        assert schema.source == "fallback"
        assert schema.degraded_reason == "llm_timeout"
        assert schema.model == _FAKE_MODEL

    async def test_uses_the_inferred_meaning_when_the_llm_answers(self, monkeypatch):
        service = _FakeLLMService(
            response=(
                "```json\n"
                '{"columns": ['
                '{"index": 0, "header": "Nom du client", "role": "contact_name",'
                ' "description": "Nom complet du client.", "value_format": ""},'
                '{"index": 1, "header": "Téléphone", "role": "phone",'
                ' "description": "Numéro de rappel du client.",'
                ' "value_format": "+212XXXXXXXXX"}'
                "]}\n```"
            )
        )
        _patch_llm(monkeypatch, service)

        schema = await infer_sheet_schema(
            _HEADERS,
            [["Ada Lovelace", "+212612345678", "12/08/2026", "oui", "1500"]],
            organization_id=_ORGANIZATION_ID,
            sheet_name="Leads",
        )

        assert schema.source == "llm"
        assert schema.is_degraded is False
        assert schema.degraded_reason is None
        assert schema.model == _FAKE_MODEL
        by_name = {column.name: column for column in schema.columns}
        assert by_name["telephone"].role == "phone"
        assert by_name["telephone"].description == "Numéro de rappel du client."
        assert by_name["telephone"].value_format == "+212XXXXXXXXX"
        # Columns the model did not answer for keep the deterministic wording
        # instead of being dropped from the sheet.
        assert by_name["montant"].role == "other"
        assert by_name["montant"].description == "Montant"

    async def test_blank_headers_are_skipped_without_shifting_positions(self):
        schema = await infer_sheet_schema(
            ["Nom", "  ", "Téléphone"], None, organization_id=None
        )

        assert [(column.letter, column.header) for column in schema.columns] == [
            ("A", "Nom"),
            ("C", "Téléphone"),
        ]

    async def test_header_row_without_a_single_name_is_rejected(self):
        """Nothing to describe, so binding must fail loudly rather than degrade."""
        with pytest.raises(SheetSchemaError):
            await infer_sheet_schema(["", "  "], None, organization_id=None)


class TestSchemaToToolParameters:
    """The bridge into `config.parameters`, the shape tool_to_function_schema eats."""

    def test_emits_one_named_parameter_per_column(self):
        parameters = schema_to_tool_parameters(_crm_schema(), required_roles=("phone",))

        assert [param["name"] for param in parameters] == [
            column["name"] for column in _COLUMNS
        ]
        by_name = {param["name"]: param for param in parameters}
        assert by_name["telephone"]["required"] is True
        # Filling twelve CRM columns by force is how you get invented values.
        assert by_name["nom_du_client"]["required"] is False
        assert by_name["montant"]["type"] == "number"
        assert by_name["rappel_accepte"]["type"] == "boolean"
        assert by_name["telephone"]["type"] == "string"
        assert "Téléphone" in by_name["telephone"]["description"]
        assert "+212XXXXXXXXX" in by_name["telephone"]["description"]


class TestSheetsToolFunctionSchema:
    """A bound sheet becomes named parameters at the single passage point."""

    def test_cached_schema_becomes_one_property_per_column(self):
        tool = _sheets_tool(
            {
                "spreadsheet_id": "1AbC",
                "sheet_name": "Leads",
                "allowed_operations": ["append_row", "update_row"],
                "sheet_schema": _crm_schema().to_dict(),
            }
        )

        schema = tool_to_function_schema(tool)
        properties = schema["function"]["parameters"]["properties"]

        for column in _COLUMNS:
            assert column["name"] in properties
            assert properties[column["name"]]["description"] == column["description"]
        # Placement is the server's job: no free-form map, no column letters.
        assert "values" not in properties
        assert schema["function"]["parameters"]["required"] == ["operation"]
        assert properties["operation"]["enum"] == ["append_row", "update_row"]
        assert properties["match_column"]["enum"] == [
            column["name"] for column in _COLUMNS
        ]
        assert schema["_tool_uuid"] == "sheets-uuid-1"

    def test_column_letters_never_reach_the_model(self):
        tool = _sheets_tool(
            {"sheet_schema": _crm_schema().to_dict(), "allowed_operations": ["append_row"]}
        )

        properties = tool_to_function_schema(tool)["function"]["parameters"][
            "properties"
        ]

        assert [key for key in properties if key in {"A", "B", "C", "D", "E"}] == []
        assert "letter" not in str(properties)

    def test_without_a_cached_schema_it_degrades_to_values(self):
        """Sheet just bound, or headers changed: schema generation must not fail."""
        tool = _sheets_tool({"spreadsheet_id": "1AbC", "sheet_name": "Leads"})

        schema = tool_to_function_schema(tool)
        properties = schema["function"]["parameters"]["properties"]

        assert properties["values"]["type"] == "object"
        assert properties["values"]["additionalProperties"] is True
        assert schema["function"]["parameters"]["required"] == ["operation"]
        assert properties["operation"]["enum"] == [
            "read_rows",
            "append_row",
            "update_row",
        ]

    def test_unparseable_cached_schema_degrades_instead_of_raising(self):
        tool = _sheets_tool({"sheet_schema": "{not json"})

        properties = tool_to_function_schema(tool)["function"]["parameters"][
            "properties"
        ]

        assert "values" in properties

    def test_a_stale_hand_written_parameter_list_is_ignored(self):
        """The columns of the sheet are the only source of Sheets parameters."""
        tool = _sheets_tool(
            {
                "sheet_schema": _crm_schema().to_dict(),
                "allowed_operations": ["append_row"],
                "parameters": [
                    {
                        "name": "column_7",
                        "type": "string",
                        "description": "Frozen in the tool description, once upon a time",
                        "required": True,
                    }
                ],
            }
        )

        schema = tool_to_function_schema(tool)

        assert "column_7" not in schema["function"]["parameters"]["properties"]
        assert schema["function"]["parameters"]["required"] == ["operation"]


class TestFunctionSchemaNonRegression:
    """Sheets support must not have changed any other tool type."""

    def test_http_api_tool_is_unchanged(self):
        tool = MockToolModel(
            tool_uuid="http-uuid",
            name="Get Weather",
            description="Get current weather for a location",
            category="http_api",
            definition={
                "schema_version": 1,
                "type": "http_api",
                "config": {
                    "method": "GET",
                    "url": "https://api.weather.com/current",
                    "parameters": [
                        {
                            "name": "location",
                            "type": "string",
                            "description": "City name",
                            "required": True,
                        },
                        {
                            "name": "units",
                            "type": "string",
                            "description": "celsius or fahrenheit",
                            "required": False,
                        },
                    ],
                },
            },
        )

        assert tool_to_function_schema(tool) == {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "City name"},
                        "units": {
                            "type": "string",
                            "description": "celsius or fahrenheit",
                        },
                    },
                    "required": ["location"],
                },
            },
            "_tool_uuid": "http-uuid",
        }

    def test_end_call_tool_with_reason_is_unchanged(self):
        tool = MockToolModel(
            tool_uuid="end-uuid",
            name="End Call",
            description="End the call",
            category="end_call",
            definition={
                "schema_version": 1,
                "type": "end_call",
                "config": {
                    "endCallReason": True,
                    "endCallReasonDescription": "Why the call ended",
                },
            },
        )

        parameters = tool_to_function_schema(tool)["function"]["parameters"]

        assert parameters["properties"] == {
            "reason": {"type": "string", "description": "Why the call ended"}
        }
        assert parameters["required"] == ["reason"]

    def test_end_call_tool_without_reason_is_unchanged(self):
        tool = MockToolModel(
            tool_uuid="end-uuid",
            name="End Call",
            description="End the call",
            category="end_call",
            definition={"schema_version": 1, "type": "end_call", "config": {}},
        )

        parameters = tool_to_function_schema(tool)["function"]["parameters"]

        assert parameters["properties"] == {}
        assert parameters["required"] == []

    def test_static_transfer_call_tool_is_unchanged(self):
        """A static transfer takes no argument, even with a leftover list."""
        tool = MockToolModel(
            tool_uuid="transfer-uuid",
            name="Transfer To Supervisor",
            description="Transfer the call to a supervisor",
            category="transfer_call",
            definition={
                "schema_version": 1,
                "type": "transfer_call",
                "config": {
                    "destination_source": "static",
                    "destination": "+14155550123",
                    "parameters": [
                        {"name": "state", "type": "string", "description": "US state"}
                    ],
                },
            },
        )

        parameters = tool_to_function_schema(tool)["function"]["parameters"]

        assert parameters["properties"] == {}
        assert parameters["required"] == []

    def test_dynamic_transfer_call_tool_uses_the_resolver_parameters(self):
        """The precedent this feature imitates: parameters from config.resolver."""
        tool = MockToolModel(
            tool_uuid="transfer-uuid",
            name="Transfer To Partner",
            description="Transfer to the correct referral partner",
            category="transfer_call",
            definition={
                "schema_version": 1,
                "type": "transfer_call",
                "config": {
                    "destination_source": "dynamic",
                    "destination": "",
                    "resolver": {
                        "type": "http",
                        "url": "https://crm.example.com/resolve-transfer",
                        "parameters": [
                            {
                                "name": "state",
                                "type": "string",
                                "description": "The caller's US state.",
                                "required": True,
                            },
                            {
                                "name": "reason",
                                "type": "string",
                                "description": "Why transfer is needed.",
                                "required": False,
                            },
                        ],
                    },
                },
            },
        )

        parameters = tool_to_function_schema(tool)["function"]["parameters"]

        assert parameters["properties"] == {
            "state": {"type": "string", "description": "The caller's US state."},
            "reason": {"type": "string", "description": "Why transfer is needed."},
        }
        assert parameters["required"] == ["state"]

    def test_dynamic_transfer_call_tool_without_resolver_parameters_is_unchanged(self):
        tool = MockToolModel(
            tool_uuid="transfer-uuid",
            name="Transfer To Partner",
            description="Transfer to the correct referral partner",
            category="transfer_call",
            definition={
                "schema_version": 1,
                "type": "transfer_call",
                "config": {
                    "destination_source": "dynamic",
                    "resolver": {
                        "type": "http",
                        "url": "https://crm.example.com/resolve-transfer",
                        "parameters": None,
                    },
                },
            },
        )

        parameters = tool_to_function_schema(tool)["function"]["parameters"]

        assert parameters["properties"] == {}
        assert parameters["required"] == []
