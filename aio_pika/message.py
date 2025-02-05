import json
import time
from datetime import datetime, timedelta
from functools import singledispatch
from pprint import pformat
from types import TracebackType
from typing import (
    Any, Callable, Dict, Iterable, Iterator, List, MutableMapping, Optional,
    Type, TypeVar, Union,
)

import aiormq
from aiormq.abc import DeliveredMessage, FieldTable
from pamqp.common import FieldValue

from .abc import (
    MILLISECONDS, ZERO_TIME, AbstractChannel, AbstractIncomingMessage,
    AbstractMessage, AbstractProcessContext, DateType, DeliveryMode,
    HeadersPythonValues, HeadersType, NoneType,
)
from .exceptions import MessageProcessError
from .log import get_logger


log = get_logger(__name__)


def to_milliseconds(seconds: Union[float, int]) -> int:
    return int(seconds * MILLISECONDS)


@singledispatch
def encode_expiration(value: Any) -> Optional[str]:
    raise ValueError("Invalid timestamp type: %r" % type(value), value)


@encode_expiration.register(datetime)
def encode_expiration_datetime(value: datetime) -> str:
    now = datetime.now(tz=value.tzinfo)
    return str(to_milliseconds((value - now).total_seconds()))


@encode_expiration.register(int)
@encode_expiration.register(float)
def encode_expiration_number(value: Union[int, float]) -> str:
    return str(to_milliseconds(value))


@encode_expiration.register(timedelta)
def encode_expiration_timedelta(value: timedelta) -> str:
    return str(int(value.total_seconds() * MILLISECONDS))


@encode_expiration.register(NoneType)       # type: ignore
def encode_expiration_none(_: Any) -> None:
    return None


@singledispatch
def decode_expiration(t: Any) -> Optional[float]:
    raise ValueError("Invalid expiration type: %r" % type(t), t)


@decode_expiration.register(time.struct_time)
def decode_expiration_struct_time(t: time.struct_time) -> float:
    return (datetime(*t[:7]) - ZERO_TIME).total_seconds()


@decode_expiration.register(str)
def decode_expiration_str(t: str) -> float:
    return float(t)


@singledispatch
def encode_timestamp(value: Any) -> Optional[datetime]:
    raise ValueError("Invalid timestamp type: %r" % type(value), value)


@encode_timestamp.register(time.struct_time)
def encode_timestamp_struct_time(value: time.struct_time) -> datetime:
    return datetime(*value[:6])


@encode_timestamp.register(datetime)
def encode_timestamp_datetime(value: datetime) -> datetime:
    return value


@encode_timestamp.register(float)
@encode_timestamp.register(int)
def encode_timestamp_number(value: Union[int, float]) -> datetime:
    return datetime.utcfromtimestamp(value)


@encode_timestamp.register(timedelta)
def encode_timestamp_timedelta(value: timedelta) -> datetime:
    return datetime.utcnow() + value


@encode_timestamp.register(NoneType)        # type: ignore
def encode_timestamp_none(_: Any) -> None:
    return None


@singledispatch
def decode_timestamp(value: Any) -> Optional[datetime]:
    raise ValueError("Invalid timestamp type: %r" % type(value), value)


@decode_timestamp.register(datetime)
def decode_timestamp_datetime(value: datetime) -> datetime:
    return value


@decode_timestamp.register(float)
@decode_timestamp.register(int)
def decode_timestamp_number(value: Union[float, int]) -> datetime:
    return datetime.utcfromtimestamp(value)


@decode_timestamp.register(time.struct_time)
def decode_timestamp_struct_time(value: time.struct_time) -> datetime:
    return datetime(*value[:6])


@decode_timestamp.register(NoneType)    # type: ignore
def decode_timestamp_none(_: Any) -> None:
    return None


V = TypeVar("V")
D = TypeVar("D")
T = TypeVar("T")


def optional(
    value: V,
    func: Union[Callable[[V], T], Type[T]],
    default: D = None,
) -> Union[T, D]:
    return func(value) if value else default    # type: ignore


class HeaderProxy(MutableMapping):
    def __init__(self, headers: FieldTable):
        self._headers: FieldTable = headers
        self._cache: Dict[str, Any] = {}

    def __getitem__(self, k: str) -> FieldValue:
        if k not in self._headers:
            raise KeyError(k)

        if k not in self._cache:
            value = self._headers[k]

            if isinstance(value, bytes):
                self._cache[k] = value.decode()
            else:
                self._cache[k] = value

        return self._cache[k]

    def __delitem__(self, key: str) -> None:
        del self._headers[key]

    def __setitem__(self, key: str, value: FieldValue) -> None:
        self._headers[key] = header_converter(value)
        self._cache.pop(key, None)

    def __len__(self) -> int:
        return len(self._headers)

    def __iter__(self) -> Iterator[str]:
        for key in self._headers:
            yield key


