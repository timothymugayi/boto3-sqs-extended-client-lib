import base64
import binascii
import json
import logging
import os
import pathlib
import threading
import time
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from io import BytesIO

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import (
	ClientError,
	MissingParametersError,
	NoCredentialsError,
	ParamValidationError,
	UnknownCredentialError,
	ValidationError,
)
from botocore.hooks import BaseEventHooks

from pysqs_extended_client.extended_config import ExtendedClientConfiguration


logger = logging.getLogger(__name__)


def _blank_to_none(value):
	"""Treat None and whitespace-only strings as omitted."""
	if value is None:
		return None
	if not isinstance(value, str):
		raise TypeError(
			"Credential and region values must be str or None, got {}.".format(type(value).__name__)
		)
	stripped = value.strip()
	return stripped or None


def _validate_credential_pair(aws_access_key_id, aws_secret_access_key):
	"""Require both explicit keys, or neither.

	A region on its own stays valid: boto3 then uses the default credential chain.
	Blank strings count as omitted so padding cannot look like a half-specified pair.
	"""
	access_key = _blank_to_none(aws_access_key_id)
	secret_key = _blank_to_none(aws_secret_access_key)
	if (access_key is None) != (secret_key is None):
		present = "aws_access_key_id" if access_key is not None else "aws_secret_access_key"
		missing = "aws_secret_access_key" if access_key is not None else "aws_access_key_id"
		raise ValueError(
			"{} is set but {} is missing. Set both aws_access_key_id and "
			"aws_secret_access_key, or omit both to use the default credential chain. "
			"A half-specified pair is not passed to boto3 and is not ignored. "
			"aws_region_name may be set on its own.".format(present, missing)
		)
	return access_key, secret_key


class SQSExtendedClientConstants(Enum):
	DEFAULT_MESSAGE_SIZE_THRESHOLD = 262144
	MAX_ALLOWED_ATTRIBUTES = 10 - 1  # 10 for SQS, 1 for the reserved attribute
	RESERVED_ATTRIBUTE_NAME = "SQSLargePayloadSize"
	RESERVED_ATTRIBUTE_NAME_EXTENDED = "ExtendedPayloadSize"
	CONTENT_TYPE_ATTRIBUTE = "SQSExtendedContentType"  # "text" | "binary" | "binary-b64"
	PAYLOAD_S3_POINTER_CLASS = "software.amazon.payloadoffloading.PayloadS3Pointer"
	LEGACY_PAYLOAD_S3_POINTER_CLASS = "com.amazon.sqs.javamessaging.MessageS3Pointer"
	S3_BUCKET_NAME_MARKER = "-..s3BucketName..-"
	S3_KEY_MARKER = "-..s3Key..-"


# SQS counts the inline binary content-type attribute this library adds:
# name "SQSExtendedContentType" (22) + DataType "String" (6) + value "binary-b64" (10).
INLINE_BINARY_CONTENT_TYPE_ATTRIBUTE_BYTES = (
	len(SQSExtendedClientConstants.CONTENT_TYPE_ATTRIBUTE.value) + len("String") + len("binary-b64")
)
EVENT_MESSAGE_LEFT = "message_left"
EVENT_S3_OBJECT_ORPHANED = "s3_object_orphaned"
MESSAGE_LEFT_S3_NOT_FOUND = "s3_not_found"
MESSAGE_LEFT_BROKEN_POINTER = "broken_pointer"
ORPHAN_AFTER_SQS_SUCCEEDED = "after_sqs_succeeded"
ORPHAN_AFTER_UPLOAD_SQS_REJECTED = "after_upload_sqs_rejected"
ORPHAN_AFTER_SQS_UNCERTAIN = "after_sqs_uncertain"
_CONFIRMED_S3_NOT_FOUND_CODES = frozenset(("NoSuchKey", "404", "NotFound"))
_SQS_SEND_ATTEMPT_OPERATIONS = ("SendMessage", "SendMessageBatch")
_SQS_SEND_ATTEMPT_HOOK_PREFIX = "pysqs-extended-client-before-send"
# One tracker per botocore SQS client. The before-send handler holds the tracker,
# not an extended client, so wrappers created per request can be collected.
_SEND_ATTEMPT_TRACKERS = weakref.WeakKeyDictionary()
_SEND_ATTEMPT_TRACKERS_LOCK = threading.Lock()


class _SendAttemptTracker:
	"""Thread-local before-send count shared by every wrapper of one SQS client."""

	def __init__(self):
		self._local = threading.local()
		self.lock = threading.Lock()
		self.users = 0
		self.registered = False

	def reset(self):
		self._local.count = 0

	def note(self, **kwargs):
		self._local.count = getattr(self._local, "count", 0) + 1

	def was_attempted(self):
		return getattr(self._local, "count", 0) > 0

	def count(self):
		return getattr(self._local, "count", 0)


def _live_sqs_events(sqs):
	"""Return botocore event hooks only when before-send will actually run.

	A ``MagicMock`` has a callable ``register`` that never emits. Treating that
	as a live hook leaves the attempt count at 0, so a timeout looks like a
	definite rejection and the uploaded object is deleted.
	"""
	events = getattr(getattr(sqs, "meta", None), "events", None)
	if isinstance(sqs, BaseClient) and isinstance(events, BaseEventHooks):
		return events
	return None


def _tracker_for(sqs):
	with _SEND_ATTEMPT_TRACKERS_LOCK:
		tracker = _SEND_ATTEMPT_TRACKERS.get(sqs)
		if tracker is None:
			tracker = _SendAttemptTracker()
			_SEND_ATTEMPT_TRACKERS[sqs] = tracker
		return tracker


def _send_attempt_hook_id(operation):
	return "%s-%s" % (_SQS_SEND_ATTEMPT_HOOK_PREFIX, operation)


