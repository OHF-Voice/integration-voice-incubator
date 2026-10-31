"""What the app streams through Home Assistant: a conversation agent's answer
as it is made, and speech whose text comes a sentence at a time.

``voice_labs/conversation/stream`` asks a conversation agent as Assist does:
in a chat log opened with a delta listener, so each piece of the answer comes
back as the agent makes it (``delta`` events), then the result as
``conversation/process`` gives it. The chat log keeps the whole turn under the
conversation's id -- what was asked, what the agent said and did, and the
answer of an agent that doesn't write to it itself -- as Assist's would.

``voice_labs/tts/stream`` speaks on a media player with a TTS entity, its text
given a piece at a time (``voice_labs/tts/stream/add``): a TTS entity that
streams its input speaks each piece as it comes, and the media player plays
it as it is made. One that doesn't gets the whole text at the end, and the
media player is only told to play it then.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol

from homeassistant.components import conversation, tts, websocket_api
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import chat_session, config_validation as cv

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_TTS_STREAMS = f"{DOMAIN}_tts_streams"

_SESSION_CONTEXT = "context" in inspect.signature(chat_session.async_get_chat_session).parameters
"""Whether a chat session is told whose it is (Home Assistant from 2026.11)."""


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_conversation_stream)
    websocket_api.async_register_command(hass, websocket_tts_stream)
    websocket_api.async_register_command(hass, websocket_tts_stream_add)


# ------------------------------------------------------------- conversation


@websocket_api.websocket_command(
    {
        vol.Required("type"): "voice_labs/conversation/stream",
        vol.Required("text"): cv.string,
        vol.Optional("agent_id"): cv.string,
        vol.Optional("conversation_id"): vol.Any(cv.string, None),
        vol.Optional("language"): cv.string,
        vol.Optional("device_id"): cv.string,
    }
)
@websocket_api.require_admin
@callback
def websocket_conversation_stream(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Ask a conversation agent, its answer streamed: a subscription whose
    events are each piece of the chat log as it comes (``delta``: role,
    content, the tools called), then ``result`` (as conversation/process
    gives it) or ``error``. Unsubscribing ends the turn."""
    msg_id = msg["id"]
    context = connection.context(msg)

    @callback
    def send(event: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg_id, event))

    @callback
    def on_delta(_chat_log: conversation.ChatLog, delta: dict) -> None:
        send({"type": "delta", "delta": _delta_json(delta)})

    async def converse() -> None:
        try:
            result = await _converse(hass, context, msg, on_delta)
        except (HomeAssistantError, ValueError) as err:
            # Busy with another turn, no such agent, not this user's conversation.
            send({"type": "error", "code": type(err).__name__, "message": str(err)})
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.exception("Conversation agent failed")
            send({"type": "error", "code": type(err).__name__, "message": str(err)})
        else:
            send({"type": "result", "result": result.as_dict()})
        finally:
            connection.subscriptions.pop(msg_id, None)

    task = hass.async_create_task(converse(), f"voice_labs conversation {msg_id}", eager_start=False)
    connection.subscriptions[msg_id] = task.cancel
    connection.send_result(msg_id)


async def _converse(
    hass: HomeAssistant, context: Context, msg: dict[str, Any], on_delta: Callable[[conversation.ChatLog, dict], None]
) -> conversation.ConversationResult:
    """The agent's answer, in a chat log of the conversation's that hears
    its deltas, as Assist's pipeline asks it."""
    language = msg.get("language") or hass.config.language
    agent_id = msg.get("agent_id") or conversation.HOME_ASSISTANT_AGENT
    user_input = conversation.ConversationInput(
        text=msg["text"],
        context=context,
        conversation_id=msg.get("conversation_id"),
        device_id=msg.get("device_id"),
        satellite_id=None,
        language=language,
        agent_id=agent_id,
    )
    session_kwargs: dict[str, Any] = {"context": context} if _SESSION_CONTEXT else {}
    with (
        chat_session.async_get_chat_session(hass, user_input.conversation_id, **session_kwargs) as session,
        conversation.async_get_chat_log(hass, session, user_input, chat_log_delta_listener=on_delta) as chat_log,
    ):
        result = await conversation.async_converse(
            hass=hass,
            text=user_input.text,
            conversation_id=session.conversation_id,
            context=context,
            language=language,
            agent_id=agent_id,
            device_id=user_input.device_id,
        )
        speech = result.response.speech.get("plain", {}).get("speech", "")
        # An agent that doesn't write to the chat log only answers in its result.
        if chat_log.content[-1].role == "user" and speech:
            chat_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(agent_id=agent_id, content=speech)
            )
    return result


