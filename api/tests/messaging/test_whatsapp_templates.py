"""Unit tests for the WhatsApp template mirror pure helpers.

Covers definition validation, placeholder extraction and send-payload
construction — no network or database access.
"""

import pytest

from api.services.messaging.whatsapp.template_service import (
    MEDIA_HEADERS_UNSUPPORTED,
    build_send_components,
    extract_template_placeholders,
    validate_template_definition,
)


def _body(text: str, example: dict | None = None) -> dict:
    component = {"type": "BODY", "text": text}
    if example is not None:
        component["example"] = example
    return component


BODY_OK = _body(
    "Your order {{1}} ships on {{2}}.", {"body_text": [["#42", "Monday"]]}
)
BODY_PLAIN = _body("Your order has shipped.")


def _validate(components: list[dict], **overrides) -> list[str]:
    kwargs = dict(
        name="order_update",
        language="en_US",
        category="UTILITY",
        components=components,
        parameter_format="positional",
    )
    kwargs.update(overrides)
    return validate_template_definition(**kwargs)


def _quick_replies(count: int) -> dict:
    return {
        "type": "BUTTONS",
        "buttons": [{"type": "QUICK_REPLY", "text": f"Option {i}"} for i in range(count)],
    }


# ---------------------------------------------------------------------------
# validate_template_definition
# ---------------------------------------------------------------------------
def test_valid_template_passes():
    components = [
        {
            "type": "HEADER",
            "format": "TEXT",
            "text": "Hi {{1}}",
            "example": {"header_text": ["Sam"]},
        },
        BODY_OK,
        {"type": "FOOTER", "text": "Reply STOP to opt out"},
        {
            "type": "BUTTONS",
            "buttons": [
                {"type": "QUICK_REPLY", "text": "Confirm"},
                {
                    "type": "URL",
                    "text": "Track",
                    "url": "https://example.com/track/{{1}}",
                    "example": ["https://example.com/track/42"],
                },
            ],
        },
    ]
    assert _validate(components) == []


@pytest.mark.parametrize("bad_name", ["Bad Name", "UPPER", "dash-ed", "", "a" * 513])
def test_invalid_name_rejected(bad_name):
    errors = _validate([BODY_PLAIN], name=bad_name)
    assert any("name" in error for error in errors)


def test_invalid_category_rejected():
    errors = _validate([BODY_PLAIN], category="PROMO")
    assert any("category" in error for error in errors)


def test_missing_body_rejected():
    errors = _validate([{"type": "FOOTER", "text": "bye"}])
    assert any("BODY component is required" in error for error in errors)


def test_body_too_long_rejected():
    errors = _validate([_body("x" * 1025)])
    assert any("1024" in error for error in errors)


def test_non_sequential_positional_rejected():
    errors = _validate(
        [_body("Hi {{1}}, code {{3}}", {"body_text": [["Sam", "1234"]]})]
    )
    assert any("sequential" in error for error in errors)


def test_body_variables_without_example_rejected():
    errors = _validate([_body("Hi {{1}}")])
    assert any("example values" in error for error in errors)


def test_body_example_must_cover_all_variables():
    errors = _validate(
        [_body("Hi {{1}}, order {{2}}", {"body_text": [["Sam"]]})]
    )
    assert any("example values" in error for error in errors)


def test_header_variable_without_example_rejected():
    errors = _validate(
        [{"type": "HEADER", "format": "TEXT", "text": "Hi {{1}}"}, BODY_PLAIN]
    )
    assert any("example values" in error for error in errors)


def test_too_many_buttons_rejected():
    errors = _validate([BODY_PLAIN, _quick_replies(11)])
    assert any("at most 10 buttons" in error for error in errors)


def test_quick_reply_text_too_long_rejected():
    buttons = {
        "type": "BUTTONS",
        "buttons": [{"type": "QUICK_REPLY", "text": "x" * 26}],
    }
    errors = _validate([BODY_PLAIN, buttons])
    assert any("25" in error for error in errors)


def test_too_many_url_buttons_rejected():
    buttons = {
        "type": "BUTTONS",
        "buttons": [
            {"type": "URL", "text": f"Link {i}", "url": f"https://example.com/{i}"}
            for i in range(3)
        ],
    }
    errors = _validate([BODY_PLAIN, buttons])
    assert any("at most 2 URL buttons" in error for error in errors)


