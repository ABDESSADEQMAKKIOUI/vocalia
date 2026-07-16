"""Pure unit tests for WhatsApp campaign dispatch helpers.

No network or DB access: exercises wa_id normalization, the
marketing-vs-utility suppression scope decision, the
retry_kind -> dispatcher action mapping and the Meta error-code ->
suppression mapping used by campaign_message_dispatcher.
"""

from datetime import timedelta

import pytest

from api.services.campaign.campaign_message_dispatcher import (
    ACTION_FAIL,
    ACTION_HALT,
    ACTION_RETRY,
    ACTION_SUPPRESS_24H,
    ACTION_SUPPRESS_PERMANENT,
    action_for_retry_kind,
    is_marketing_template,
    lowercase_template_values,
    normalize_wa_id,
    suppression_for_meta_error_code,
)


class TestNormalizeWaId:
    """Meta wa_ids are digits only — no '+', spaces, or separators."""

    def test_strips_plus_prefix(self):
        assert normalize_wa_id("+212612345678") == "212612345678"

    def test_strips_spaces_dashes_and_parentheses(self):
        assert normalize_wa_id("+1 (415) 555-01 23") == "14155550123"

    def test_plain_digits_pass_through(self):
        assert normalize_wa_id("212612345678") == "212612345678"

    def test_non_string_input_is_coerced(self):
        assert normalize_wa_id(14155550123) == "14155550123"

    def test_empty_and_none_yield_empty_string(self):
        assert normalize_wa_id("") == ""
        assert normalize_wa_id(None) == ""

    def test_no_digits_yields_empty_string(self):
        assert normalize_wa_id("not-a-number") == ""


class TestSuppressionScopeDecision:
    """The dispatcher derives the ``marketing`` flag passed to
    ``is_messaging_suppressed`` from the template category: marketing sends
    are blocked by both suppression scopes ('all' and 'marketing'), while
    utility/authentication sends are only blocked by scope 'all'."""

    def test_marketing_category_is_marketing(self):
        assert is_marketing_template("MARKETING") is True

    def test_marketing_category_is_case_insensitive_and_trimmed(self):
        assert is_marketing_template("marketing") is True
        assert is_marketing_template(" Marketing ") is True

    def test_utility_category_is_not_marketing(self):
        assert is_marketing_template("UTILITY") is False

    def test_authentication_category_is_not_marketing(self):
        assert is_marketing_template("AUTHENTICATION") is False

    def test_missing_category_defaults_to_non_marketing(self):
        assert is_marketing_template(None) is False
        assert is_marketing_template("") is False


class TestRetryKindActionMapping:
    """WhatsAppApiError.retry_kind -> dispatcher action table."""

    @pytest.mark.parametrize(
        ("retry_kind", "expected_action"),
        [
            ("backoff", ACTION_RETRY),
            ("cooldown_24h", ACTION_SUPPRESS_24H),
            ("suppress_permanent", ACTION_SUPPRESS_PERMANENT),
            ("campaign_halt", ACTION_HALT),
            ("never", ACTION_FAIL),
        ],
    )
    def test_known_retry_kinds(self, retry_kind, expected_action):
        assert action_for_retry_kind(retry_kind) == expected_action

    def test_unknown_retry_kind_fails_without_retry(self):
        assert action_for_retry_kind("something_else") == ACTION_FAIL

    def test_missing_retry_kind_fails_without_retry(self):
        assert action_for_retry_kind(None) == ACTION_FAIL
        assert action_for_retry_kind("") == ACTION_FAIL


class TestMetaErrorCodeSuppressions:
    """Delivery-failure codes in status callbacks that feed the
    do-not-message list."""

    def test_131049_is_24h_marketing_cooldown(self):
        scope, reason, ttl = suppression_for_meta_error_code(131049)
        assert scope == "marketing"
        assert reason == "meta_131049"
        assert ttl == timedelta(hours=24)

    def test_131050_is_permanent_marketing_opt_out(self):
        scope, reason, ttl = suppression_for_meta_error_code(131050)
        assert scope == "marketing"
        assert reason == "meta_131050"
        assert ttl is None

    def test_string_codes_are_coerced(self):
        assert suppression_for_meta_error_code("131050") == (
            "marketing",
            "meta_131050",
            None,
        )

    def test_other_codes_do_not_suppress(self):
        assert suppression_for_meta_error_code(131026) is None
        assert suppression_for_meta_error_code(0) is None

    def test_invalid_codes_do_not_suppress(self):
        assert suppression_for_meta_error_code(None) is None
        assert suppression_for_meta_error_code("oops") is None


class TestLowercaseTemplateValues:
    """Template send values are keyed by lowercased context keys, matching
    the lowercased CSV headers enforced at campaign creation."""

    def test_keys_lowercased_and_values_stringified(self):
        assert lowercase_template_values({"First_Name": "Ada", "AGE": 36}) == {
            "first_name": "Ada",
            "age": "36",
        }

    def test_none_values_become_empty_strings(self):
        assert lowercase_template_values({"city": None}) == {"city": ""}

    def test_none_context_yields_empty_map(self):
        assert lowercase_template_values(None) == {}