class SQSClientExtended:
	"""
	SQS client that offloads large payloads to S3, modelled on the Java extended client.

	Share one instance per process across worker threads. boto3 clients are created once
	with a sized connection pool. Batch S3 work is fanned out on a bounded executor.

	The four-argument constructor stays compatible with existing callers.
	Configure scale knobs on ExtendedClientConfiguration before constructing the client;
	the config is frozen after __init__.

	When ``payload_support_enabled`` is true (the default), a non-blank bucket name
	is required here. Offload can happen for every message (``always_through_s3``)
	or for any message over ``message_size_threshold``. Small-only callers that
	must not offload or hydrate set ``payload_support_enabled=False`` and may omit
	the bucket. Access key and secret must both be set or both omitted.
	"""

	def __init__(
		self,
		aws_access_key_id=None,
		aws_secret_access_key=None,
		aws_region_name=None,
		s3_bucket_name=None,
		config=None,
		sqs_client=None,
		s3_client=None,
	):
		if config is None:
			self.config = ExtendedClientConfiguration(s3_bucket_name=s3_bucket_name)
		else:
			self.config = config
			self._apply_bucket_argument(s3_bucket_name)
		# Keys are passed only into boto3 and are not retained on this instance.
		# Omit them to use the default credential chain (env, shared file, role).
		# Checked even when callers inject clients, so a half-specified pair is never ignored.
		access_key, secret_key = _validate_credential_pair(aws_access_key_id, aws_secret_access_key)
		self._require_bucket_if_offload_can_happen()
		self.sqs = sqs_client if sqs_client is not None else self._build_client(
			"sqs", access_key, secret_key, aws_region_name
		)
		self.s3 = s3_client if s3_client is not None else self._build_client(
			"s3", access_key, secret_key, aws_region_name
		)
		self._pool = None
		self._pool_lock = threading.Lock()
		# Shared with every extended client on this botocore SQS client.
		# boto3 retries on the calling thread; the count itself is thread-local.
		self._send_attempt_tracker = None
		self._sqs_attempt_hook_registered = False
		self._hook_released = True
		self._register_sqs_send_attempt_hook()
		self._reject_delete_s3_before_sqs()
		self.config.freeze()

	def _apply_bucket_argument(self, s3_bucket_name):
		"""Use the constructor bucket, unless config already names a different one."""
		if s3_bucket_name is None:
			return
		ctor_bucket = ExtendedClientConfiguration.validate_s3_bucket_name(s3_bucket_name)
		config_bucket = self.config.s3_bucket_name
		if config_bucket and ctor_bucket != config_bucket:
			raise ValueError(
				"s3_bucket_name passed to SQSClientExtended ({!r}) does not match "
				"ExtendedClientConfiguration.s3_bucket_name ({!r}). "
				"Pass the bucket in one place, or use the same name in both. "
				"The constructor value is not ignored.".format(ctor_bucket, config_bucket)
			)
		if not config_bucket:
			self.config.s3_bucket_name = ctor_bucket

	def _require_bucket_if_offload_can_happen(self):
		"""Fail at init when a later send could offload and no bucket is configured.

		``payload_support_enabled=False`` is the small-only mode: nothing is
		offloaded, hydrated, or deleted in S3, so a bucket is not required.
		``always_through_s3`` does not offload in that mode.
		"""
		if not self.config.payload_support_enabled:
			return
		bucket = self.config.s3_bucket_name
		if isinstance(bucket, str) and bucket.strip():
			return
		if self.config.always_through_s3:
			reason = "always_through_s3=True stores every payload in S3"
		else:
			reason = (
				"payload_support_enabled=True stores any message larger than "
				"message_size_threshold (%s bytes) in S3" % self.config.message_size_threshold
			)
		raise ValueError(
			"s3_bucket_name is required when constructing SQSClientExtended because {}. "
			"Pass a non-blank bucket name to SQSClientExtended or ExtendedClientConfiguration. "
			"To use the client for small inline messages only, set payload_support_enabled=False. "
			"That mode does not offload, hydrate, or delete S3 payloads, so a bucket is not required. "
			"always_through_s3 has no effect when payload support is disabled. "
			"Receive-only clients that hydrate pointers should still set s3_bucket_name "
			"(receive reads the bucket from the pointer; the check is so a later send "
			"cannot fail on a missing bucket).".format(reason)
		)

	def _build_client(self, service_name, aws_access_key_id, aws_secret_access_key, aws_region_name):
		kwargs = {"config": self._botocore_config()}
		if self.config.endpoint_url:
			kwargs["endpoint_url"] = self.config.endpoint_url
		access_key = _blank_to_none(aws_access_key_id)
		secret_key = _blank_to_none(aws_secret_access_key)
		region = _blank_to_none(aws_region_name)
		# Both keys are passed whenever they are set, including when region is omitted.
		# Previously they were dropped unless region was set too.
		if access_key is not None:
			kwargs["aws_access_key_id"] = access_key
			kwargs["aws_secret_access_key"] = secret_key
		if region is not None:
			kwargs["region_name"] = region
		return boto3.client(service_name, **kwargs)

	def _botocore_config(self):
		retries = {"max_attempts": self.config.retry_max_attempts}
		kwargs = {
			"max_pool_connections": self.config.max_pool_connections,
			"connect_timeout": self.config.connect_timeout,
			"read_timeout": self.config.read_timeout,
		}
		if self.config.endpoint_url:
			kwargs["s3"] = {"addressing_style": "path"}
		try:
			return Config(retries=dict(retries, mode="standard"), **kwargs)
		except TypeError:
			return Config(retries=retries, **kwargs)

	def _transfer_config(self):
		return TransferConfig(
			multipart_threshold=self.config.multipart_threshold,
			max_concurrency=self.config.s3_max_concurrency,
		)

	def _executor(self):
		if self._pool is None:
			with self._pool_lock:
				if self._pool is None:
					self._pool = ThreadPoolExecutor(max_workers=self.config.s3_max_concurrency)
		return self._pool

	def _emit(self, name, **fields):
		logger.debug("%s %s", name, fields)
		callback = self.config.on_event
		if callback is None:
			return
		try:
			callback(name, **fields)
		except Exception:
			logger.exception(
				"on_event callback raised for %s; the error was logged and ignored",
				name,
			)

	def close(self):
		self._release_sqs_send_attempt_hook()
		with self._pool_lock:
			pool = self._pool
			self._pool = None
		if pool is not None:
			pool.shutdown(wait=True)

	@property
	def s3_bucket_name(self):
		return self.config.s3_bucket_name

	@property
	def always_through_s3(self):
		return self.config.always_through_s3

	@property
	def message_size_threshold(self):
		return self.config.message_size_threshold

	def is_large_payload_support_enabled(self):
		"""Java ``isPayloadSupportEnabled`` / deprecated ``isLargePayloadSupportEnabled``.

		True unless ``ExtendedClientConfiguration(payload_support_enabled=False)``
		was set before this client was created. There is no setter after init.
		"""
		return bool(self.config.payload_support_enabled)

	def __getattr__(self, name):
		return getattr(self.sqs, name)

	def _get_string_size_in_bytes(self, value):
		if isinstance(value, (bytes, bytearray)):
			return len(value)
		return len(value.encode("utf-8"))

	def _binary_attribute_size(self, value):
		# boto3 BinaryValue is bytes. SQS counts the raw byte length, not a base64 wire size.
		if isinstance(value, (bytes, bytearray, memoryview)):
			return len(value)
		if isinstance(value, str):
			return len(value.encode("utf-8"))
		raise TypeError("BinaryValue must be bytes or str, got %s" % type(value).__name__)

	def _get_msg_attributes_size(self, message_attributes):
		total_msg_attributes_size = 0
		for key, entry in message_attributes.items():
			total_msg_attributes_size += self._get_string_size_in_bytes(key)
			if entry.get("DataType"):
				total_msg_attributes_size += self._get_string_size_in_bytes(entry.get("DataType"))
			if entry.get("StringValue"):
				total_msg_attributes_size += self._get_string_size_in_bytes(entry.get("StringValue"))
			binary_value = entry.get("BinaryValue")
			if binary_value is not None and binary_value != "":
				total_msg_attributes_size += self._binary_attribute_size(binary_value)
		return total_msg_attributes_size

	def _reserved_attribute_name_if_present(self, message_attributes):
		if not message_attributes:
			return None
		extended = SQSExtendedClientConstants.RESERVED_ATTRIBUTE_NAME_EXTENDED.value
		legacy = SQSExtendedClientConstants.RESERVED_ATTRIBUTE_NAME.value
		if extended in message_attributes:
			return extended
		if legacy in message_attributes:
			return legacy
		return None

	def _payload_size_attribute_name(self):
		if self.config.use_legacy_attribute:
			return SQSExtendedClientConstants.RESERVED_ATTRIBUTE_NAME.value
		return SQSExtendedClientConstants.RESERVED_ATTRIBUTE_NAME_EXTENDED.value

	def _parse_s3_pointer(self, message_body):
		parsed = json.loads(message_body)
		pointer_classes = (
			SQSExtendedClientConstants.PAYLOAD_S3_POINTER_CLASS.value,
			SQSExtendedClientConstants.LEGACY_PAYLOAD_S3_POINTER_CLASS.value,
		)
		if isinstance(parsed, list) and len(parsed) > 1 and parsed[0] in pointer_classes:
			parsed = parsed[1]
		return parsed

	def _to_payload_s3_pointer_json(self, s3_pointer):
		return json.dumps([
			SQSExtendedClientConstants.PAYLOAD_S3_POINTER_CLASS.value,
			s3_pointer,
		])

	def _validate_message_attributes(self, message_attributes):
		msg_attributes_size = self._get_msg_attributes_size(message_attributes)
		if msg_attributes_size > self.config.message_size_threshold:
			raise ValueError(
				"Total size of Message attributes is {} bytes which is larger than the threshold of {} Bytes. "
				"Consider including the payload in the message body instead of message attributes.".format(
					msg_attributes_size, self.config.message_size_threshold
				)
			)
		message_attributes_number = len(message_attributes)
		if message_attributes_number > SQSExtendedClientConstants.MAX_ALLOWED_ATTRIBUTES.value:
			raise ValueError(
				"Number of message attributes [{}] exceeds the maximum allowed for large-payload messages [{}].".format(
					message_attributes_number, SQSExtendedClientConstants.MAX_ALLOWED_ATTRIBUTES.value
				)
			)
		reserved_attribute_name = self._reserved_attribute_name_if_present(message_attributes)
		if reserved_attribute_name:
			raise ValueError(
				"Message attribute name {} is reserved for use by SQS extended client.".format(reserved_attribute_name)
			)
		content_type_name = SQSExtendedClientConstants.CONTENT_TYPE_ATTRIBUTE.value
		if content_type_name in message_attributes:
			raise ValueError(
				"Message attribute name {} is reserved for use by SQS extended client.".format(content_type_name)
			)

	def _normalize_payload(self, message):
		"""
		Returns (payload, size_bytes, is_binary, is_streamable).
		payload is bytes, a filesystem path (str), or an open binary file-like.

		A plain str is always text, even when that string is an existing filesystem
		path. Pass pathlib.Path / os.PathLike or call send_file() to upload a file.
		"""
		if message is None:
			raise ValueError("message cannot be None")

		if isinstance(message, (bytes, bytearray)):
			data = bytes(message)
			return data, len(data), True, False

		if isinstance(message, os.PathLike):
			path = os.fspath(message)
			if not os.path.isfile(path):
				raise ValueError("file path does not exist or is not a file: {}".format(path))
			return path, os.path.getsize(path), True, True

		if hasattr(message, "read"):
			if hasattr(message, "seek") and hasattr(message, "tell"):
				pos = message.tell()
				message.seek(0, os.SEEK_END)
				size = message.tell()
				message.seek(pos)
			else:
				size = self.config.message_size_threshold + 1
			return message, size, True, True

		if isinstance(message, str):
			data = message.encode("utf-8")
			return data, len(data), False, False

		raise TypeError(
			"message must be str, bytes, pathlib.Path, or a binary file-like object, got %s. "
			"To send a file, pass a pathlib.Path or call send_file()." % type(message).__name__
		)

	def _base64_wire_size(self, size_bytes):
		return 4 * ((size_bytes + 2) // 3)

	def _should_offload(self, size_bytes, message_attributes, is_binary=False):
		if self.config.always_through_s3:
			return True
		attr_size = self._get_msg_attributes_size(message_attributes)
		if is_binary:
			# Inline binary is the base64 body plus the content-type attribute
			# this library adds. Over the threshold, store the raw bytes instead.
			wire = (
				self._base64_wire_size(size_bytes)
				+ attr_size
				+ INLINE_BINARY_CONTENT_TYPE_ATTRIBUTE_BYTES
			)
			return wire > self.config.message_size_threshold
		return (attr_size + size_bytes) > self.config.message_size_threshold

	def _ensure_binary_offload_attribute_room(self, message_attributes, is_binary):
		if not is_binary:
			return
		# Offload already reserves one attribute for payload size. Binary adds content type.
		limit = SQSExtendedClientConstants.MAX_ALLOWED_ATTRIBUTES.value - 1
		if len(message_attributes) > limit:
			raise ValueError(
				"Number of message attributes [{}] exceeds the maximum allowed for binary large-payload messages [{}].".format(
					len(message_attributes), limit
				)
			)

	def _attach_offload_attributes(self, message_attributes, size_bytes, is_binary):
		message_attributes[self._payload_size_attribute_name()] = {
			"StringValue": str(size_bytes),
			"DataType": "Number",
		}
		if is_binary:
			message_attributes[SQSExtendedClientConstants.CONTENT_TYPE_ATTRIBUTE.value] = {
				"StringValue": "binary",
				"DataType": "String",
			}
		return message_attributes

	def _read_small_payload(self, payload, is_streamable):
		if not is_streamable:
			return payload
		if isinstance(payload, str):
			with open(payload, "rb") as handle:
				return handle.read()
		return payload.read()

	def _encode_small_body(self, data, is_binary, message_attributes):
		if is_binary:
			body = base64.b64encode(data).decode("ascii")
			message_attributes[SQSExtendedClientConstants.CONTENT_TYPE_ATTRIBUTE.value] = {
				"StringValue": "binary-b64",
				"DataType": "String",
			}
			return body, message_attributes
		return data.decode("utf-8"), message_attributes

	def _require_s3_bucket(self):
		if not self.config.s3_bucket_name or not str(self.config.s3_bucket_name).strip():
			raise ValueError("S3 bucket name cannot be null")

	def _maybe_offload(self, message, message_attributes):
		if not self.config.payload_support_enabled:
			if not isinstance(message, str):
				raise TypeError(
					"payload support is disabled; message must be str, got %s" % type(message).__name__
				)
			return message, message_attributes
		self._validate_message_attributes(message_attributes)
		payload, size_bytes, is_binary, is_streamable = self._normalize_payload(message)
		if self._should_offload(size_bytes, message_attributes, is_binary):
			self._ensure_binary_offload_attribute_room(message_attributes, is_binary)
			self._require_s3_bucket()
			pointer = self._store_message_in_s3(payload, size_bytes, is_streamable)
			self._attach_offload_attributes(message_attributes, size_bytes, is_binary)
			return self._to_payload_s3_pointer_json(pointer), message_attributes
		data = self._read_small_payload(payload, is_streamable)
		return self._encode_small_body(data, is_binary, message_attributes)

	def _payload_would_offload(self, message, message_attributes):
		"""Decide offload without uploading. Does not invent a deduplication id."""
		if not self.config.payload_support_enabled:
			return False
		self._validate_message_attributes(message_attributes)
		_payload, size_bytes, is_binary, _is_streamable = self._normalize_payload(message)
		return self._should_offload(size_bytes, message_attributes, is_binary)

	def _queue_name(self, queue_url):
		if not isinstance(queue_url, str):
			return ""
		path = queue_url.split("?", 1)[0].rstrip("/")
		if not path:
			return ""
		return path.rsplit("/", 1)[-1]

	def _is_fifo_queue(self, queue_url):
		return self._queue_name(queue_url).endswith(".fifo")

	def _blank_id(self, value):
		if value is None:
			return None
		if isinstance(value, str) and not value.strip():
			return None
		return value

	def _entry_deduplication_id(self, entry):
		# Batch entries carry the boto3 field. A blank value counts as omitted.
		return self._blank_id(entry.get("MessageDeduplicationId"))

	def _fifo_offload_without_dedup_error(self, queue_url):
		return ValueError(
			"Refusing to offload to FIFO queue {} without message_deduplication_id. "
			"An offloaded body contains a new S3 key, so content-based deduplication "
			"will not collapse a retry. Pass message_deduplication_id. "
			"This library does not invent one, and nothing was uploaded.".format(
				self._queue_name(queue_url)
			)
		)

	def _fifo_redrive_error(self, queue_url, problem):
		return ValueError(
			"Refusing to use FIFO queue {} because its RedrivePolicy {}. "
			"A message that cannot be read blocks its message group until it is dead-lettered. "
			"Create a redrive policy with deadLetterTargetArn and a maxReceiveCount of at least 1 on this queue "
			"before sending or receiving. This library checks only this queue's policy, "
			"creates neither the queue nor the policy, and does not emit an event.".format(
				self._queue_name(queue_url), problem
			)
		)

	def _require_fifo_redrive_policy(self, queue_url):
		"""Refuse a FIFO queue whose redrive policy is missing or incomplete.

		The GetQueueAttributes result is not cached. A policy removed after an
		earlier call is refused on the next send, batch send, or receive.
		Standard queues are not checked. No event is emitted.
		"""
		if not self._is_fifo_queue(queue_url):
			return
		response = self.sqs.get_queue_attributes(
			QueueUrl=queue_url,
			AttributeNames=["RedrivePolicy"],
		)
		self._raise_unless_redrive_policy(queue_url, response)

	def _raise_unless_redrive_policy(self, queue_url, response):
		attrs = {}
		if isinstance(response, dict):
			raw_attrs = response.get("Attributes")
			if isinstance(raw_attrs, dict):
				attrs = raw_attrs
		if "RedrivePolicy" not in attrs or attrs.get("RedrivePolicy") is None:
			raise self._fifo_redrive_error(queue_url, "is missing")
		raw = attrs.get("RedrivePolicy")
		if isinstance(raw, str):
			if not raw.strip():
				raise self._fifo_redrive_error(queue_url, "is missing")
			try:
				policy = json.loads(raw)
			except ValueError:
				raise self._fifo_redrive_error(queue_url, "is not valid JSON")
		else:
			policy = raw
		if not isinstance(policy, dict):
			raise self._fifo_redrive_error(queue_url, "lacks deadLetterTargetArn")
		if not policy:
			raise self._fifo_redrive_error(queue_url, "is empty")
		arn = policy.get("deadLetterTargetArn")
		if not isinstance(arn, str) or not arn.strip():
			raise self._fifo_redrive_error(queue_url, "lacks deadLetterTargetArn")
		if "maxReceiveCount" not in policy:
			raise self._fifo_redrive_error(queue_url, "lacks maxReceiveCount")
		count, problem = self._max_receive_count(policy.get("maxReceiveCount"))
		if problem:
			raise self._fifo_redrive_error(queue_url, problem)
		if count is None:
			raise self._fifo_redrive_error(queue_url, "lacks maxReceiveCount")

	def _usable_pointer_target(self, message_body):
		"""Return (bucket, key) when both are non-empty strings, else None."""
		if not isinstance(message_body, dict):
			return None
		bucket = message_body.get("s3BucketName")
		key = message_body.get("s3Key")
		if not isinstance(bucket, str) or not bucket.strip():
			return None
		if not isinstance(key, str) or not key.strip():
			return None
		return bucket, key

	def _leave_unreadable_message(self, queue_url, message, s3_bucket_name, s3_key, cause):
		"""Leave the SQS message in place. Do not delete it.

		``ignore_payload_not_found`` does not turn deletion back on. The event
		reason is ``message_left``: the message was left, not deleted.
		"""
		logger.warning(
			"Left the SQS message in place because the payload could not be read (%s). "
			"The message was not deleted.",
			cause,
		)
		self._emit(
			EVENT_MESSAGE_LEFT,
			queue_url=queue_url,
			s3_bucket_name=s3_bucket_name,
			s3_key=s3_key,
			receipt_handle=message.get("ReceiptHandle"),
			cause=cause,
		)

	def _discard_payload_file(self, filename):
		"""Remove a payload file for a message that will not be returned."""
		if not filename:
			return
		try:
			os.remove(filename)
		except FileNotFoundError:
			return
		except OSError:
			logger.warning(
				"Failed to remove payload file %s for a message that will not be returned.",
				filename,
				exc_info=True,
			)

	def _max_receive_count(self, count):
		"""Return ``(value, problem)``.

		``value`` is an int when the policy has an integer count. ``problem``
		is a phrase for ``_fifo_redrive_error`` when the value is present but
		not an integer count (a float such as ``3.0``, or an integer below 1).
		Both are None when the count is missing or not a number. Booleans are
		rejected. Non-ASCII digits such as ``²`` are not counts: ``str.isdigit``
		is true for them, and ``int`` would raise.
		"""
		if isinstance(count, bool) or count is None:
			return None, None
		if isinstance(count, int):
			if count < 1:
				return count, "requires maxReceiveCount >= 1"
			return count, None
		if isinstance(count, float):
			return None, self._non_integer_count_problem(count)
		if isinstance(count, str):
			text = count.strip()
			if not text:
				return None, None
			if text.isascii() and text.isdigit():
				value = int(text)
				if value < 1:
					return value, "requires maxReceiveCount >= 1"
				return value, None
			if text.isascii():
				try:
					float(text)
				except ValueError:
					return None, None
				return None, self._non_integer_count_problem(text)
		return None, None

	def _non_integer_count_problem(self, count):
		return "requires an integer maxReceiveCount of at least 1, not {!r}".format(count)

	def _reject_delete_s3_before_sqs(self):
		if not self.config.delete_s3_before_sqs:
			return
		raise ValueError(
			"delete_s3_before_sqs=True is not supported. "
			"Deleting the S3 object before the SQS message can leave a dead pointer "
			"if the SQS delete fails. Neither delete was performed. "
			"Leave delete_s3_before_sqs unset so the SQS message is deleted first."
		)

	def receive_message(self, queue_url, max_number_of_messages=1, wait_time_seconds=10, payload_dir=None, **kwargs):
		"""
		Retrieves one or more messages (up to 10) and hydrates S3 payloads in parallel.

		Returns a list of messages. An empty queue returns ``[]``.

		By default each S3 payload is loaded fully into memory (``message["Body"]`` is
		``str`` or ``bytes``). Pass ``payload_dir`` to stream S3 objects to files with
		``download_file`` instead. Those messages set ``Body`` and ``_payload_path`` to
		the file path. Inline messages are unchanged. Use ``payload_dir`` for
		multi-hundred-MB bodies; the in-memory path is the Java-compatible default.

		A FIFO queue is refused, before this calls SQS, when its redrive policy
		is missing or incomplete. The whole batch is classified before any
		message is returned or any error is raised. A pointer that is not valid
		JSON, or that is missing ``s3BucketName`` or ``s3Key``, and a confirmed
		S3 not-found, each leave that SQS message in place and emit
		``message_left``. A timeout, a 5xx, or any other read error emits
		nothing for itself. Every ``message_left`` in the batch is emitted, and
		payload files for messages that will not be returned are removed,
		before the first of those other errors is raised.
		"""
		self._require_fifo_redrive_policy(queue_url)
		if "max_number_Of_Messages" in kwargs:
			max_number_of_messages = kwargs.pop("max_number_Of_Messages")
		message_attribute_names = list(kwargs.pop("MessageAttributeNames", ["All"]))
		if "All" not in message_attribute_names:
			for reserved in (
				SQSExtendedClientConstants.RESERVED_ATTRIBUTE_NAME.value,
				SQSExtendedClientConstants.RESERVED_ATTRIBUTE_NAME_EXTENDED.value,
				SQSExtendedClientConstants.CONTENT_TYPE_ATTRIBUTE.value,
			):
				if reserved not in message_attribute_names:
					message_attribute_names.append(reserved)
		params = {
			"QueueUrl": queue_url,
			"MaxNumberOfMessages": max_number_of_messages,
			"WaitTimeSeconds": wait_time_seconds,
			"AttributeNames": kwargs.pop("AttributeNames", ["All"]),
			"MessageAttributeNames": message_attribute_names,
		}
		params.update(kwargs)
		response_opt_queue = self.sqs.receive_message(**params)
		opt_messages = response_opt_queue.get("Messages") or []
		if not opt_messages:
			return []
		if not self.config.payload_support_enabled:
			return list(opt_messages)

		records = []
		hydrate_jobs = []
		errors = {}
		for index, message in enumerate(opt_messages):
			reserved_attribute_name = self._reserved_attribute_name_if_present(message.get("MessageAttributes", {}))
			content_type = self._content_type_of(message)
			if not reserved_attribute_name:
				try:
					self._decode_inline_binary(message, content_type)
				except (binascii.Error, ValueError) as exc:
					# Same deferred path as a hydrate failure: siblings still
					# emit message_left before this error is raised.
					errors[index] = exc
					records.append({"kind": "pending", "index": index, "message": message})
				else:
					records.append({"kind": "inline", "message": message})
				continue
			try:
				message_body = self._parse_s3_pointer(message.get("Body"))
			except (ValueError, TypeError):
				records.append({
					"kind": "broken",
					"message": message,
					"bucket": None,
					"key": None,
				})
				continue
			target = self._usable_pointer_target(message_body)
			if target is None:
				bucket = key = None
				if isinstance(message_body, dict):
					bucket = message_body.get("s3BucketName")
					key = message_body.get("s3Key")
				records.append({
					"kind": "broken",
					"message": message,
					"bucket": bucket,
					"key": key,
				})
				continue
			s3_bucket_name, s3_key = target
			as_binary = content_type in ("binary", "binary-b64")
			filename = self._payload_download_path(payload_dir, s3_key) if payload_dir else None
			records.append({
				"kind": "pending",
				"index": index,
				"message": message,
				"reserved": reserved_attribute_name,
				"bucket": s3_bucket_name,
				"key": s3_key,
				"content_type": content_type,
				"filename": filename,
			})
			hydrate_jobs.append((index, s3_bucket_name, s3_key, as_binary, filename))

		bodies = {}
		if hydrate_jobs:
			future_to_index = {
				self._executor().submit(self._hydrate_from_s3, bucket, key, as_binary, filename): index
				for index, bucket, key, as_binary, filename in hydrate_jobs
			}
			for future in as_completed(future_to_index):
				index = future_to_index[future]
				try:
					bodies[index] = future.result()
				except Exception as exc:
					errors[index] = exc

		first_other = None
		for record in records:
			if record["kind"] != "pending":
				continue
			index = record["index"]
			if index in errors:
				exc = errors[index]
				if self._is_confirmed_s3_not_found(exc):
					record["kind"] = "not_found"
					continue
				record["kind"] = "other"
				record["error"] = exc
				if first_other is None:
					first_other = exc
				continue
			orig_msg_body = bodies.get(index)
			if orig_msg_body is None:
				# Not a confirmed S3 not-found. Do not delete and do not emit.
				record["kind"] = "other"
				record["error"] = ValueError(
					"S3 download returned no body for key {}. "
					"This is not a confirmed S3 not-found, and the SQS message was not deleted.".format(
						record["key"]
					)
				)
				if first_other is None:
					first_other = record["error"]
				continue
			record["kind"] = "ok"
			record["body"] = orig_msg_body

		for record in records:
			if record["kind"] == "broken":
				self._leave_unreadable_message(
					queue_url, record["message"], record["bucket"], record["key"], MESSAGE_LEFT_BROKEN_POINTER
				)
			elif record["kind"] == "not_found":
				self._leave_unreadable_message(
					queue_url, record["message"], record["bucket"], record["key"], MESSAGE_LEFT_S3_NOT_FOUND
				)

		if first_other is not None:
			for record in records:
				self._discard_payload_file(record.get("filename"))
			raise first_other

		hydrated = []
		for record in records:
			kind = record["kind"]
			if kind in ("broken", "not_found"):
				self._discard_payload_file(record.get("filename"))
				continue
			if kind == "inline":
				hydrated.append(record["message"])
				continue
			message = record["message"]
			orig_msg_body = record["body"]
			filename = record["filename"]
			content_type = record["content_type"]
			if filename:
				message["Body"] = orig_msg_body
				message["_payload_path"] = orig_msg_body
			else:
				if content_type == "binary-b64":
					orig_msg_body = base64.b64decode(orig_msg_body)
				message["Body"] = orig_msg_body
			if content_type in ("binary", "binary-b64"):
				message["_is_binary"] = True
			message.get("MessageAttributes").pop(record["reserved"])
			message.get("MessageAttributes").pop(SQSExtendedClientConstants.CONTENT_TYPE_ATTRIBUTE.value, None)
			message["ReceiptHandle"] = (
				SQSExtendedClientConstants.S3_BUCKET_NAME_MARKER.value
				+ record["bucket"]
				+ SQSExtendedClientConstants.S3_BUCKET_NAME_MARKER.value
				+ SQSExtendedClientConstants.S3_KEY_MARKER.value
				+ record["key"]
				+ SQSExtendedClientConstants.S3_KEY_MARKER.value
				+ message.get("ReceiptHandle")
			)
			hydrated.append(message)
		return hydrated

	def _content_type_of(self, message):
		attrs = message.get("MessageAttributes") or {}
		entry = attrs.get(SQSExtendedClientConstants.CONTENT_TYPE_ATTRIBUTE.value) or {}
		if isinstance(entry, dict):
			return entry.get("StringValue") or "text"
		return "text"

	def _decode_inline_binary(self, message, content_type):
		if content_type != "binary-b64":
			return
		body = message.get("Body")
		if isinstance(body, str):
			body = body.encode("ascii")
		message["Body"] = base64.b64decode(body)
		message["_is_binary"] = True
		attrs = message.get("MessageAttributes")
		if attrs:
			attrs.pop(SQSExtendedClientConstants.CONTENT_TYPE_ATTRIBUTE.value, None)

	def _payload_download_path(self, payload_dir, s3_key):
		base = os.path.basename(str(s3_key)) or "payload"
		base = base.replace("..", "_")
		return os.path.join(payload_dir, "{}-{}".format(uuid.uuid4().hex, base))

	def _hydrate_from_s3(self, s3_bucket_name, s3_key, as_binary=False, filename=None):
		return self.get_payload_from_s3(
			s3_bucket_name, s3_key, as_binary=as_binary, filename=filename
		)

	def _is_confirmed_s3_not_found(self, exc):
		"""True only for a confirmed missing object. Timeouts and 5xx are not."""
		if not isinstance(exc, ClientError):
			return False
		response = exc.response if isinstance(getattr(exc, "response", None), dict) else {}
		error = response.get("Error") if isinstance(response.get("Error"), dict) else {}
		code = error.get("Code")
		meta = response.get("ResponseMetadata") if isinstance(response.get("ResponseMetadata"), dict) else {}
		status = meta.get("HTTPStatusCode")
		if isinstance(status, int) and status >= 500:
			return False
		return code in _CONFIRMED_S3_NOT_FOUND_CODES

	def _s3_target_from_receipt_handle(self, receipt_handle):
		bucket = self._get_bucket_marker_from_receipt_handle(
			receipt_handle, SQSExtendedClientConstants.S3_BUCKET_NAME_MARKER.value
		)
		key = self._get_bucket_marker_from_receipt_handle(
			receipt_handle, SQSExtendedClientConstants.S3_KEY_MARKER.value
		)
		return bucket, key

	def _delete_message_payload_from_s3(self, receipt_handle):
		"""Best-effort S3 cleanup after the SQS delete has already succeeded."""
		if not self.config.cleanup_s3_payload:
			return
		s3_msg_bucket_name, s3_msg_key = self._s3_target_from_receipt_handle(receipt_handle)
		started = time.time()
		try:
			self.s3.delete_object(Bucket=s3_msg_bucket_name, Key=s3_msg_key)
			self._emit(
				"s3_delete",
				s3_key=s3_msg_key,
				duration_ms=int((time.time() - started) * 1000),
			)
			logger.info("Deleted s3 object s3://%s/%s", s3_msg_bucket_name, s3_msg_key)
		except Exception:
			logger.warning(
				"Failed to delete S3 payload s3://%s/%s after the SQS message was deleted. "
				"The object is orphaned until a lifecycle rule or a later cleanup removes it.",
				s3_msg_bucket_name,
				s3_msg_key,
				exc_info=True,
			)
			self._emit(
				EVENT_S3_OBJECT_ORPHANED,
				s3_bucket_name=s3_msg_bucket_name,
				s3_key=s3_msg_key,
				case=ORPHAN_AFTER_SQS_SUCCEEDED,
			)

	def _pointer_bucket_and_key(self, body):
		try:
			parsed = self._parse_s3_pointer(body)
		except (ValueError, TypeError):
			return None
		if not isinstance(parsed, dict):
			return None
		bucket = parsed.get("s3BucketName")
		key = parsed.get("s3Key")
		if not bucket or not key:
			return None
		return bucket, key

	def _client_error_status(self, exc):
		if not isinstance(exc, ClientError):
			return None
		response = getattr(exc, "response", None)
		if not isinstance(response, dict):
			return None
		meta = response.get("ResponseMetadata")
		if not isinstance(meta, dict):
			return None
		status = meta.get("HTTPStatusCode")
		if isinstance(status, bool) or not isinstance(status, int):
			return None
		return status

	def _retry_attempts(self, exc):
		"""Return an explicit ``RetryAttempts`` int, or None when it is absent.

		Missing metadata is not zero. Only a present value of 0 means boto3
		did not retry, so a 4xx with the key omitted stays uncertain.
		"""
		response = getattr(exc, "response", None)
		if not isinstance(response, dict):
			return None
		meta = response.get("ResponseMetadata")
		if not isinstance(meta, dict) or "RetryAttempts" not in meta:
			return None
		attempts = meta.get("RetryAttempts")
		if isinstance(attempts, bool) or not isinstance(attempts, int):
			return None
		return attempts

	def _register_sqs_send_attempt_hook(self):
		"""Record before-send once per botocore SQS client.

		botocore emits ``before-send`` after signing and immediately before the
		HTTP attempt. A refresh callback runs during the re-sign of a retry,
		which is before that attempt's ``before-send`` and after attempt 1
		already emitted it. The callback can raise any exception.

		The handler is the tracker's ``note`` method. It does not reference
		this extended client. ``unique_id`` is per SQS client and operation, so
		another wrapper of the same client does not add a handler. Injected
		clients that are not botocore clients keep the type-based fallback.
		"""
		if self._sqs_attempt_hook_registered:
			return
		events = _live_sqs_events(self.sqs)
		if events is None:
			return
		tracker = _tracker_for(self.sqs)
		with tracker.lock:
			tracker.users += 1
			if not tracker.registered:
				for operation in _SQS_SEND_ATTEMPT_OPERATIONS:
					events.register(
						"before-send.sqs.%s" % operation,
						tracker.note,
						unique_id=_send_attempt_hook_id(operation),
					)
				tracker.registered = True
		self._send_attempt_tracker = tracker
		self._sqs_attempt_hook_registered = True
		self._hook_released = False

	def _release_sqs_send_attempt_hook(self):
		"""Drop this wrapper's use of the shared before-send handler."""
		if self._hook_released:
			return
		self._hook_released = True
		self._sqs_attempt_hook_registered = False
		tracker = self._send_attempt_tracker
		self._send_attempt_tracker = None
		if tracker is not None:
			events = _live_sqs_events(self.sqs)
			with tracker.lock:
				if tracker.users > 0:
					tracker.users -= 1
				if tracker.users == 0 and tracker.registered and events is not None:
					for operation in _SQS_SEND_ATTEMPT_OPERATIONS:
						events.unregister(
							"before-send.sqs.%s" % operation,
							unique_id=_send_attempt_hook_id(operation),
						)
					tracker.registered = False

	def _reset_sqs_send_attempts(self):
		tracker = self._send_attempt_tracker
		if tracker is not None:
			tracker.reset()

	def _sqs_send_was_attempted(self):
		tracker = self._send_attempt_tracker
		if tracker is None:
			return False
		return tracker.was_attempted()

	def _sqs_send_attempt_count(self):
		tracker = self._send_attempt_tracker
		if tracker is None:
			return 0
		return tracker.count()

	def _is_pre_send_validation_error(self, exc):
		"""Fallback when the SQS client has no before-send events.

		Used for injected clients that are not botocore clients. These types
		are the ones botocore raises while validating parameters or resolving
		a missing credential provider, which is before an HTTP attempt on a
		real client. A refresh callback can raise the same types after an
		attempt was already sent; that case is decided by ``before-send``.
		"""
		return isinstance(exc, (
			ParamValidationError,
			ValidationError,
			MissingParametersError,
			NoCredentialsError,
			UnknownCredentialError,
		))

	def _is_sqs_send_operation(self, exc):
		"""True when the error is from SendMessage or SendMessageBatch."""
		return getattr(exc, "operation_name", None) in (
			"SendMessage",
			"SendMessageBatch",
		)

	def _is_definite_sqs_rejection(self, exc):
		"""True only when the send cannot already be stored in SQS.

		A botocore ``ClientError`` whose HTTP status is 4xx is a rejection
		only when its operation is ``SendMessage`` or ``SendMessageBatch`` and
		``RetryAttempts`` is explicitly 0. A missing value is not zero. A 4xx
		from STS (for example ``AssumeRole``) during a credential refresh is
		not an SQS rejection. boto3 retries a timeout, then a later 400
		``RequestThrottled`` can report ``RetryAttempts`` greater than 0: the
		first attempt may already be stored.

		Any other exception is a definite rejection only when this call's
		``before-send`` event did not run, so no HTTP attempt left the client.
		botocore re-signs before every retry, and the refresh callback can
		raise any type (including ``NoCredentialsError`` and
		``ParamValidationError``) after attempt 1 was already sent. The
		before-send count is used only for a real botocore client. A mock or
		other wrapper uses the type-based fallback, so a timeout stays uncertain.
		"""
		if isinstance(exc, ClientError):
			status = self._client_error_status(exc)
			if status is None or not (400 <= status <= 499):
				return False
			if not self._is_sqs_send_operation(exc):
				return False
			return self._retry_attempts(exc) == 0
		if self._sqs_attempt_hook_registered:
			return not self._sqs_send_was_attempted()
		return self._is_pre_send_validation_error(exc)

	def _retain_uncertain_upload(self, body):
		"""Keep an uploaded object when the send may already have been stored."""
		target = self._pointer_bucket_and_key(body)
		if target is None:
			return
		bucket, key = target
		logger.warning(
			"Left S3 payload s3://%s/%s in place because the SQS send outcome is uncertain. "
			"The message may already be stored. The object is left for the lifecycle rule.",
			bucket,
			key,
		)
		self._emit(
			EVENT_S3_OBJECT_ORPHANED,
			s3_bucket_name=bucket,
			s3_key=key,
			case=ORPHAN_AFTER_SQS_UNCERTAIN,
		)

	def _release_offloaded_send(self, body, message_attributes, definite_rejection):
		if not self._reserved_attribute_name_if_present(message_attributes):
			return
		if definite_rejection:
			self._best_effort_delete_pointer(body)
		else:
			self._retain_uncertain_upload(body)

	def _best_effort_delete_pointer(self, body):
		"""Delete an uploaded payload when SQS definitely rejected the message."""
		target = self._pointer_bucket_and_key(body)
		if target is None:
			return
		bucket, key = target
		try:
			self.s3.delete_object(Bucket=bucket, Key=key)
			logger.warning(
				"Deleted S3 payload s3://%s/%s because the SQS send did not complete.",
				bucket,
				key,
			)
		except Exception:
			logger.warning(
				"SQS send did not complete and S3 payload s3://%s/%s could not be deleted. "
				"The object is orphaned. Configure an S3 lifecycle rule on the payload prefix.",
				bucket,
				key,
				exc_info=True,
			)
			self._emit(
				EVENT_S3_OBJECT_ORPHANED,
				s3_bucket_name=bucket,
				s3_key=key,
				case=ORPHAN_AFTER_UPLOAD_SQS_REJECTED,
			)

	def _get_bucket_marker_from_receipt_handle(self, receipt_handle, marker):
		start_marker = receipt_handle.index(marker) + len(marker)
		end_marker = receipt_handle.rindex(marker, start_marker)
		return receipt_handle[start_marker:end_marker]

	def _get_orig_receipt_handle(self, receipt_handle):
		return receipt_handle[
			receipt_handle.rindex(SQSExtendedClientConstants.S3_KEY_MARKER.value)
			+ len(SQSExtendedClientConstants.S3_KEY_MARKER.value):
		]

	def _is_s3_receipt_handle(self, receipt_handle):
		if not receipt_handle:
			return False
		return (
			SQSExtendedClientConstants.S3_BUCKET_NAME_MARKER.value in receipt_handle
			and SQSExtendedClientConstants.S3_KEY_MARKER.value in receipt_handle
		)

	def delete_message(self, queue_url, receipt_handle, **kwargs):
		"""Delete an SQS message and, when configured, its S3 payload.

		Order is SQS first, then best-effort S3 cleanup. If SQS delete fails,
		the S3 object is left in place so the message can be retried. If S3
		cleanup fails afterward, the SQS delete still stands, the object is
		orphaned, and ``s3_object_orphaned`` is emitted with
		``case=after_sqs_succeeded``.

		``delete_s3_before_sqs=True`` raises ``ValueError`` when the client is
		constructed. There is no S3-then-SQS path and no dead-pointer path.
		"""
		s3_handle = None
		sqs_handle = receipt_handle
		if self.config.payload_support_enabled and self._is_s3_receipt_handle(receipt_handle):
			s3_handle = receipt_handle
			sqs_handle = self._get_orig_receipt_handle(receipt_handle)
		logger.debug("receipt_handle=%s", sqs_handle)
		params = {"QueueUrl": queue_url, "ReceiptHandle": sqs_handle}
		params.update(kwargs)
		result = self.sqs.delete_message(**params)
		if s3_handle:
			self._delete_message_payload_from_s3(s3_handle)
		return result

	def delete_message_batch(self, queue_url, entries, **kwargs):
		prepared = []
		s3_handles = []
		for entry in entries:
			entry = dict(entry)
			handle = entry.get("ReceiptHandle")
			if self.config.payload_support_enabled and self._is_s3_receipt_handle(handle):
				s3_handles.append((entry.get("Id"), handle))
				entry["ReceiptHandle"] = self._get_orig_receipt_handle(handle)
			prepared.append(entry)
		params = {"QueueUrl": queue_url, "Entries": prepared}
		params.update(kwargs)
		result = self.sqs.delete_message_batch(**params)
		if s3_handles:
			failed_ids = {item.get("Id") for item in (result or {}).get("Failed") or []}
			successful = (result or {}).get("Successful")
			successful_ids = None if successful is None else {item.get("Id") for item in successful}
			pending = []
			for entry_id, handle in s3_handles:
				if entry_id in failed_ids:
					continue
				if successful_ids is not None and entry_id not in successful_ids:
					continue
				pending.append(handle)
			if pending:
				futures = [
					self._executor().submit(self._delete_message_payload_from_s3, handle)
					for handle in pending
				]
				for future in as_completed(futures):
					future.result()
		return result

	def change_message_visibility(self, queue_url, receipt_handle, visibility_timeout, **kwargs):
		if self._is_s3_receipt_handle(receipt_handle):
			receipt_handle = self._get_orig_receipt_handle(receipt_handle)
		params = {
			"QueueUrl": queue_url,
			"ReceiptHandle": receipt_handle,
			"VisibilityTimeout": visibility_timeout,
		}
		params.update(kwargs)
		return self.sqs.change_message_visibility(**params)

	def change_message_visibility_batch(self, queue_url, entries, **kwargs):
		prepared = []
		for entry in entries:
			entry = dict(entry)
			handle = entry.get("ReceiptHandle")
			if self._is_s3_receipt_handle(handle):
				entry["ReceiptHandle"] = self._get_orig_receipt_handle(handle)
			prepared.append(entry)
		params = {"QueueUrl": queue_url, "Entries": prepared}
		params.update(kwargs)
		return self.sqs.change_message_visibility_batch(**params)

	def purge_queue(self, queue_url, **kwargs):
		logger.warning("Calling purge_queue deletes SQS messages without deleting their payload from S3.")
		params = {"QueueUrl": queue_url}
		params.update(kwargs)
		return self.sqs.purge_queue(**params)

	def send_message(
		self,
		queue_url,
		message,
		message_group_id=None,
		message_deduplication_id=None,
		message_attributes=None,
		**kwargs
	):
		if message is None:
			raise ValueError("message_body required")
		if message_attributes is None:
			message_attributes = {}
		else:
			message_attributes = dict(message_attributes)

		if message_group_id is None:
			message_group_id = kwargs.pop("MessageGroupId", None)
		if message_deduplication_id is None:
			message_deduplication_id = kwargs.pop("MessageDeduplicationId", None)
		message_group_id = self._blank_id(message_group_id)
		message_deduplication_id = self._blank_id(message_deduplication_id)

		# A group id alone is allowed so a content-based-dedup FIFO queue can
		# send an inline body. A deduplication id still requires a group id.
		# This library never invents a deduplication id.
		if message_deduplication_id and not message_group_id:
			raise ValueError("message_group_id is required when message_deduplication_id is set")

		self._require_fifo_redrive_policy(queue_url)
		if (
			self._is_fifo_queue(queue_url)
			and not message_deduplication_id
			and self._payload_would_offload(message, message_attributes)
		):
			raise self._fifo_offload_without_dedup_error(queue_url)

		body, message_attributes = self._maybe_offload(message, message_attributes)
		params = {
			"QueueUrl": queue_url,
			"MessageBody": body,
			"MessageAttributes": message_attributes,
		}
		params.update(kwargs)
		if message_group_id:
			params["MessageGroupId"] = message_group_id
		if message_deduplication_id:
			params["MessageDeduplicationId"] = message_deduplication_id
		try:
			self._reset_sqs_send_attempts()
			return self.sqs.send_message(**params)
		except Exception as exc:
			self._release_offloaded_send(
				body, message_attributes, self._is_definite_sqs_rejection(exc)
			)
			raise

	def send_file(
		self,
		queue_url,
		path,
		message_group_id=None,
		message_deduplication_id=None,
		message_attributes=None,
		**kwargs
	):
		"""Upload an existing file. ``path`` may be a string or ``os.PathLike``.

		``send_message`` treats every ``str`` as text, including strings that happen
		to name a file on disk. Use this method or pass a ``pathlib.Path``.
		"""
		return self.send_message(
			queue_url,
			pathlib.Path(os.fspath(path)),
			message_group_id=message_group_id,
			message_deduplication_id=message_deduplication_id,
			message_attributes=message_attributes,
			**kwargs
		)

	def send_message_batch(self, queue_url, entries, **kwargs):
		self._require_fifo_redrive_policy(queue_url)
		if not self.config.payload_support_enabled:
			prepared = []
			for entry in entries:
				entry = dict(entry)
				body = entry.get("MessageBody")
				if body is None:
					raise ValueError("message_body required")
				if not isinstance(body, str):
					raise TypeError(
						"payload support is disabled; MessageBody must be str, got %s" % type(body).__name__
					)
				prepared.append(entry)
			params = {"QueueUrl": queue_url, "Entries": prepared}
			params.update(kwargs)
			return self.sqs.send_message_batch(**params)

		prepared = []
		offload_jobs = []
		fifo_queue = self._is_fifo_queue(queue_url)
		for index, entry in enumerate(entries):
			entry = dict(entry)
			body = entry.get("MessageBody")
			if body is None:
				raise ValueError("message_body required")
			attrs = dict(entry.get("MessageAttributes") or {})
			payload, size_bytes, is_binary, is_streamable = self._normalize_payload(body)
			self._validate_message_attributes(attrs)
			if self._should_offload(size_bytes, attrs, is_binary):
				if fifo_queue and not self._entry_deduplication_id(entry):
					raise self._fifo_offload_without_dedup_error(queue_url)
				self._ensure_binary_offload_attribute_room(attrs, is_binary)
				self._require_s3_bucket()
				offload_jobs.append((index, payload, size_bytes, is_streamable, is_binary, attrs))
			else:
				data = self._read_small_payload(payload, is_streamable)
				inline, attrs = self._encode_small_body(data, is_binary, attrs)
				entry["MessageBody"] = inline
				entry["_attrs"] = attrs
			prepared.append(entry)

		if offload_jobs:
			future_to_index = {
				self._executor().submit(self._store_message_in_s3, payload, size_bytes, is_streamable): (
					index, size_bytes, is_binary, attrs
				)
				for index, payload, size_bytes, is_streamable, is_binary, attrs in offload_jobs
			}
			results = {}
			first_error = None
			for future in as_completed(future_to_index):
				index, size_bytes, is_binary, attrs = future_to_index[future]
				try:
					results[index] = (future.result(), size_bytes, is_binary, attrs)
				except Exception as exc:
					if first_error is None:
						first_error = exc
			if first_error is not None:
				for pointer, _size_bytes, _is_binary, _attrs in results.values():
					self._best_effort_delete_pointer(self._to_payload_s3_pointer_json(pointer))
				raise first_error
			for index, (pointer, size_bytes, is_binary, attrs) in results.items():
				self._attach_offload_attributes(attrs, size_bytes, is_binary)
				prepared[index]["MessageBody"] = self._to_payload_s3_pointer_json(pointer)
				prepared[index]["_attrs"] = attrs

		sqs_entries = []
		for entry in prepared:
			attrs = entry.pop("_attrs")
			if attrs:
				entry["MessageAttributes"] = attrs
			sqs_entries.append(entry)
		params = {"QueueUrl": queue_url, "Entries": sqs_entries}
		params.update(kwargs)
		try:
			self._reset_sqs_send_attempts()
			result = self.sqs.send_message_batch(**params)
		except Exception as exc:
			definite = self._is_definite_sqs_rejection(exc)
			for entry in sqs_entries:
				self._release_offloaded_send(
					entry.get("MessageBody"), entry.get("MessageAttributes"), definite
				)
			raise
		failed = {
			item.get("Id"): item for item in (result or {}).get("Failed") or []
		}
		if failed:
			for entry in sqs_entries:
				item = failed.get(entry.get("Id"))
				if item is None:
					continue
				self._release_offloaded_send(
					entry.get("MessageBody"),
					entry.get("MessageAttributes"),
					item.get("SenderFault") is True,
				)
		return result

	def _s3_extra_args(self):
		extra = {}
		if self.config.s3_canned_acl:
			extra["ACL"] = self.config.s3_canned_acl
		strategy = self.config.server_side_encryption
		if strategy is not None:
			extra.update(strategy.extra_args())
		return extra or None

	def _store_message_in_s3(self, payload, size_bytes, is_streamable):
		s3_key = self.config.s3_key_prefix + str(uuid.uuid4())
		started = time.time()
		extra_args = self._s3_extra_args()
		transfer_cfg = self._transfer_config()
		try:
			if is_streamable and isinstance(payload, str):
				kwargs = {
					"Filename": payload,
					"Bucket": self.config.s3_bucket_name,
					"Key": s3_key,
					"Config": transfer_cfg,
				}
				if extra_args:
					kwargs["ExtraArgs"] = extra_args
				self.s3.upload_file(**kwargs)
			else:
				fileobj = payload if is_streamable else BytesIO(payload)
				kwargs = {
					"Fileobj": fileobj,
					"Bucket": self.config.s3_bucket_name,
					"Key": s3_key,
					"Config": transfer_cfg,
				}
				if extra_args:
					kwargs["ExtraArgs"] = extra_args
				self.s3.upload_fileobj(**kwargs)
			self._emit(
				"s3_offload",
				bytes=size_bytes,
				s3_key=s3_key,
				duration_ms=int((time.time() - started) * 1000),
			)
			return {"s3BucketName": self.config.s3_bucket_name, "s3Key": s3_key}
		except Exception:
			logger.exception(
				"Failed to store the message content in an S3 object. SQS message was not sent."
			)
			raise

	def get_payload_from_s3(self, s3_bucket_name, s3_key, as_binary=False, filename=None):
		"""Download an S3 payload.

		Without ``filename``, the object is read fully into memory and returned as
		``str`` (or ``bytes`` when ``as_binary`` is true). Pass ``filename`` to stream
		the object to disk with ``download_file`` and return that path. That is the
		same path ``receive_message(..., payload_dir=...)`` uses for large bodies.
		"""
		started = time.time()
		if filename:
			parent = os.path.dirname(filename)
			if parent:
				os.makedirs(parent, exist_ok=True)
			self.s3.download_file(
				s3_bucket_name,
				s3_key,
				filename,
				Config=self._transfer_config(),
			)
			size = os.path.getsize(filename)
			self._emit(
				"s3_hydrate",
				bytes=size,
				s3_key=s3_key,
				duration_ms=int((time.time() - started) * 1000),
			)
			return filename
		buffer = BytesIO()
		self.s3.download_fileobj(
			s3_bucket_name,
			s3_key,
			buffer,
			Config=self._transfer_config(),
		)
		buffer.seek(0)
		body = buffer.read()
		self._emit(
			"s3_hydrate",
			bytes=len(body),
			s3_key=s3_key,
			duration_ms=int((time.time() - started) * 1000),
		)
		if as_binary:
			return body
		if isinstance(body, bytes):
			return body.decode("utf-8")
		return body

	def get_text_from_s3(self, s3_bucket_name, s3_key):
		return self.get_payload_from_s3(s3_bucket_name, s3_key, as_binary=False)
