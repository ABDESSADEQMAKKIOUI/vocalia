"""Pure unit tests for messaging-config masking helpers and request schemas.

No network or database access — the helpers operate on plain dicts and the
schemas are validated in memory.
"""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.routes.messaging_config import (
    _reject_masked_credentials,
    mask_messaging_credentials,
    merge_masked_credentials,
)
from api.schemas.messaging_config import (
    MessagingAddressCreateRequest,
    MessagingAddressUpdateRequest,
    MessagingConfigurationCreateRequest,
    MessagingConfigurationUpdateRequest,
    MessagingCredentialsRequest,
    MessagingSuppressionCreateRequest,
)

ACCESS_TOKEN = "EAAGm0PX4ZCpsBO1234567890abcdef"
APP_SECRET = "0123456789abcdef0123456789abcdef"
VERIFY_TOKEN = "my-verify-token-42"
WABA_ID = "102290129340398"

PLAIN_CREDENTIALS = {
    "access_token": ACCESS_TOKEN,
    "app_secret": APP_SECRET,
    "verify_token": VERIFY_TOKEN,
    "waba_id": WABA_ID,
}


# ---------------------------------------------------------------------------
# mask_messaging_credentials
# ---------------------------------------------------------------------------


class TestMaskMessagingCredentials:
    def test_secrets_are_masked_showing_last_four_chars(self):
        masked = mask_messaging_credentials(PLAIN_CREDENTIALS)

        for key, real in (
            ("access_token", ACCESS_TOKEN),
            ("app_secret", APP_SECRET),
            ("verify_token", VERIFY_TOKEN),
        ):
            assert masked[key] != real
            assert masked[key].endswith(real[-4:])
            assert set(masked[key][:-4]) == {"*"}

    def test_waba_id_is_not_masked(self):
        masked = mask_messaging_credentials(PLAIN_CREDENTIALS)
        assert masked["waba_id"] == WABA_ID

    def test_missing_and_empty_values_pass_through(self):
        masked = mask_messaging_credentials(
            {"access_token": ACCESS_TOKEN, "app_secret": "", "verify_token": None}
        )
        assert masked["app_secret"] == ""
        assert masked["verify_token"] is None

    def test_does_not_mutate_input(self):
        original = dict(PLAIN_CREDENTIALS)
        mask_messaging_credentials(PLAIN_CREDENTIALS)
        assert PLAIN_CREDENTIALS == original


# ---------------------------------------------------------------------------
# merge_masked_credentials — the masked round-trip
# ---------------------------------------------------------------------------


class TestMergeMaskedCredentials:
    def test_masked_round_trip_does_not_overwrite_stored_secrets(self):
        """GET returns masked values; re-submitting them unchanged on PUT
        must keep every stored secret intact."""
        masked = mask_messaging_credentials(PLAIN_CREDENTIALS)
        merged = merge_masked_credentials(masked, PLAIN_CREDENTIALS)
        assert merged == PLAIN_CREDENTIALS

    def test_new_plain_value_overwrites(self):
        incoming = {"access_token": "brand-new-token-xyz"}
        merged = merge_masked_credentials(incoming, PLAIN_CREDENTIALS)
        assert merged["access_token"] == "brand-new-token-xyz"
        # untouched keys preserved
        assert merged["app_secret"] == APP_SECRET
        assert merged["waba_id"] == WABA_ID

    def test_absent_keys_are_preserved(self):
        merged = merge_masked_credentials({}, PLAIN_CREDENTIALS)
        assert merged == PLAIN_CREDENTIALS

    def test_empty_and_none_values_keep_stored_secret(self):
        incoming = {"access_token": "", "app_secret": None}
        merged = merge_masked_credentials(incoming, PLAIN_CREDENTIALS)
        assert merged["access_token"] == ACCESS_TOKEN
        assert merged["app_secret"] == APP_SECRET

    def test_generic_masked_placeholder_is_ignored(self):
        # A value containing the mask marker never overwrites, even if it is
        # not the exact mask of the stored secret (e.g. UI-truncated).
        incoming = {"access_token": "**********cdef"}
        merged = merge_masked_credentials(incoming, PLAIN_CREDENTIALS)
        assert merged["access_token"] == ACCESS_TOKEN

    def test_short_secret_mask_is_recognized(self):
        # mask_key("abcde") == "*bcde" — no "***" marker, so only the
        # is_mask_of comparison can catch it.
        stored = {"verify_token": "abcde"}
        merged = merge_masked_credentials({"verify_token": "*bcde"}, stored)
        assert merged["verify_token"] == "abcde"

    def test_masked_value_for_unset_key_is_dropped(self):
        # Masked placeholder with nothing stored to restore: keep nothing
        # rather than persisting asterisks as a real secret.
        merged = merge_masked_credentials(
            {"app_secret": "***abcd"}, {"access_token": ACCESS_TOKEN}
        )
        assert "app_secret" not in merged

    def test_waba_id_can_be_changed(self):
        merged = merge_masked_credentials({"waba_id": "999"}, PLAIN_CREDENTIALS)
        assert merged["waba_id"] == "999"


