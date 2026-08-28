"""Persisted "has this credential set ever delivered a push" record (#437).

`_pushes_received` and `_last_push_at` are per-process, so a registration that
Ajax accepts and then never delivers to looks identical, after every restart, to
one that simply hasn't seen an event yet. That is mechanically why #359 spent a
month in total silence without producing a single observable signal. This record
survives restarts and is keyed to the credential fingerprint, so re-entering the
four values resets the evidence by construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from custom_components.aegis_ajax.notification import AjaxNotificationListener

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_FCM_KWARGS = {
    "fcm_project_id": "mws-mobile-client---2",
    "fcm_app_id": "1:991608156148:android:" + "a" * 40,
    "fcm_api_key": "AIza" + "x" * 35,
    "fcm_sender_id": "991608156148",
}

# Same shapes, different project — a distinct fingerprint.
_OTHER_FCM_KWARGS = {
    **_FCM_KWARGS,
    "fcm_app_id": "1:60875194502:android:" + "b" * 40,
    "fcm_sender_id": "60875194502",
}


def _make_listener(**overrides: str) -> AjaxNotificationListener:
    hass = MagicMock()
    # A real loop marshals the store write; these tests drive the record
    # directly, so keep the threadsafe hop out of the way.
    hass.loop = None
    return AjaxNotificationListener(
        hass=hass,
        coordinator=MagicMock(),
        **{**_FCM_KWARGS, **overrides},
    )


class TestDeliveryRecord:
    """The in-memory half of the record."""

    def test_starts_undelivered(self) -> None:
        listener = _make_listener()
        assert listener.ever_delivered is False
        assert listener.first_delivery_at is None

    def test_first_delivery_is_recorded_once(self) -> None:
        listener = _make_listener()

        listener._note_push_delivered()
        first = listener.first_delivery_at
        assert listener.ever_delivered is True
        assert first is not None

        # A later push must not move the timestamp: "when did this credential
        # set first prove itself" is the question, and it has one answer.
        listener._note_push_delivered()
        assert listener.first_delivery_at == first

    def test_fingerprint_is_not_the_credentials(self) -> None:
        listener = _make_listener()
        fingerprint = listener.creds_fingerprint

        assert fingerprint
        for value in _FCM_KWARGS.values():
            assert value not in fingerprint
        # Short enough to be a fingerprint rather than a re-derivable digest.
        assert len(fingerprint) <= 16

    def test_fingerprint_changes_with_the_credentials(self) -> None:
        assert _make_listener().creds_fingerprint != (
            _make_listener(**_OTHER_FCM_KWARGS).creds_fingerprint
        )


class TestDeliveryRecordPersistence:
    """The stored half: it must survive a restart, and reset on new values."""

    @pytest.mark.asyncio
    async def test_loads_a_matching_record(self) -> None:
        listener = _make_listener()
        stored = {
            "hash": listener.creds_fingerprint,
            "first_delivery_at": "2026-08-01T10:00:00+00:00",
        }
        listener._delivery_store.async_load = _async_return(stored)

        await listener._async_load_delivery_record()

        assert listener.ever_delivered is True
        assert listener.first_delivery_at == "2026-08-01T10:00:00+00:00"

    @pytest.mark.asyncio
    async def test_ignores_a_record_from_other_credentials(self) -> None:
        """Re-entering the four values must reset the evidence.

        Otherwise a user who fixes broken credentials keeps a record saying
        delivery already worked, and the detector this record exists to feed
        would never fire again.
        """
        listener = _make_listener()
        listener._delivery_store.async_load = _async_return(
            {"hash": "not-this-credential-set", "first_delivery_at": "2026-08-01T10:00:00+00:00"}
        )

        await listener._async_load_delivery_record()

        assert listener.ever_delivered is False
        assert listener.first_delivery_at is None

    @pytest.mark.asyncio
    async def test_no_record_at_all(self) -> None:
        listener = _make_listener()
        listener._delivery_store.async_load = _async_return(None)

        await listener._async_load_delivery_record()

        assert listener.ever_delivered is False

    @pytest.mark.asyncio
    async def test_a_load_failure_does_not_break_startup(self) -> None:
        """The record is diagnostic; it must never be able to stop push."""
        listener = _make_listener()

        async def _boom() -> dict[str, Any]:
            raise OSError("storage unavailable")

        listener._delivery_store.async_load = _boom

        await listener._async_load_delivery_record()

        assert listener.ever_delivered is False

    @pytest.mark.asyncio
    async def test_saves_the_fingerprint_and_timestamp(self) -> None:
        listener = _make_listener()
        saved: list[dict[str, Any]] = []

        async def _save(data: dict[str, Any]) -> None:
            saved.append(data)

        listener._delivery_store.async_save = _save
        listener._note_push_delivered()

        await listener._async_persist_delivery_record()

        assert len(saved) == 1
        assert saved[0]["hash"] == listener.creds_fingerprint
        assert saved[0]["first_delivery_at"] == listener.first_delivery_at
        # The four values must never reach disk here — the credentials store
        # is a different key and this one is only ever a fingerprint.
        for value in _FCM_KWARGS.values():
            assert value not in str(saved[0])


class TestDeliveryRecordDoesNotDisturbThePushPath:
    """The mark must not add a thread hop, and must still reach disk."""

    def test_marking_schedules_nothing(self) -> None:
        """`_on_notification` marshals exactly one call to the loop (the
        refresh). A record written once per credential set does not justify a
        second hop on every push, and adding one silently broke ten existing
        push tests when this was first written that way."""
        listener = _make_listener()
        listener._hass = MagicMock()

        listener._note_push_delivered()

        listener._hass.loop.call_soon_threadsafe.assert_not_called()
        assert listener._delivery_record_unsaved is True

    @pytest.mark.asyncio
    async def test_supervisor_flushes_the_mark(self) -> None:
        listener = _make_listener()
        saved: list[dict[str, Any]] = []

        async def _save(data: dict[str, Any]) -> None:
            saved.append(data)

        listener._delivery_store.async_save = _save
        listener._note_push_delivered()

        await listener._async_supervise_push_client(now=1000.0)

        assert len(saved) == 1
        assert listener._delivery_record_unsaved is False

    @pytest.mark.asyncio
    async def test_supervisor_does_not_rewrite_a_saved_record(self) -> None:
        """Otherwise every supervisor tick writes to `.storage` forever."""
        listener = _make_listener()
        saved: list[dict[str, Any]] = []

        async def _save(data: dict[str, Any]) -> None:
            saved.append(data)

        listener._delivery_store.async_save = _save
        listener._note_push_delivered()

        await listener._async_supervise_push_client(now=1000.0)
        await listener._async_supervise_push_client(now=1060.0)

        assert len(saved) == 1

    @pytest.mark.asyncio
    async def test_a_save_failure_is_retried_next_tick(self) -> None:
        listener = _make_listener()
        attempts: list[int] = []

        async def _boom(_data: dict[str, Any]) -> None:
            attempts.append(1)
            raise OSError("storage unavailable")

        listener._delivery_store.async_save = _boom
        listener._note_push_delivered()

        await listener._async_supervise_push_client(now=1000.0)
        assert listener._delivery_record_unsaved is True
        await listener._async_supervise_push_client(now=1060.0)

        assert len(attempts) == 2


def _async_return(value: object) -> Callable[[], Awaitable[object]]:
    async def _inner() -> object:
        return value

    return _inner