def test_media_header_rejected():
    header = {"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["h"]}}
    errors = _validate([header, BODY_PLAIN])
    assert MEDIA_HEADERS_UNSUPPORTED in errors


def test_footer_with_variable_rejected():
    errors = _validate([BODY_PLAIN, {"type": "FOOTER", "text": "Bye {{1}}"}])
    assert any("FOOTER" in error and "variables" in error for error in errors)


def test_named_placeholder_in_positional_template_rejected():
    errors = _validate([_body("Hi {{name}}", {"body_text": [["Sam"]]})])
    assert any("non-numeric" in error for error in errors)


def test_named_template_valid():
    components = [
        _body(
            "Hi {{name}}, your code is {{code}}.",
            {
                "body_text_named_params": [
                    {"param_name": "name", "example": "Sam"},
                    {"param_name": "code", "example": "1234"},
                ]
            },
        )
    ]
    assert _validate(components, parameter_format="named") == []


def test_named_template_missing_example_entry_rejected():
    components = [
        _body(
            "Hi {{name}}, your code is {{code}}.",
            {"body_text_named_params": [{"param_name": "name", "example": "Sam"}]},
        )
    ]
    errors = _validate(components, parameter_format="named")
    assert any("example values" in error for error in errors)


# ---------------------------------------------------------------------------
# extract_template_placeholders
# ---------------------------------------------------------------------------
def test_extract_positional_body_only():
    assert extract_template_placeholders([BODY_OK], "positional") == ["1", "2"]


def test_extract_includes_header_and_url_button_vars():
    components = [
        {"type": "HEADER", "format": "TEXT", "text": "Hi {{1}}"},
        _body("Order {{1}} ships {{2}}, code {{3}}"),
        {
            "type": "BUTTONS",
            "buttons": [
                {"type": "QUICK_REPLY", "text": "OK"},
                {"type": "URL", "text": "Track", "url": "https://x.example/{{1}}"},
            ],
        },
    ]
    assert extract_template_placeholders(components, "positional") == ["1", "2", "3"]


def test_extract_named_preserves_appearance_order():
    components = [
        {"type": "HEADER", "format": "TEXT", "text": "Hi {{name}}"},
        _body("Your code is {{code}}, {{name}}."),
        {
            "type": "BUTTONS",
            "buttons": [
                {"type": "URL", "text": "Go", "url": "https://x.example/{{token}}"}
            ],
        },
    ]
    assert extract_template_placeholders(components, "named") == [
        "name",
        "code",
        "token",
    ]


def test_extract_no_variables():
    assert extract_template_placeholders([BODY_PLAIN], "positional") == []


# ---------------------------------------------------------------------------
# build_send_components
# ---------------------------------------------------------------------------
def test_build_positional_happy_path():
    components = [
        {"type": "HEADER", "format": "TEXT", "text": "Hi {{1}}"},
        _body("Order {{1}} ships {{2}}."),
        {"type": "FOOTER", "text": "Reply STOP to opt out"},
        {
            "type": "BUTTONS",
            "buttons": [
                {"type": "QUICK_REPLY", "text": "Confirm"},
                {"type": "URL", "text": "Track", "url": "https://x.example/{{1}}"},
            ],
        },
    ]
    result = build_send_components(
        components, "positional", {"1": "Sam", "2": "Monday"}
    )
    assert result == [
        {"type": "header", "parameters": [{"type": "text", "text": "Sam"}]},
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": "Sam"},
                {"type": "text", "text": "Monday"},
            ],
        },
        {
            "type": "button",
            "sub_type": "url",
            "index": "1",
            "parameters": [{"type": "text", "text": "Sam"}],
        },
    ]


def test_build_named_includes_parameter_names():
    components = [_body("Hi {{name}}, code {{code}}.")]
    result = build_send_components(
        components, "named", {"name": "Sam", "code": "1234"}
    )
    assert result == [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "parameter_name": "name", "text": "Sam"},
                {"type": "text", "parameter_name": "code", "text": "1234"},
            ],
        }
    ]


def test_build_missing_value_raises():
    with pytest.raises(ValueError, match="missing value.*'2'"):
        build_send_components([BODY_OK], "positional", {"1": "Sam"})


def test_build_media_header_raises():
    components = [{"type": "HEADER", "format": "IMAGE"}, BODY_PLAIN]
    with pytest.raises(ValueError, match="media headers"):
        build_send_components(components, "positional", {})


def test_build_no_variables_returns_empty():
    assert build_send_components([BODY_PLAIN], "positional", {}) == []
