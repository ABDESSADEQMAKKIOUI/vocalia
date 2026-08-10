"""The model configuration belongs to the administrator; the voice does not.

These tests exist because the control they cover is the entire point of the
change, and an access control with no test is one that a later refactor removes
without anybody noticing. What they pin down is narrow and deliberate: a
non-privileged caller may move two values — ``voice`` and ``language`` — and
every other part of the configuration comes back exactly as the administrator
left it, including the provider API key.
"""

import pytest

from api.services.configuration.voice_selection import (
    apply_voice_selection,
    extract_voice_selection,
    restrict_override_to_voice_selection,
)


def admin_configuration() -> dict:
    """What the platform admin set for the organization."""
    return {
        "version": 2,
        "mode": "byok",
        "byok": {
            "mode": "realtime",
            "realtime": {
                "realtime": {
                    "provider": "google_realtime",
                    "model": "gemini-3.1-flash-live-preview",
                    "api_key": "admin-secret-key",
                    "voice": "Puck",
                    "language": "en",
                },
                "is_realtime": True,
            },
        },
    }


def realtime_of(configuration: dict) -> dict:
    return configuration["byok"]["realtime"]["realtime"]


def override_carrying(**realtime_fields) -> dict:
    """A payload shaped like what the browser posts."""
    return {
        "version": 2,
        "mode": "byok",
        "byok": {"mode": "realtime", "realtime": {"realtime": dict(realtime_fields)}},
    }


class TestVoiceSelectionIsTheOnlyThingThatPassesThrough:
    def test_voice_and_language_are_applied(self):
        result = restrict_override_to_voice_selection(
            override_carrying(voice="Aoede", language="fr"), admin_configuration()
        )
        assert realtime_of(result)["voice"] == "Aoede"
        assert realtime_of(result)["language"] == "fr"

    def test_a_different_provider_is_discarded(self):
        """The escalation this guard exists for: swapping the provider."""
        result = restrict_override_to_voice_selection(
            override_carrying(
                provider="openai_realtime", model="gpt-realtime", voice="Kore"
            ),
            admin_configuration(),
        )
        assert realtime_of(result)["provider"] == "google_realtime"
        assert realtime_of(result)["model"] == "gemini-3.1-flash-live-preview"
        assert realtime_of(result)["voice"] == "Kore"

    def test_a_planted_api_key_is_discarded(self):
        """Billing the administrator's traffic to someone else's key."""
        result = restrict_override_to_voice_selection(
            override_carrying(api_key="attacker-key", voice="Charon"),
            admin_configuration(),
        )
        assert realtime_of(result)["api_key"] == "admin-secret-key"

    def test_switching_the_top_level_mode_is_discarded(self):
        """A payload that does not even share the admin's shape."""
        result = restrict_override_to_voice_selection(
            {"version": 2, "mode": "dograh", "dograh": {"api_key": "attacker"}},
            admin_configuration(),
        )
        assert result["mode"] == "byok"
        assert realtime_of(result)["api_key"] == "admin-secret-key"

    @pytest.mark.parametrize("field", ["provider", "model", "api_key"])
    def test_no_privileged_field_survives(self, field):
        result = restrict_override_to_voice_selection(
            override_carrying(**{field: "hostile", "voice": "Fenrir"}),
            admin_configuration(),
        )
        assert realtime_of(result)[field] == realtime_of(admin_configuration())[field]


class TestAbsentValuesMeanKeepWhatIsThere:
    def test_a_payload_with_no_voice_changes_nothing(self):
        admin = admin_configuration()
        result = restrict_override_to_voice_selection(
            override_carrying(provider="openai_realtime"), admin
        )
        assert result == admin

    def test_a_null_voice_is_not_a_request_to_clear_it(self):
        """None means "unspecified" here, never "erase the admin's value"."""
        result = restrict_override_to_voice_selection(
            override_carrying(voice=None, language="ar"), admin_configuration()
        )
        assert realtime_of(result)["voice"] == "Puck"
        assert realtime_of(result)["language"] == "ar"

    def test_extraction_of_an_unrelated_shape_is_empty(self):
        assert extract_voice_selection({"mode": "dograh"}) == {}
        assert extract_voice_selection(None) == {}
        assert extract_voice_selection("not a configuration") == {}


class TestTheBaseIsNeverMutated:
    def test_the_organization_configuration_is_left_alone(self):
        """It is shared within the request; mutating it would leak across calls."""
        admin = admin_configuration()
        restrict_override_to_voice_selection(
            override_carrying(voice="Aoede", language="fr"), admin
        )
        assert realtime_of(admin)["voice"] == "Puck"
        assert realtime_of(admin)["language"] == "en"

    def test_a_configuration_without_a_realtime_block_is_returned_unchanged(self):
        """Inventing one would mean inventing a provider nobody chose."""
        base = {"version": 2, "mode": "dograh", "dograh": {"voice": "default"}}
        assert apply_voice_selection(base, {"voice": "Aoede"}) == base