@singledispatch
def header_converter(value: Any) -> FieldValue:
    return bytearray(
        json.dumps(
            value,
            separators=(",", ":"),
            ensure_ascii=False,
            default=repr,
        ).encode(),
    )


@header_converter.register(NoneType)        # type: ignore
@header_converter.register(bytearray)
@header_converter.register(str)
@header_converter.register(datetime)
@header_converter.register(time.struct_time)
@header_converter.register(list)
@header_converter.register(int)
def header_converter_native(v: T) -> T:
    return v


@header_converter.register(bytes)
def header_converter_bytes(v: bytes) -> bytearray:
    return bytearray(v)


@header_converter.register(set)
@header_converter.register(tuple)
@header_converter.register(frozenset)
def header_converter_iterable(v: Iterable[T]) -> List[T]:
    return header_converter(list(v))        # type: ignore


def format_headers(d: Optional[HeadersType]) -> FieldTable:
    ret: FieldTable = {}

    if not d:
        return ret

    for key, value in d.items():
        ret[key] = header_converter(value)
    return ret


class Message(AbstractMessage):
    """ AMQP message abstraction """

    __slots__ = (
        "app_id",
        "body",
        "body_size",
        "content_encoding",
        "content_type",
        "correlation_id",
        "delivery_mode",
        "expiration",
        "_headers",
        "headers_raw",
        "message_id",
        "priority",
        "reply_to",
        "timestamp",
        "type",
        "user_id",
        "__lock",
    )

    def __init__(
        self,
        body: bytes,
        *,
        headers: HeadersType = None,
        content_type: Optional[str] = None,
        content_encoding: Optional[str] = None,
        delivery_mode: Union[DeliveryMode, int] = None,
        priority: Optional[int] = None,
        correlation_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        expiration: Optional[DateType] = None,
        message_id: Optional[str] = None,
        timestamp: Optional[DateType] = None,
        type: Optional[str] = None,
        user_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ):

        """ Creates a new instance of Message

        :param body: message body
        :param headers: message headers
        :param headers_raw: message raw headers
        :param content_type: content type
        :param content_encoding: content encoding
        :param delivery_mode: delivery mode
        :param priority: priority
        :param correlation_id: correlation id
        :param reply_to: reply to
        :param expiration: expiration in seconds (or datetime or timedelta)
        :param message_id: message id
        :param timestamp: timestamp
        :param type: type
        :param user_id: user id
        :param app_id: app id
        """

        self.__lock = False
        self.body = body if isinstance(body, bytes) else bytes(body)
        self.body_size = len(self.body) if self.body else 0
        self.headers_raw: FieldTable = format_headers(headers)
        self._headers: HeadersType = HeaderProxy(self.headers_raw)
        self.content_type = content_type
        self.content_encoding = content_encoding
        self.delivery_mode: DeliveryMode = DeliveryMode(
            optional(
                delivery_mode, int, DeliveryMode.NOT_PERSISTENT,
            ),
        )
        self.priority = optional(priority, int, 0)
        self.correlation_id = optional(correlation_id, str)
        self.reply_to = optional(reply_to, str)
        self.expiration = expiration
        self.message_id = optional(message_id, str)
        self.timestamp = encode_timestamp(timestamp)
        self.type = optional(type, str)
        self.user_id = optional(user_id, str)
        self.app_id = optional(app_id, str)

    @property
    def headers(self) -> HeadersType:
        return self._headers

    @headers.setter
    def headers(self, value: Dict[str, HeadersPythonValues]) -> None:
        self.headers_raw = format_headers(value)

    @staticmethod
    def _as_bytes(value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        elif isinstance(value, str):
            return value.encode()
        elif value is None:
            return b""
        else:
            return str(value).encode()

    def info(self) -> dict:
        """ Create a dict with message attributes

        ::

            {
                "body_size": 100,
                "headers": {},
                "content_type": "text/plain",
                "content_encoding": "",
                "delivery_mode": DeliveryMode.NOT_PERSISTENT,
                "priority": 0,
                "correlation_id": "",
                "reply_to": "",
                "expiration": "",
                "message_id": "",
                "timestamp": "",
                "type": "",
                "user_id": "",
                "app_id": "",
            }

        """

        return {
            "body_size": self.body_size,
            "headers": self.headers_raw,
            "content_type": self.content_type,
            "content_encoding": self.content_encoding,
            "delivery_mode": self.delivery_mode,
            "priority": self.priority,
            "correlation_id": self.correlation_id,
            "reply_to": self.reply_to,
            "expiration": self.expiration,
            "message_id": self.message_id,
            "timestamp": decode_timestamp(self.timestamp),
            "type": str(self.type),
            "user_id": self.user_id,
            "app_id": self.app_id,
        }

    @property
    def locked(self) -> bool:
        """ is message locked

        :return: :class:`bool`
        """
        return bool(self.__lock)

    @property
    def properties(self) -> aiormq.spec.Basic.Properties:
        """ Build :class:`aiormq.spec.Basic.Properties` object """
        return aiormq.spec.Basic.Properties(
            content_type=self.content_type,
            content_encoding=self.content_encoding,
            headers=self.headers_raw,
            delivery_mode=self.delivery_mode,
            priority=self.priority,
            correlation_id=self.correlation_id,
            reply_to=self.reply_to,
            expiration=encode_expiration(self.expiration),
            message_id=self.message_id,
            timestamp=self.timestamp,
            message_type=self.type,
            user_id=self.user_id,
            app_id=self.app_id,
        )

    def __repr__(self) -> str:
        return "{name}:{repr}".format(
            name=self.__class__.__name__, repr=pformat(self.info()),
        )

    def __setattr__(self, key: str, value: FieldValue) -> None:
        if not key.startswith("_") and self.locked:
            raise ValueError("Message is locked")

        return super().__setattr__(key, value)

    def __iter__(self) -> Iterator[int]:
        return iter(self.body)

    def lock(self) -> None:
        """ Set lock flag to `True`"""
        self.__lock = True

    def __copy__(self) -> "Message":
        return Message(
            body=self.body,
            headers=self.headers_raw,
            content_encoding=self.content_encoding,
            content_type=self.content_type,
            delivery_mode=self.delivery_mode,
            priority=self.priority,
            correlation_id=self.correlation_id,
            reply_to=self.reply_to,
            expiration=self.expiration,
            message_id=self.message_id,
            timestamp=self.timestamp,
            type=self.type,
            user_id=self.user_id,
            app_id=self.app_id,
        )


class IncomingMessage(Message, AbstractIncomingMessage):
    """ Incoming message is seems like Message but has additional methods for
    message acknowledgement.

    Depending on the acknowledgement mode used, RabbitMQ can consider a
    message to be successfully delivered either immediately after it is sent
    out (written to a TCP socket) or when an explicit ("manual") client
    acknowledgement is received. Manually sent acknowledgements can be
    positive or negative and use one of the following protocol methods:

    * basic.ack is used for positive acknowledgements
    * basic.nack is used for negative acknowledgements (note: this is a RabbitMQ
      extension to AMQP 0-9-1)
    * basic.reject is used for negative acknowledgements but has one limitations
      compared to basic.nack

    Positive acknowledgements simply instruct RabbitMQ to record a message as
    delivered. Negative acknowledgements with basic.reject have the same effect.
    The difference is primarily in the semantics: positive acknowledgements
    assume a message was successfully processed while their negative
    counterpart suggests that a delivery wasn't processed but still should
    be deleted.

    """

    __slots__ = (
        "_loop",
        "__channel",
        "cluster_id",
        "consumer_tag",
        "delivery_tag",
        "exchange",
        "routing_key",
        "redelivered",
        "__no_ack",
        "__processed",
        "message_count",
    )

    def __init__(self, message: DeliveredMessage, no_ack: bool = False):
        """ Create an instance of :class:`IncomingMessage` """

        self.__channel = message.channel
        self.__no_ack = no_ack
        self.__processed = False

        expiration = None
        if message.header.properties.expiration:
            expiration = decode_expiration(
                message.header.properties.expiration,
            )

        super().__init__(
            body=message.body,
            content_type=message.header.properties.content_type,
            content_encoding=message.header.properties.content_encoding,
            headers=message.header.properties.headers,
            delivery_mode=message.header.properties.delivery_mode,
            priority=message.header.properties.priority,
            correlation_id=message.header.properties.correlation_id,
            reply_to=message.header.properties.reply_to,
            expiration=expiration / 1000.0 if expiration else None,
            message_id=message.header.properties.message_id,
            timestamp=decode_timestamp(message.header.properties.timestamp),
            type=message.header.properties.message_type,
            user_id=message.header.properties.user_id,
            app_id=message.header.properties.app_id,
        )

        self.cluster_id = message.header.properties.cluster_id
        self.consumer_tag = message.consumer_tag
        self.delivery_tag = message.delivery_tag
        self.exchange = message.exchange
        self.message_count = message.message_count
        self.redelivered = message.redelivered
        self.routing_key = message.routing_key

        if no_ack or not self.delivery_tag:
            self.lock()
            self.__processed = True

    @property
    def channel(self) -> aiormq.abc.AbstractChannel:
        return self.__channel

    def process(
        self,
        requeue: bool = False,
        reject_on_redelivered: bool = False,
        ignore_processed: bool = False,
    ) -> AbstractProcessContext:
        """ Context manager for processing the message

            >>> async def on_message_received(message: IncomingMessage):
            ...    async with message.process():
            ...        # When exception will be raised
            ...        # the message will be rejected
            ...        print(message.body)

        Example with ignore_processed=True

            >>> async def on_message_received(message: IncomingMessage):
            ...    async with message.process(ignore_processed=True):
            ...        # Now (with ignore_processed=True) you may reject
            ...        # (or ack) message manually too
            ...        if True:  # some reasonable condition here
            ...            await message.reject()
            ...        print(message.body)

        :param requeue: Requeue message when exception.
        :param reject_on_redelivered:
            When True message will be rejected only when
            message was redelivered.
        :param ignore_processed: Do nothing if message already processed

        """
        return ProcessContext(
            self,
            requeue=requeue,
            reject_on_redelivered=reject_on_redelivered,
            ignore_processed=ignore_processed,
        )

    async def ack(self, multiple: bool = False) -> None:
        """ Send basic.ack is used for positive acknowledgements

        .. note::
            This method looks like a blocking-method, but actually it just
            sends bytes to the socket and doesn't require any responses from
            the broker.

        :param multiple: If set to True, the message's delivery tag is
                         treated as "up to and including", so that multiple
                         messages can be acknowledged with a single method.
                         If set to False, the ack refers to a single message.
        :return: None
        """
        if self.__no_ack:
            raise TypeError('Can\'t ack message with "no_ack" flag')

        if self.__processed:
            raise MessageProcessError("Message already processed", self)

        if self.delivery_tag is not None:
            await self.__channel.basic_ack(
                delivery_tag=self.delivery_tag, multiple=multiple,
            )

        self.__processed = True

        if not self.locked:
            self.lock()

    async def reject(self, requeue: bool = False) -> None:
        """ When `requeue=True` the message will be returned to queue.
        Otherwise message will be dropped.

        .. note::
            This method looks like a blocking-method, but actually it just
            sends bytes to the socket and doesn't require any responses from
            the broker.

        :param requeue: bool
        """
        if self.__no_ack:
            raise TypeError('This message has "no_ack" flag.')

        if self.__processed:
            raise MessageProcessError("Message already processed", self)

        if self.delivery_tag is not None:
            await self.__channel.basic_reject(
                delivery_tag=self.delivery_tag,
                requeue=requeue,
            )

        self.__processed = True
        if not self.locked:
            self.lock()

    async def nack(
        self, multiple: bool = False, requeue: bool = True,
    ) -> None:

        if not self.channel.connection.basic_nack:
            raise RuntimeError("Method not supported on server")

        if self.__no_ack:
            raise TypeError('Can\'t nack message with "no_ack" flag')

        if self.__processed:
            raise MessageProcessError("Message already processed", self)

        if self.delivery_tag is not None:
            await self.__channel.basic_nack(
                delivery_tag=self.delivery_tag,
                multiple=multiple,
                requeue=requeue,
            )

        self.__processed = True

        if not self.locked:
            self.lock()

    def info(self) -> dict:
        """ Method returns dict representation of the message """

        info = super(IncomingMessage, self).info()
        info["cluster_id"] = self.cluster_id
        info["consumer_tag"] = self.consumer_tag
        info["delivery_tag"] = self.delivery_tag
        info["exchange"] = self.exchange
        info["redelivered"] = self.redelivered
        info["routing_key"] = self.routing_key
        return info

    @property
    def processed(self) -> bool:
        return self.__processed


class ReturnedMessage(IncomingMessage):
    pass


ReturnCallback = Callable[[AbstractChannel, ReturnedMessage], Any]


class ProcessContext(AbstractProcessContext):
    def __init__(
        self,
        message: IncomingMessage,
        *,
        requeue: bool,
        reject_on_redelivered: bool,
        ignore_processed: bool,
    ):
        self.message = message
        self.requeue = requeue
        self.reject_on_redelivered = reject_on_redelivered
        self.ignore_processed = ignore_processed

    async def __aenter__(self) -> IncomingMessage:
        return self.message

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        if not exc_type:
            if not self.ignore_processed or not self.message.processed:
                await self.message.ack()

            return

        if not self.ignore_processed or not self.message.processed:
            if self.reject_on_redelivered and self.message.redelivered:
                if not self.message.channel.is_closed:
                    log.info(
                        "Message %r was redelivered and will be rejected",
                        self.message,
                    )
                    await self.message.reject(requeue=False)
                    return
                log.warning(
                    "Message %r was redelivered and reject is not sent "
                    "since channel is closed",
                    self.message,
                )
            else:
                if not self.message.channel.is_closed:
                    await self.message.reject(requeue=self.requeue)
                    return
                log.warning("Reject is not sent since channel is closed")


__all__ = "Message", "IncomingMessage", "ReturnedMessage",
