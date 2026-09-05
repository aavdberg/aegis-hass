"""Hub-sourced `type=0x08` event frames (#454): exit-delay complete / entry-delay started.

Fixtures are the REAL frames captured on bvis-home (2026-09-05) and in
dheuts90's #284 capture (2026-06-12), byte for byte — the parser must be
proven against what the hub actually sends, not against a shape we invented
(the #413 lesson).
"""

from __future__ import annotations

from custom_components.aegis_ajax.api.hts.hub_events import (
    HUB_EVENT_ENTRY_DELAY_STARTED,
    HUB_EVENT_EXIT_DELAY_COMPLETE,
    is_hub_event,
    parse_hub_event,
)
from custom_components.aegis_ajax.api.hts.messages import tlv_decode, tlv_encode

# bvis-home, 2026-09-05 11:55:07 local — 19 s after an app arm.
EXIT_COMPLETE_HEX = "050b052105002b1a51056805fdfd06360059b4e16605fdfd07006a9be6fa050a0a67"
# bvis-home, 2026-09-05 14:02:43 local — 0.4 s after door_opened on the
# delayed DoorProtect Plus while armed; key 0x17 = expiry = 0x07 + 20 s.
ENTRY_STARTED_HEX = (
    "050b052105002b1a51057405fdfd17006a9c04f505fdfd1a000205fdfd06360059b4e16805fdfd07006a9c04e1"
    "050a0a0a64"
)
# dheuts90 #284, 2026-06-12 — 29 s after a Keypad Plus night arm. The 0x07
# value was lost to the log's text redaction, so it decodes to an empty value.
DHEUTS90_EXIT_COMPLETE_HEX = "050b052105002ca00635056805fdfd04000205fdfd06360035fd704b05fdfd0700"


class TestIsHubEvent:
    def test_true_for_exit_delay_complete_frame(self) -> None:
        assert is_hub_event(tlv_decode(bytes.fromhex(EXIT_COMPLETE_HEX))) is True

    def test_true_for_entry_delay_started_frame(self) -> None:
        assert is_hub_event(tlv_decode(bytes.fromhex(ENTRY_STARTED_HEX))) is True

    def test_false_for_space_event_frame(self) -> None:
        # A 0x02-family space event (arm from the app) must not match.
        params = tlv_decode(
            tlv_encode([b"\x02", b"\x22", b"\x77\xdd\x6a\x14", b"\x01", b"\x00\x00", b"\x00"])
        )
        assert is_hub_event(params) is False

    def test_false_when_hub_id_is_not_four_bytes(self) -> None:
        params = tlv_decode(tlv_encode([b"\x0b", b"\x21", b"\x00\x2b", b"\x68"]))
        assert is_hub_event(params) is False

    def test_false_for_other_second_byte(self) -> None:
        params = tlv_decode(tlv_encode([b"\x0b", b"\x22", b"\x00\x2b\x1a\x51", b"\x68"]))
        assert is_hub_event(params) is False


class TestParseHubEvent:
    def test_exit_delay_complete_fields(self) -> None:
        event = parse_hub_event(tlv_decode(bytes.fromhex(EXIT_COMPLETE_HEX)))
        assert event is not None
        assert event.hub_id == "002B1A51"
        assert event.code == HUB_EVENT_EXIT_DELAY_COMPLETE
        assert event.hub_ts == 0x6A9BE6FA
        assert event.expires_at is None
        assert event.delay_seconds is None

    def test_entry_delay_started_carries_expiry_twenty_seconds_out(self) -> None:
        event = parse_hub_event(tlv_decode(bytes.fromhex(ENTRY_STARTED_HEX)))
        assert event is not None
        assert event.hub_id == "002B1A51"
        assert event.code == HUB_EVENT_ENTRY_DELAY_STARTED
        assert event.hub_ts == 0x6A9C04E1
        assert event.expires_at == 0x6A9C04F5
        # The DoorProtect Plus has alarm_delay_seconds (0xAD) = 20 on that hub.
        assert event.delay_seconds == 20

    def test_keyed_values_are_exposed_raw(self) -> None:
        event = parse_hub_event(tlv_decode(bytes.fromhex(ENTRY_STARTED_HEX)))
        assert event is not None
        assert event.values[0x1A] == b"\x00\x02"
        assert event.values[0x06] == b"\x00\x59\xb4\xe1\x68"

    def test_dheuts90_frame_with_redacted_timestamp(self) -> None:
        event = parse_hub_event(tlv_decode(bytes.fromhex(DHEUTS90_EXIT_COMPLETE_HEX)))
        assert event is not None
        assert event.hub_id == "002CA005"
        assert event.code == HUB_EVENT_EXIT_DELAY_COMPLETE
        assert event.hub_ts is None
        assert event.values[0x04] == b"\x00\x02"

    def test_returns_none_for_non_hub_event(self) -> None:
        params = tlv_decode(tlv_encode([b"\x02", b"\x22", b"\x77\xdd\x6a\x14", b"\x01"]))
        assert parse_hub_event(params) is None

    def test_trailing_junk_without_marker_is_ignored(self) -> None:
        # The frames end in a short 0a… param that carries no fdfd marker.
        event = parse_hub_event(tlv_decode(bytes.fromhex(EXIT_COMPLETE_HEX)))
        assert event is not None
        assert set(event.values) == {0x06, 0x07}
