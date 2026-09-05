"""Exit/entry delays as `arming` / `pending` panel states (#454).

Pure parsing of the per-detector delay settings, the coordinator's overlay
state machine driven by the hub's HTS events, and its guardrails: opt-in,
never persisted, always bounded by a timer so a missed hub frame cannot
leave the panel stuck.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.aegis_ajax.api.hts.hub_events import (
    HUB_EVENT_ENTRY_DELAY_STARTED,
    HUB_EVENT_EXIT_DELAY_COMPLETE,
    HubEvent,
    parse_hub_event,
)
from custom_components.aegis_ajax.api.hts.messages import tlv_decode
from custom_components.aegis_ajax.api.models import (
    ConnectionStatus,
    Device,
    DeviceState,
    Space,
)
from custom_components.aegis_ajax.const import SecurityState
from custom_components.aegis_ajax.coordinator import AjaxCobrandedCoordinator
from custom_components.aegis_ajax.delay_states import (
    DELAY_OVERLAY_GRACE_SECONDS,
    ArmDelays,
    DelayKind,
    parse_arm_delays,
)

HUB = "002B1A51"
# Real bvis-home frames (2026-09-05), see tests/unit/hts/test_hub_events.py.
EXIT_COMPLETE = parse_hub_event(
    tlv_decode(
        bytes.fromhex("050b052105002b1a51056805fdfd06360059b4e16605fdfd07006a9be6fa050a0a67")
    )
)
ENTRY_STARTED = parse_hub_event(
    tlv_decode(
        bytes.fromhex(
            "050b052105002b1a51057405fdfd17006a9c04f505fdfd1a000205fdfd06360059b4e168"
            "05fdfd07006a9c04e1050a0a0a64"
        )
    )
)
assert EXIT_COMPLETE is not None and ENTRY_STARTED is not None


# --- settings parsing ---------------------------------------------------------


class TestParseArmDelays:
    def test_reads_both_delays_big_endian(self) -> None:
        # bvis-home DoorProtect Plus: 0xAC = 0x0014 (20 s), 0xAD = 0x0014.
        delays = parse_arm_delays({0xAC: b"\x00\x14", 0xAD: b"\x00\x14", 0xAE: b"\x01"})
        assert delays == ArmDelays(arm_delay_seconds=20, alarm_delay_seconds=20, night_mode=True)

    def test_missing_keys_default_to_zero_and_unknown_night_flag(self) -> None:
        delays = parse_arm_delays({0xAC: b"\x00\x1e"})
        assert delays == ArmDelays(arm_delay_seconds=30, alarm_delay_seconds=0, night_mode=None)

    def test_none_when_row_carries_no_delay_key(self) -> None:
        # The 60 s STATUS_BODY rows never carry 0xAC/0xAD — they must not
        # overwrite what the SETTINGS_BODY told us at connect.
        assert parse_arm_delays({0x02: b"\x17", 0x04: b"\x80"}) is None

    def test_empty_value_is_zero(self) -> None:
        delays = parse_arm_delays({0xAC: b""})
        assert delays is not None
        assert delays.arm_delay_seconds == 0


# --- coordinator overlay --------------------------------------------------------


def _space(state: SecurityState = SecurityState.DISARMED, space_id: str = "s1") -> Space:
    return Space(
        id=space_id,
        hub_id=HUB,
        name="Home",
        security_state=state,
        connection_status=ConnectionStatus.ONLINE,
        malfunctions_count=0,
    )


def _device(device_id: str, hub_id: str = HUB) -> Device:
    return Device(
        id=device_id,
        hub_id=hub_id,
        name="Door",
        device_type="door_protect_plus",
        room_id=None,
        group_id=None,
        state=DeviceState.ONLINE,
        malfunctions=0,
        bypassed=False,
        statuses={},
        battery=None,
    )


def _coordinator(
    *,
    enabled: bool = True,
    state: SecurityState = SecurityState.DISARMED,
    baseline: bool = True,
) -> AjaxCobrandedCoordinator:
    hass = MagicMock()
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        return_value=None,
    ):
        coordinator = AjaxCobrandedCoordinator(
            hass=hass,
            client=MagicMock(),
            space_ids=["s1"],
            poll_interval=300,
            delay_panel_states=enabled,
        )
    coordinator.hass = hass
    coordinator.async_set_updated_data = MagicMock()
    coordinator.spaces = {"s1": _space(state)}
    coordinator.devices = {"D1": _device("D1"), "D2": _device("D2")}
    if baseline:
        # Baseline observation, as the first poll after a (re)start leaves it.
        coordinator.sync_delay_overlays()
        coordinator.async_set_updated_data.reset_mock()
    return coordinator


def _arm(coordinator: AjaxCobrandedCoordinator, state: SecurityState = SecurityState.ARMED) -> None:
    from dataclasses import replace

    coordinator.spaces["s1"] = replace(coordinator.spaces["s1"], security_state=state)
    coordinator.sync_delay_overlays()


def _disarm(coordinator: AjaxCobrandedCoordinator) -> None:
    _arm(coordinator, SecurityState.DISARMED)


def _with_delays(
    coordinator: AjaxCobrandedCoordinator, arm: int = 20, alarm: int = 20, device: str = "D1"
) -> None:
    coordinator._on_hts_device_kv(
        HUB, device, {0xAC: arm.to_bytes(2, "big"), 0xAD: alarm.to_bytes(2, "big")}, from_body=True
    )


class TestSettingsIntake:
    def test_settings_row_feeds_the_space_exit_delay(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20, device="D1")
        _with_delays(coordinator, arm=45, device="D2")
        assert coordinator.space_exit_delay_seconds("s1") == 45

    def test_status_row_without_delay_keys_keeps_the_known_value(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        coordinator._on_hts_device_kv(HUB, "D1", {0x02: b"\x17"}, from_body=True)
        assert coordinator.space_exit_delay_seconds("s1") == 20

    def test_other_hubs_devices_do_not_count(self) -> None:
        coordinator = _coordinator()
        coordinator.devices["X1"] = _device("X1", hub_id="0000FFFF")
        coordinator._on_hts_device_kv("0000FFFF", "X1", {0xAC: b"\x00\x63"}, from_body=True)
        assert coordinator.space_exit_delay_seconds("s1") == 0

    def test_zero_for_unknown_space(self) -> None:
        assert _coordinator().space_exit_delay_seconds("nope") == 0


class TestArmingOverlay:
    def test_arm_transition_starts_arming_when_a_detector_has_a_delay(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        _arm(coordinator)
        overlay = coordinator.delay_overlays["s1"]
        assert overlay.kind is DelayKind.ARMING
        coordinator.async_set_updated_data.assert_called()

    def test_arm_transition_without_any_delay_configured_shows_nothing(self) -> None:
        coordinator = _coordinator()
        _arm(coordinator)
        assert "s1" not in coordinator.delay_overlays

    def test_disabled_option_tracks_nothing(self) -> None:
        coordinator = _coordinator(enabled=False)
        _with_delays(coordinator, arm=20)
        _arm(coordinator)
        assert coordinator.delay_overlays == {}
        coordinator._on_hts_hub_event(ENTRY_STARTED)
        assert coordinator.delay_overlays == {}

    def test_first_observation_after_start_is_not_a_transition(self) -> None:
        # A restart while armed must show the hub's plain state — nothing to
        # overlay, because we never saw the arm happen.
        coordinator = _coordinator(state=SecurityState.ARMED, baseline=False)
        _with_delays(coordinator, arm=20)
        coordinator.sync_delay_overlays()  # first ever observation: ARMED
        assert "s1" not in coordinator.delay_overlays
        coordinator.async_set_updated_data.assert_not_called()

    def test_fallback_timer_is_the_longest_delay_plus_grace(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20, device="D1")
        _with_delays(coordinator, arm=30, device="D2")
        _arm(coordinator)
        delay, _cb = coordinator.hass.loop.call_later.call_args[0][:2]
        assert delay == 30 + DELAY_OVERLAY_GRACE_SECONDS

    def test_ends_at_is_now_plus_the_longest_delay(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        now = datetime(2026, 9, 5, 9, 54, 47, tzinfo=UTC)
        with patch("custom_components.aegis_ajax.coordinator.dt_util.utcnow", return_value=now):
            _arm(coordinator)
        assert coordinator.delay_overlays["s1"].ends_at == datetime(
            2026, 9, 5, 9, 55, 7, tzinfo=UTC
        )

    def test_hub_exit_complete_event_clears_arming(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        _arm(coordinator)
        coordinator.async_set_updated_data.reset_mock()
        coordinator._on_hts_hub_event(EXIT_COMPLETE)
        assert "s1" not in coordinator.delay_overlays
        coordinator.async_set_updated_data.assert_called_once()
        coordinator.hass.loop.call_later.return_value.cancel.assert_called()

    def test_exit_complete_for_another_hub_is_ignored(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        _arm(coordinator)
        coordinator._on_hts_hub_event(
            HubEvent(hub_id="0000FFFF", code=HUB_EVENT_EXIT_DELAY_COMPLETE)
        )
        assert coordinator.delay_overlays["s1"].kind is DelayKind.ARMING

    def test_timer_expiry_clears_arming(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        _arm(coordinator)
        overlay = coordinator.delay_overlays["s1"]
        coordinator.async_set_updated_data.reset_mock()
        coordinator._expire_delay_overlay("s1", overlay)
        assert "s1" not in coordinator.delay_overlays
        coordinator.async_set_updated_data.assert_called_once()

    def test_stale_timer_does_not_clear_a_newer_overlay(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        _arm(coordinator)
        stale = coordinator.delay_overlays["s1"]
        coordinator._on_hts_hub_event(ENTRY_STARTED)  # replaces arming with pending
        coordinator._expire_delay_overlay("s1", stale)
        assert coordinator.delay_overlays["s1"].kind is DelayKind.PENDING

    def test_disarm_clears_arming(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        _arm(coordinator)
        _disarm(coordinator)
        assert "s1" not in coordinator.delay_overlays

    def test_rearm_after_disarm_starts_a_fresh_overlay(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        _arm(coordinator)
        _disarm(coordinator)
        _arm(coordinator)
        assert coordinator.delay_overlays["s1"].kind is DelayKind.ARMING

    def test_armed_to_night_is_not_a_new_arm(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        _arm(coordinator)
        coordinator._on_hts_hub_event(EXIT_COMPLETE)
        _arm(coordinator, SecurityState.NIGHT_MODE)
        assert "s1" not in coordinator.delay_overlays

    def test_night_arm_counts_only_detectors_whose_delay_applies_at_night(self) -> None:
        coordinator = _coordinator()
        coordinator._on_hts_device_kv(HUB, "D1", {0xAC: b"\x00\x14", 0xAE: b"\x00"}, from_body=True)
        coordinator._on_hts_device_kv(HUB, "D2", {0xAC: b"\x00\x0a", 0xAE: b"\x01"}, from_body=True)
        _arm(coordinator, SecurityState.NIGHT_MODE)
        delay, _cb = coordinator.hass.loop.call_later.call_args[0][:2]
        assert delay == 10 + DELAY_OVERLAY_GRACE_SECONDS

    def test_night_arm_with_unknown_night_flag_shows_nothing(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        _arm(coordinator, SecurityState.NIGHT_MODE)
        assert "s1" not in coordinator.delay_overlays

    def test_intrusion_alarm_clears_the_overlay(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        _arm(coordinator)
        coordinator.note_intrusion_alarm("s1")
        assert "s1" not in coordinator.delay_overlays

    def test_push_arm_starts_arming(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        coordinator.apply_push_security_state("s1", SecurityState.ARMED)
        assert coordinator.delay_overlays["s1"].kind is DelayKind.ARMING

    def test_push_disarm_clears_arming(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        _arm(coordinator)
        coordinator.apply_push_security_state("s1", SecurityState.DISARMED)
        assert "s1" not in coordinator.delay_overlays


class TestPendingOverlay:
    def test_entry_started_while_armed_sets_pending_with_hub_expiry(self) -> None:
        coordinator = _coordinator(state=SecurityState.ARMED)
        now = datetime(2026, 9, 5, 12, 2, 43, tzinfo=UTC)
        with patch("custom_components.aegis_ajax.coordinator.dt_util.utcnow", return_value=now):
            coordinator._on_hts_hub_event(ENTRY_STARTED)
        overlay = coordinator.delay_overlays["s1"]
        assert overlay.kind is DelayKind.PENDING
        # 0x17 - 0x07 = 20 s on the hub's clock, applied to OUR clock.
        assert overlay.ends_at == datetime(2026, 9, 5, 12, 3, 3, tzinfo=UTC)
        delay, _cb = coordinator.hass.loop.call_later.call_args[0][:2]
        assert delay == 20 + DELAY_OVERLAY_GRACE_SECONDS

    def test_entry_started_replaces_a_running_arming(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        _arm(coordinator)
        coordinator._on_hts_hub_event(ENTRY_STARTED)
        assert coordinator.delay_overlays["s1"].kind is DelayKind.PENDING

    def test_entry_started_while_disarmed_is_ignored(self) -> None:
        coordinator = _coordinator()
        coordinator._on_hts_hub_event(ENTRY_STARTED)
        assert "s1" not in coordinator.delay_overlays

    def test_entry_started_without_expiry_falls_back_to_settings(self) -> None:
        coordinator = _coordinator(state=SecurityState.ARMED)
        _with_delays(coordinator, alarm=25)
        coordinator._on_hts_hub_event(HubEvent(hub_id=HUB, code=HUB_EVENT_ENTRY_DELAY_STARTED))
        delay, _cb = coordinator.hass.loop.call_later.call_args[0][:2]
        assert delay == 25 + DELAY_OVERLAY_GRACE_SECONDS

    def test_entry_started_with_no_bound_at_all_is_ignored(self) -> None:
        # Neither the hub expiry nor a known alarm_delay: an unbounded
        # `pending` could stick, so show nothing.
        coordinator = _coordinator(state=SecurityState.ARMED)
        coordinator._on_hts_hub_event(HubEvent(hub_id=HUB, code=HUB_EVENT_ENTRY_DELAY_STARTED))
        assert "s1" not in coordinator.delay_overlays

    def test_disarm_clears_pending(self) -> None:
        coordinator = _coordinator(state=SecurityState.ARMED)
        coordinator._on_hts_hub_event(ENTRY_STARTED)
        _disarm(coordinator)
        assert "s1" not in coordinator.delay_overlays

    def test_exit_complete_does_not_clear_pending(self) -> None:
        coordinator = _coordinator(state=SecurityState.ARMED)
        coordinator._on_hts_hub_event(ENTRY_STARTED)
        coordinator._on_hts_hub_event(EXIT_COMPLETE)
        assert coordinator.delay_overlays["s1"].kind is DelayKind.PENDING

    def test_unknown_code_changes_nothing(self) -> None:
        coordinator = _coordinator(state=SecurityState.ARMED)
        coordinator._on_hts_hub_event(HubEvent(hub_id=HUB, code=0x99))
        assert coordinator.delay_overlays == {}
        coordinator.async_set_updated_data.assert_not_called()


class TestPollIntegration:
    @pytest.mark.asyncio
    async def test_poll_observed_arm_starts_arming(self) -> None:
        coordinator = _coordinator()
        _with_delays(coordinator, arm=20)
        armed = _space(SecurityState.ARMED)
        coordinator._refresh_spaces = AsyncMock(return_value={"s1": armed})
        coordinator._ensure_authenticated = AsyncMock()
        coordinator._prune_manual_refresh = MagicMock()
        coordinator._update_hub_offline_repairs = MagicMock()
        coordinator._maybe_refresh_sim_and_firmware = AsyncMock()
        coordinator._maybe_refresh_rooms = AsyncMock()
        coordinator._streams_started = True
        coordinator._maybe_fallback_device_snapshot = AsyncMock()
        coordinator._maybe_restart_hts = AsyncMock()
        await coordinator._async_update_data()
        assert coordinator.delay_overlays["s1"].kind is DelayKind.ARMING
