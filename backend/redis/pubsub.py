from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from backend.events.envelope import EventEnvelope

from .client import RedisRuntimeClient, RedisUnavailable
from .namespace import RedisNamespace


@dataclass(frozen=True, slots=True)
class StreamNotification:
    """Ephemeral pointer to an event already appended to the Redis Stream."""

    player_id: str
    session_id: str
    sequence: int

    @property
    def stream_id(self) -> str:
        return f"{self.sequence}-0"

    def encode(self) -> str:
        return json.dumps(
            {
                "player_id": self.player_id,
                "session_id": self.session_id,
                "sequence": self.sequence,
                "stream_id": self.stream_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def decode(cls, value: bytes | str) -> "StreamNotification":
        try:
            decoded = json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid Pub/Sub stream notification") from error
        if not isinstance(decoded, dict):
            raise ValueError("Pub/Sub stream notification must be an object")
        player_id = decoded.get("player_id")
        session_id = decoded.get("session_id")
        sequence = decoded.get("sequence")
        stream_id = decoded.get("stream_id")
        if not isinstance(player_id, str) or not isinstance(session_id, str):
            raise ValueError("Pub/Sub notification identity is invalid")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("Pub/Sub notification sequence is invalid")
        notification = cls(player_id, session_id, sequence)
        if stream_id != notification.stream_id:
            raise ValueError("Pub/Sub notification stream ID does not match its sequence")
        return notification


class RedisLiveSubscription:
    def __init__(
        self,
        runtime_client: RedisRuntimeClient,
        pubsub: Any,
        *,
        player_id: str,
        channel: str,
    ) -> None:
        self._runtime_client = runtime_client
        self._pubsub = pubsub
        self.player_id = player_id
        self.channel = channel
        self._closed = False

    def receive(self, *, timeout: float = 0.0) -> StreamNotification | None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a finite non-negative number")
        if not math.isfinite(timeout) or timeout < 0 or timeout > 30:
            raise ValueError("timeout must be between 0 and 30 seconds")
        if self._closed:
            return None
        try:
            message = self._pubsub.get_message(timeout=timeout)
        except Exception as error:
            self._runtime_client.report_failure(error)
            raise RedisUnavailable("Redis Pub/Sub receive failed") from error
        if message is None or message.get("type") != "message":
            return None
        channel = _text(message.get("channel"))
        if channel != self.channel:
            return None
        notification = StreamNotification.decode(message.get("data", ""))
        if notification.player_id != self.player_id:
            raise ValueError("Pub/Sub notification was delivered on the wrong player channel")
        return notification

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._pubsub.unsubscribe(self.channel)
        finally:
            close = getattr(self._pubsub, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "RedisLiveSubscription":
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()


class RedisLivePubSub:
    """Send transient wake-up pointers; the stream remains the replay source."""

    def __init__(self, client: RedisRuntimeClient) -> None:
        self.client = client

    def publish(self, event: EventEnvelope) -> bool:
        if not isinstance(event, EventEnvelope):
            raise TypeError("event must be an EventEnvelope")
        if event.type == "state.snapshot" or event.sequence < 1:
            raise ValueError("only canonical sequenced player events can be published")
        channel = RedisNamespace.live_pubsub(event.player_id)
        notification = StreamNotification(
            player_id=event.player_id,
            session_id=event.session_id,
            sequence=event.sequence,
        )
        try:
            result = self.client.execute(
                lambda redis_client: redis_client.publish(channel, notification.encode())
            )
        except RedisUnavailable:
            # Fanout is advisory only; callers keep local control and durable
            # event handling even when Redis is offline or intentionally absent.
            return False
        return isinstance(result, int) and not isinstance(result, bool) and result >= 0

    def subscribe(self, player_id: str) -> RedisLiveSubscription:
        channel = RedisNamespace.live_pubsub(player_id)

        def create_subscription(redis_client: Any) -> RedisLiveSubscription:
            pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
            try:
                pubsub.subscribe(channel)
            except Exception:
                close = getattr(pubsub, "close", None)
                if callable(close):
                    close()
                raise
            return RedisLiveSubscription(
                self.client,
                pubsub,
                player_id=player_id,
                channel=channel,
            )

        return self.client.execute(create_subscription)


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)
