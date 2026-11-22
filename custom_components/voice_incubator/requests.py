"""Requests Home Assistant makes of the app: ``voice_incubator.ask_question``.

The app is a websocket client of Home Assistant, so Home Assistant cannot call
it; instead the app subscribes (``voice_incubator/requests/subscribe``) and is
sent each request as an event. It answers with ``voice_incubator/requests/result``.

``ask_question`` has a satellite speak a question, listen for the reply without
a wake word, and returns what was heard, matched against the automation's
answers here, with hassil, as Home Assistant's own
``assist_satellite.ask_question`` does: ``{"id", "sentence", "slots"}``.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import DOMAIN

DATA_REQUESTS = f"{DOMAIN}_requests"
SERVICE_ASK_QUESTION = "ask_question"
DEFAULT_TIMEOUT = 30

ASK_QUESTION_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("question"): cv.string,
        vol.Optional("answers"): [
            {
                vol.Required("id"): cv.string,
                vol.Required("sentences"): vol.All(cv.ensure_list, [cv.string]),
            }
        ],
        vol.Optional("preannounce", default=True): cv.boolean,
        vol.Optional("timeout", default=DEFAULT_TIMEOUT): vol.All(vol.Coerce(float), vol.Range(min=1, max=300)),
    }
)


class Requests:
    """The app's subscription, and the requests waiting on its result."""

    def __init__(self) -> None:
        self.connection: websocket_api.ActiveConnection | None = None
        self.subscription: int | None = None
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @callback
    def subscribe(self, connection: websocket_api.ActiveConnection, msg_id: int) -> None:
        # One app: a new subscription replaces the old (a reconnect). What
        # was sent over the old one won't be answered: fail it now, not at
        # its timeout.
        if self.connection is not None and self.connection is not connection:
            self.fail_all("app reconnected")
        self.connection, self.subscription = connection, msg_id

    @callback
    def unsubscribe(self, connection: websocket_api.ActiveConnection, msg_id: int) -> None:
        if self.connection is connection and self.subscription == msg_id:
            self.connection = self.subscription = None
            self.fail_all("app disconnected")

    @callback
    def fail_all(self, reason: str) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_result({"text": None, "error": reason})
        self.pending.clear()

    async def request(self, event: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Send the app a request and wait for its result."""
        if self.connection is None or self.subscription is None:
            raise HomeAssistantError("The Voice Incubator app is not connected")
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.connection.send_message(
            websocket_api.event_message(self.subscription, {"request_id": request_id, **event})
        )
        try:
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError:
            if self.connection is not None and self.subscription is not None:
                # The app stops listening for it.
                self.connection.send_message(
                    websocket_api.event_message(self.subscription, {"request_id": request_id, "kind": "cancel"})
                )
            return {"text": None, "error": "timeout"}
        finally:
            self.pending.pop(request_id, None)

    @callback
    def resolve(self, request_id: str, result: dict[str, Any]) -> bool:
        future = self.pending.pop(request_id, None)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True


@callback
def _requests(hass: HomeAssistant) -> Requests:
    return hass.data.setdefault(DATA_REQUESTS, Requests())


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_subscribe)
    websocket_api.async_register_command(hass, websocket_result)
    hass.services.async_register(
        DOMAIN,
        SERVICE_ASK_QUESTION,
        _ask_question,
        schema=ASK_QUESTION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


@websocket_api.websocket_command({vol.Required("type"): "voice_incubator/requests/subscribe"})
@websocket_api.require_admin
@callback
def websocket_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The app listens for requests; each comes as an event."""
    requests = _requests(hass)
    requests.subscribe(connection, msg["id"])

    @callback
    def unsubscribe() -> None:
        requests.unsubscribe(connection, msg["id"])

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "voice_incubator/requests/result",
        vol.Required("request_id"): cv.string,
        vol.Optional("text"): vol.Any(cv.string, None),
        vol.Optional("error"): vol.Any(cv.string, None),
    }
)
@websocket_api.require_admin
@callback
def websocket_result(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The app's result for a request: what was heard, or why nothing was."""
    known = _requests(hass).resolve(msg["request_id"], {"text": msg.get("text"), "error": msg.get("error")})
    connection.send_result(msg["id"], {"known": known})


def _satellite_for(hass: HomeAssistant, device_id: str) -> tuple[str, str | None]:
    """The app's key for the satellite whose device this is (its own, or its
    ESPHome device's, by MAC), and its area."""
    from . import _satellites  # pylint: disable=import-outside-toplevel

    satellites = _satellites(hass)
    if satellites is None:
        raise ServiceValidationError("Voice Incubator is not set up")
    devices = dr.async_get(hass)
    device = devices.async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"Unknown device: {device_id}")
    known = {s.id: s for s in satellites.all()}
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier in known:
            own = device
            return identifier, own.area_id
    for kind, value in device.connections:
        if kind == dr.CONNECTION_NETWORK_MAC:
            key = value.replace(":", "").lower()
            if key in known:
                own = devices.async_get_device(identifiers={(DOMAIN, key)})
                return key, (own.area_id if own is not None else device.area_id)
    raise ServiceValidationError(f"{device.name_by_user or device.name} is not a Voice Incubator satellite")


_SLOT = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::[a-zA-Z0-9_]+)?\}")


def match_answer(text: str | None, answers: list[dict[str, Any]] | None, language: str) -> dict[str, Any]:
    """Which answer the reply is, as Home Assistant's assist_satellite does:
    each answer's sentences are hassil templates; a ``{name}`` in one is a
    wildcard slot."""
    result: dict[str, Any] = {"id": None, "sentence": text or "", "slots": {}}
    if not text or not answers:
        return result
    from hassil import Intents, recognize_best  # pylint: disable=import-outside-toplevel

    slots = {name for answer in answers for sentence in answer["sentences"] for name in _SLOT.findall(sentence)}
    intents = Intents.from_dict(
        {
            "language": language,
            "intents": {
                "QuestionIntent": {
                    "data": [
                        {"sentences": answer["sentences"], "metadata": {"answer_id": answer["id"]}}
                        for answer in answers
                    ]
                }
            },
            "lists": {name: {"wildcard": True} for name in slots},
        }
    )
    recognized = recognize_best(text, intents)
    if recognized is None or not recognized.intent_metadata:
        return result
    result["id"] = recognized.intent_metadata.get("answer_id")
    result["slots"] = {name: entity.value for name, entity in recognized.entities.items()}
    return result


async def _ask_question(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    from . import _satellites  # pylint: disable=import-outside-toplevel

    key, area_id = _satellite_for(hass, call.data["device_id"])
    satellites = _satellites(hass)
    switch = satellites.do_not_disturb.get(area_id) if satellites and area_id else None
    if switch is not None and switch.is_on:
        raise ServiceValidationError("Do not disturb is on where this satellite is")
    answers = call.data.get("answers")
    reply = await _requests(hass).request(
        {
            "kind": "ask",
            "satellite": key,
            "question": call.data.get("question") or "",
            "sentences": [s for a in answers or [] for s in a["sentences"]],
            "preannounce": call.data["preannounce"],
        },
        call.data["timeout"],
    )
    error = reply.get("error")
    if error and error != "timeout":
        raise HomeAssistantError(f"The satellite could not ask: {error}")
    return match_answer(reply.get("text"), answers, hass.config.language)
