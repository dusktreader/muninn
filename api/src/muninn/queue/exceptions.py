"""Project-owned exceptions for queue-backed Object Store boundaries."""

from nats.errors import (
    ConnectionClosedError,
    ConnectionDrainingError,
    ConnectionReconnectingError,
    NoRespondersError,
    NoServersError,
    StaleConnectionError,
    TimeoutError as NatsTimeoutError,
)

from muninn.exceptions import MuninnError
from nats.js.errors import (
    BadObjectMetaError,
    BadRequestError,
    DigestMismatchError,
    InvalidObjectNameError,
    LinkIsABucketError,
    NoStreamResponseError,
    NotFoundError,
    ObjectAlreadyExists,
    ObjectDeletedError,
    ObjectNotFoundError,
    ServerError,
    ServiceUnavailableError,
)


class QueueError(MuninnError):
    """Represent a failure at the queue boundary."""


class ObjectStoreError(QueueError):
    """Represent a failure while reading or writing a project Object Store object."""


class ObjectStoreUnavailableError(ObjectStoreError):
    """Represent a retryable Object Store transport or backend failure."""


class ObjectStoreNotFoundError(ObjectStoreError):
    """Represent an object that is missing or has been deleted."""


class ObjectStoreIntegrityError(ObjectStoreError):
    """Represent an object whose metadata or content cannot be trusted."""


class ObjectStoreOperationError(ObjectStoreError):
    """Represent an expected rejected or invalid Object Store operation."""


ObjectStoreUnavailableErrors = (
    ConnectionClosedError,
    ConnectionDrainingError,
    ConnectionReconnectingError,
    NatsTimeoutError,
    NoRespondersError,
    NoServersError,
    NoStreamResponseError,
    ServerError,
    ServiceUnavailableError,
    StaleConnectionError,
)
ObjectStoreNotFoundErrors = (NotFoundError, ObjectDeletedError, ObjectNotFoundError)
ObjectStoreIntegrityErrors = (BadObjectMetaError, DigestMismatchError, LinkIsABucketError)
ObjectStoreOperationErrors = (BadRequestError, InvalidObjectNameError, ObjectAlreadyExists)
