"""Tests for device capability handlers."""

from __future__ import annotations

import pytest

from custom_components.aegis_ajax import device_handlers
from custom_components.aegis_ajax.api.models import Device
from custom_components.aegis_ajax.const import DeviceState


def _device(device_type: str) -> Device:
    return Device(
        id="device-1",
        hub_id="hub-1",
        name="Test device",
        device_type=device_type,
        room_id=None,
        group_id=None,
        state=DeviceState.ONLINE,
        malfunctions=0,
        bypassed=False,
        statuses={},
        battery=None,
    )


def test_build_handler_map_rejects_duplicate_device_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handler collisions must fail instead of silently changing capabilities."""
    handler = device_handlers.StaticDeviceHandler(("duplicate_type",), ())
    monkeypatch.setattr(device_handlers, "_HANDLERS", (handler, handler))

    with pytest.raises(
        ValueError, match="Duplicate device handler registration for 'duplicate_type'"
    ):
        device_handlers._build_handler_map()


@pytest.mark.parametrize("device_type", ["smart_lock", "smart_lock_yale"])
def test_lock_capability_is_registered(device_type: str) -> None:
    assert device_handlers.capabilities_for(_device(device_type)).is_lock


def test_non_lock_does_not_have_lock_capability() -> None:
    assert not device_handlers.capabilities_for(_device("door_protect")).is_lock


@pytest.mark.parametrize(
    "device_type",
    [
        "motion_cam",
        "motion_cam_outdoor",
        "motion_cam_fibra",
        "motion_cam_phod",
        "motion_cam_outdoor_phod",
        "motion_cam_fibra_base",
    ],
)
def test_camera_capability_is_registered(device_type: str) -> None:
    assert device_handlers.capabilities_for(_device(device_type)).is_camera


@pytest.mark.parametrize(
    "device_type",
    ["motion_cam_phod", "motion_cam_outdoor_phod", "motion_cam_fibra_base"],
)
def test_phod_capability_is_registered(device_type: str) -> None:
    capabilities = device_handlers.capabilities_for(_device(device_type))
    assert capabilities.is_camera
    assert capabilities.is_phod


@pytest.mark.parametrize(
    "device_type", ["motion_cam", "motion_cam_outdoor", "motion_cam_fibra", "motion_cam_g3"]
)
def test_non_phod_motion_camera_has_no_phod_capability(device_type: str) -> None:
    assert not device_handlers.capabilities_for(_device(device_type)).is_phod
