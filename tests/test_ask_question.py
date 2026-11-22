"""voice_incubator.ask_question: the app is sent the request over its
subscription, answers with what it heard, and the reply is matched here."""

import asyncio

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar, device_registry as dr

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.voice_incubator.requests import match_answer

MAC = "d83bdaa3d5f4"
ANSWERS = [
    {"id": "yes", "sentences": ["yes [please]", "sure", "close it"]},
    {"id": "no", "sentences": ["no", "leave it"]},
    {"id": "temp", "sentences": ["set it to {temp} [degrees]"]},
]


async def _setup(hass: HomeAssistant, hass_ws_client: WebSocketGenerator, subscribe: bool = True):
    entry = MockConfigEntry(domain="voice_incubator", title="Voice Incubator")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "voice_incubator/satellites/sync", "satellites": [{"id": MAC, "name": "kitchen-vpe"}]}
    )
    msg = await client.receive_json()
    [satellite] = msg["result"]["satellites"]
    sub_id = None
    if subscribe:
        await client.send_json_auto_id({"type": "voice_incubator/requests/subscribe"})
        msg = await client.receive_json()
        assert msg["success"], msg
        sub_id = msg["id"]
    return client, satellite, sub_id


def _ask(hass: HomeAssistant, device_id: str, **data):
    return asyncio.ensure_future(
        hass.services.async_call(
            "voice_incubator",
            "ask_question",
            {"device_id": device_id, "question": "Close the garage?", "answers": ANSWERS, **data},
            blocking=True,
            return_response=True,
        )
    )


async def _reply(client, sub_id: int, text: str | None, error: str | None = None) -> dict:
    event = await client.receive_json()
    assert event["id"] == sub_id and event["type"] == "event"
    request = event["event"]
    await client.send_json_auto_id(
        {"type": "voice_incubator/requests/result", "request_id": request["request_id"], "text": text, "error": error}
    )
    msg = await client.receive_json()
    assert msg["success"] and msg["result"]["known"]
    return request


async def test_answer_matched(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    client, satellite, sub_id = await _setup(hass, hass_ws_client)
    task = _ask(hass, satellite["device_id"])
    request = await _reply(client, sub_id, "yes please")
    assert request["kind"] == "ask" and request["satellite"] == MAC
    assert request["question"] == "Close the garage?" and "leave it" in request["sentences"]
    assert request["preannounce"] is True
    assert await task == {"id": "yes", "sentence": "yes please", "slots": {}}


async def test_slots(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    client, satellite, sub_id = await _setup(hass, hass_ws_client)
    task = _ask(hass, satellite["device_id"])
    await _reply(client, sub_id, "set it to 21 degrees")
    result = await task
    assert result["id"] == "temp" and result["slots"]["temp"].strip() == "21"


async def test_no_match_and_silence(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    client, satellite, sub_id = await _setup(hass, hass_ws_client)
    task = _ask(hass, satellite["device_id"])
    await _reply(client, sub_id, "what is the weather")
    assert await task == {"id": None, "sentence": "what is the weather", "slots": {}}
    task = _ask(hass, satellite["device_id"])
    await _reply(client, sub_id, None)
    assert await task == {"id": None, "sentence": "", "slots": {}}


async def test_esphome_device_works_too(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    esphome = MockConfigEntry(domain="esphome")
    esphome.add_to_hass(hass)
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=esphome.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, "d8:3b:da:a3:d5:f4")}
    )
    client, _satellite, sub_id = await _setup(hass, hass_ws_client)
    task = _ask(hass, other.id)
    request = await _reply(client, sub_id, "no")
    assert request["satellite"] == MAC
    assert (await task)["id"] == "no"


async def test_timeout(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    client, satellite, sub_id = await _setup(hass, hass_ws_client)
    task = _ask(hass, satellite["device_id"], timeout=1)
    event = await client.receive_json()
    assert event["event"]["kind"] == "ask"
    assert await task == {"id": None, "sentence": "", "slots": {}}
    cancel = await client.receive_json()
    assert cancel["event"] == {"request_id": event["event"]["request_id"], "kind": "cancel"}


async def test_app_error(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    client, satellite, sub_id = await _setup(hass, hass_ws_client)
    task = _ask(hass, satellite["device_id"])
    await _reply(client, sub_id, None, "busy")
    with pytest.raises(HomeAssistantError, match="busy"):
        await task


async def test_no_app(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    _client, satellite, _ = await _setup(hass, hass_ws_client, subscribe=False)
    with pytest.raises(HomeAssistantError, match="not connected"):
        await _ask(hass, satellite["device_id"])


async def test_not_a_satellite(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    await _setup(hass, hass_ws_client)
    other = MockConfigEntry(domain="esphome")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=other.entry_id, identifiers={("esphome", "x")})
    with pytest.raises(ServiceValidationError):
        await _ask(hass, device.id)


async def test_do_not_disturb(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    _client, satellite, _ = await _setup(hass, hass_ws_client)
    area = ar.async_get(hass).async_create("Kitchen")
    dr.async_get(hass).async_update_device(satellite["device_id"], area_id=area.id)
    await hass.async_block_till_done()
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.kitchen_do_not_disturb"}, blocking=True)
    with pytest.raises(ServiceValidationError, match="Do not disturb"):
        await _ask(hass, satellite["device_id"])


async def test_disconnect_fails_pending(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    client, satellite, sub_id = await _setup(hass, hass_ws_client)
    task = _ask(hass, satellite["device_id"])
    await client.receive_json()
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": sub_id})
    with pytest.raises(HomeAssistantError, match="app disconnected"):
        await task


def test_match_answer_without_answers() -> None:
    assert match_answer("hello", None, "en") == {"id": None, "sentence": "hello", "slots": {}}


async def test_reconnect_fails_pending(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    client, satellite, _sub_id = await _setup(hass, hass_ws_client)
    task = _ask(hass, satellite["device_id"])
    await client.receive_json()
    other = await hass_ws_client(hass)
    await other.send_json_auto_id({"type": "voice_incubator/requests/subscribe"})
    assert (await other.receive_json())["success"]
    with pytest.raises(HomeAssistantError, match="app reconnected"):
        await task