def _delta_json(delta: dict) -> dict[str, Any]:
    """A chat log delta as the app reads it: its text, and the tools called
    by name (their arguments and results stay in the log)."""
    out: dict[str, Any] = {
        key: delta[key] for key in ("role", "content", "thinking_content", "tool_name") if isinstance(delta.get(key), str)
    }
    if delta.get("tool_calls"):
        out["tool_calls"] = [getattr(call, "tool_name", None) for call in delta["tool_calls"]]
    return out


# ---------------------------------------------------------------------- TTS


@dataclass
class _TextStream:
    """A TTS stream's text as it comes, and how to end it."""

    queue: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    play: Callable[[], Any] | None = None
    """Told to play only when its text is whole (a TTS entity that doesn't
    stream its input)."""
    end: Callable[[], None] | None = None
    """No more text: the subscription that made it ends too."""


@websocket_api.websocket_command(
    {
        vol.Required("type"): "voice_labs/tts/stream",
        vol.Required("engine_id"): cv.string,
        vol.Required("media_player"): cv.entity_id,
        vol.Optional("text", default=""): str,
        vol.Optional("language"): cv.string,
        vol.Optional("options"): dict,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_tts_stream(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Speak on ``media_player`` with the TTS entity ``engine_id`` as an
    announcement, its text given a piece at a time: ``text`` now, more with
    voice_labs/tts/stream/add (and ``end`` when there is no more). The result
    names the stream (``stream``). Unsubscribing ends its text."""
    msg_id = msg["id"]
    try:
        stream = tts.async_create_stream(hass, msg["engine_id"], msg.get("language"), msg.get("options"))
    except HomeAssistantError as err:
        connection.send_error(msg_id, "tts_failed", str(err))
        return
    text = _TextStream()
    if msg["text"]:
        text.queue.put_nowait(msg["text"])

    async def message() -> AsyncGenerator[str]:
        while (piece := await text.queue.get()) is not None:
            yield piece

    stream.async_set_message_stream(message())
    streams: dict[str, _TextStream] = hass.data.setdefault(DATA_TTS_STREAMS, {})
    streams[stream.token] = text

    async def play() -> None:
        await hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": msg["media_player"],
                "media_content_id": stream.media_source_id,
                "media_content_type": "music",
                "announce": True,
            },
            blocking=True,
            context=connection.context(msg),
        )

    @callback
    def end() -> None:
        """No more text: what came is all that is said."""
        connection.subscriptions.pop(msg_id, None)
        if streams.pop(stream.token, None) is not None:
            text.queue.put_nowait(None)

    text.end = end
    connection.subscriptions[msg_id] = end
    if stream.supports_streaming_input:
        try:
            await play()
        except HomeAssistantError as err:
            end()
            connection.send_error(msg_id, "play_failed", str(err))
            return
    else:

        async def play_whole() -> None:
            try:
                await play()
            except HomeAssistantError as err:
                _LOGGER.warning("Could not play %s on %s: %s", stream.token, msg["media_player"], err)

        text.play = play_whole
    connection.send_result(msg_id, {"stream": stream.token, "streaming_input": stream.supports_streaming_input})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "voice_labs/tts/stream/add",
        vol.Required("stream"): cv.string,
        vol.Optional("text", default=""): str,
        vol.Optional("end", default=False): bool,
    }
)
@websocket_api.require_admin
@callback
def websocket_tts_stream_add(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """More of a stream's text (voice_labs/tts/stream), and (``end``) that
    there is no more: then a TTS entity that doesn't stream its input says it
    all."""
    streams: dict[str, _TextStream] = hass.data.get(DATA_TTS_STREAMS, {})
    text = streams.get(msg["stream"])
    if text is None:
        connection.send_error(msg["id"], "not_found", "No such stream, or its text has ended")
        return
    if msg["text"]:
        text.queue.put_nowait(msg["text"])
    if msg["end"]:
        assert text.end is not None
        text.end()
        if text.play is not None:
            hass.async_create_task(text.play(), f"voice_labs tts {msg['stream']}")
    connection.send_result(msg["id"])