class TestRejectMaskedCredentials:
    def test_masked_value_on_create_is_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            _reject_masked_credentials({"access_token": "***********cdef"})
        assert exc_info.value.status_code == 422
        assert "access_token" in exc_info.value.detail

    def test_plain_values_pass(self):
        _reject_masked_credentials(PLAIN_CREDENTIALS)  # no raise


# ---------------------------------------------------------------------------
# Request schema validation
# ---------------------------------------------------------------------------


class TestCreateRequestSchema:
    def test_valid_full_payload(self):
        request = MessagingConfigurationCreateRequest(
            name="Main WABA",
            credentials={
                "access_token": ACCESS_TOKEN,
                "app_secret": APP_SECRET,
                "verify_token": VERIFY_TOKEN,
                "waba_id": WABA_ID,
            },
            addresses=[
                {
                    "address": "+14155550100",
                    "external_id": "111222333444555",
                    "account_id": WABA_ID,
                    "inbound_workflow_id": 7,
                }
            ],
        )
        assert request.credentials.access_token == ACCESS_TOKEN
        assert request.addresses[0].external_id == "111222333444555"

    def test_optional_secrets_default_to_none(self):
        creds = MessagingCredentialsRequest(
            access_token=ACCESS_TOKEN, waba_id=WABA_ID
        )
        assert creds.app_secret is None
        assert creds.verify_token is None

    def test_missing_access_token_rejected(self):
        with pytest.raises(ValidationError):
            MessagingConfigurationCreateRequest(
                name="Main WABA", credentials={"waba_id": WABA_ID}
            )

    def test_missing_waba_id_rejected(self):
        with pytest.raises(ValidationError):
            MessagingCredentialsRequest(access_token=ACCESS_TOKEN)

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            MessagingConfigurationCreateRequest(
                name="",
                credentials={"access_token": ACCESS_TOKEN, "waba_id": WABA_ID},
            )

    def test_address_requires_external_id(self):
        with pytest.raises(ValidationError):
            MessagingAddressCreateRequest(address="+14155550100")

    def test_addresses_default_to_empty_list(self):
        request = MessagingConfigurationCreateRequest(
            name="Main WABA",
            credentials={"access_token": ACCESS_TOKEN, "waba_id": WABA_ID},
        )
        assert request.addresses == []


class TestUpdateRequestSchemas:
    def test_all_fields_optional(self):
        request = MessagingConfigurationUpdateRequest()
        assert request.name is None
        assert request.is_active is None
        assert request.credentials is None

    def test_partial_credentials_update(self):
        request = MessagingConfigurationUpdateRequest(
            credentials={"access_token": "new-token"}
        )
        dumped = request.credentials.model_dump(exclude_none=True)
        assert dumped == {"access_token": "new-token"}

    def test_address_update_defaults(self):
        request = MessagingAddressUpdateRequest()
        assert request.inbound_workflow_id is None
        assert request.clear_inbound_workflow is False
        assert request.is_active is None


class TestSuppressionRequestSchema:
    def test_defaults(self):
        request = MessagingSuppressionCreateRequest(address="14155550100")
        assert request.scope == "marketing"
        assert request.reason == "manual"
        assert request.expires_at is None

    def test_scope_all_accepted(self):
        request = MessagingSuppressionCreateRequest(
            address="14155550100", scope="all"
        )
        assert request.scope == "all"

    def test_invalid_scope_rejected(self):
        with pytest.raises(ValidationError):
            MessagingSuppressionCreateRequest(
                address="14155550100", scope="promotional"
            )

    def test_non_manual_reason_rejected(self):
        with pytest.raises(ValidationError):
            MessagingSuppressionCreateRequest(
                address="14155550100", reason="meta_131050"
            )

    def test_expires_at_parsed(self):
        request = MessagingSuppressionCreateRequest(
            address="14155550100", expires_at="2026-08-01T00:00:00Z"
        )
        assert request.expires_at is not None
        assert request.expires_at.year == 2026
