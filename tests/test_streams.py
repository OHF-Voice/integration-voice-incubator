"""Streams: a conversation agent's answer as it is made, kept in its chat log;
and speech whose text comes a sentence at a time."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import DATA_CHAT_LOGS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import chat_session, intent

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service
from pytest_homeassistant_custom_component.typing import WebSocketGenerator


async def _setup(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="voice_labs", title="Voice Labs")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


class LlmAgent(conversation.AbstractConversationAgent):
    """An LLM's kind of agent: its answer written to the chat log as it comes."""

    def __init__(self, hass: HomeAssistant, pieces: list[str]) -> None:
        self.hass = hass
        self.pieces = pieces

    @property
    def supported_languages(self) -> str:
        return "*"

    async def async_process(self, user_input: conversation.ConversationInput) -> conversation.ConversationResult:
        with (
            chat_session.async_get_chat_session(self.hass, user_input.conversation_id) as session,
            conversation.async_get_chat_log(self.hass, session, user_input) as chat_log,
        ):

            async def deltas():
                yield {"role": "assistant"}
                for piece in self.pieces:
                    yield {"content": piece}

            async for _content in chat_log.async_add_delta_content_stream(user_input.agent_id, deltas()):
                pass
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_speech(chat_log.content[-1].content)
            return conversation.ConversationResult(response=response, conversation_id=chat_log.conversation_id)


class PlainAgent(conversation.AbstractConversationAgent):
    """An agent of the older kind: it only answers in its result."""

    @property
    def supported_languages(self) -> str:
        return "*"

    async def async_process(self, user_input: conversation.ConversationInput) -> conversation.ConversationResult:
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech("Plainly so.")
        return conversation.ConversationResult(response=response, conversation_id=user_input.conversation_id)


def _agent(hass: HomeAssistant, agent: conversation.AbstractConversationAgent) -> str:
    entry = MockConfigEntry(domain="test_agent")
    entry.add_to_hass(hass)
    conversation.async_set_agent(hass, entry, agent)
    return entry.entry_id


async def _ask(client, **message: Any) -> list[dict[str, Any]]:
    """The events of one question, through its result or error."""
    await client.send_json_auto_id({"type": "voice_labs/conversation/stream", **message})
    msg = await client.receive_json()
    assert msg["success"], msg
    events = []
    while True:
        msg = await client.receive_json()
        events.append(msg["event"])
        if msg["event"]["type"] in ("result", "error"):
            return events


async def test_an_answer_streamed_and_kept(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    await _setup(hass)
    agent_id = _agent(hass, LlmAgent(hass, ["The tallest mountain ", "is Everest. ", "It is 8849 meters."]))
    client = await hass_ws_client(hass)

    events = await _ask(client, text="what's the tallest mountain", agent_id=agent_id, language="en")
    # Each piece as it was made, then the result as conversation/process gives it.
    deltas = [e["delta"] for e in events if e["type"] == "delta"]
    assert {"role": "assistant"} in deltas
    assert [d["content"] for d in deltas if "content" in d] == ["The tallest mountain ", "is Everest. ", "It is 8849 meters."]
    result = events[-1]["result"]
    assert result["response"]["speech"]["plain"]["speech"] == "The tallest mountain is Everest. It is 8849 meters."
    conversation_id = result["conversation_id"]

    # The chat log keeps the turn under the conversation's id; the next
    # question in it goes on there.
    log = hass.data[DATA_CHAT_LOGS][conversation_id]
    assert [(c.role, c.content) for c in log.content[1:]] == [
        ("user", "what's the tallest mountain"),
        ("assistant", "The tallest mountain is Everest. It is 8849 meters."),
    ]
    events = await _ask(client, text="how tall is it", agent_id=agent_id, conversation_id=conversation_id, language="en")
    assert events[-1]["result"]["conversation_id"] == conversation_id
    assert [c.role for c in hass.data[DATA_CHAT_LOGS][conversation_id].content[1:]] == [
        "user", "assistant", "user", "assistant"
    ]


async def test_an_answer_only_in_the_result_is_kept_too(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    await _setup(hass)
    agent_id = _agent(hass, PlainAgent())
    client = await hass_ws_client(hass)
    events = await _ask(client, text="is it so", agent_id=agent_id, language="en")
    assert [e["type"] for e in events] == ["result"]
    conversation_id = events[-1]["result"]["conversation_id"]
    assert [(c.role, c.content) for c in hass.data[DATA_CHAT_LOGS][conversation_id].content[1:]] == [
        ("user", "is it so"), ("assistant", "Plainly so.")
    ]

    events = await _ask(client, text="anything", agent_id="conversation.nobody", language="en")
    assert events[-1]["type"] == "error"


class FakeStream:
    """tts.async_create_stream's, as far as the command uses it."""

    token = "abc"
    media_source_id = "media-source://tts/-stream-/abc"

    def __init__(self, streaming_input: bool) -> None:
        self.supports_streaming_input = streaming_input
        self.message = None

    def async_set_message_stream(self, message) -> None:
        self.message = message


async def _speak(hass: HomeAssistant, client, streaming_input: bool) -> tuple[FakeStream, list]:
    stream = FakeStream(streaming_input)
    played = async_mock_service(hass, "media_player", "play_media")
    with patch("custom_components.voice_labs.streams.tts.async_create_stream", return_value=stream):
        await client.send_json_auto_id({
            "type": "voice_labs/tts/stream", "engine_id": "tts.cloud", "media_player": "media_player.vpe",
            "text": "First sentence. ", "language": "en-US",
        })
        msg = await client.receive_json()
    assert msg["success"], msg
    assert msg["result"] == {"stream": "abc", "streaming_input": streaming_input}
    return stream, played


async def _add(client, **message: Any) -> dict:
    await client.send_json_auto_id({"type": "voice_labs/tts/stream/add", "stream": "abc", **message})
    return await client.receive_json()


async def test_speech_a_sentence_at_a_time(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)
    # A TTS entity that streams its input: played at once, each sentence as it comes.
    stream, played = await _speak(hass, client, streaming_input=True)
    assert [c.data for c in played] == [{
        "entity_id": "media_player.vpe", "media_content_id": stream.media_source_id,
        "media_content_type": "music", "announce": True,
    }]
    assert (await _add(client, text="Second sentence."))["success"]
    assert (await _add(client, end=True))["success"]
    assert [piece async for piece in stream.message] == ["First sentence. ", "Second sentence."]
    assert (await _add(client, text="Too late."))["error"]["code"] == "not_found"


async def test_speech_whole_for_a_tts_that_waits_for_it(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)
    # One that doesn't: played once its text is whole.
    stream, played = await _speak(hass, client, streaming_input=False)
    assert not played
    assert (await _add(client, text="Second sentence.", end=True))["success"]
    await hass.async_block_till_done()
    assert len(played) == 1
    assert [piece async for piece in stream.message] == ["First sentence. ", "Second sentence."]


async def test_unsubscribing_ends_the_turn(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    """The call ended ("stop" over the answer): the agent stops too."""
    import asyncio  # pylint: disable=import-outside-toplevel

    await _setup(hass)
    started, ended = asyncio.Event(), asyncio.Event()

    class SlowAgent(PlainAgent):
        async def async_process(self, user_input):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

    agent_id = _agent(hass, SlowAgent())
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "voice_labs/conversation/stream", "text": "tell me a long story", "agent_id": agent_id})
    msg = await client.receive_json()
    assert msg["success"]
    await asyncio.wait_for(started.wait(), 5)
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": msg["id"]})
    assert (await client.receive_json())["success"]
    await asyncio.wait_for(ended.wait(), 5)
