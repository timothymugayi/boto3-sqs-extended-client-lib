import base64
import json
import logging
import os
import pathlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from io import BytesIO

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

from pysqs_extended_client.extended_config import ExtendedClientConfiguration


logger = logging.getLogger(__name__)


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


class SQSClientExtended:
	"""
	SQS client that offloads large payloads to S3, modelled on the Java extended client.

	Share one instance per process across worker threads. boto3 clients are created once
	with a sized connection pool. Batch S3 work is fanned out on a bounded executor.

	The four-argument constructor stays compatible with existing callers.
	Configure scale knobs on ExtendedClientConfiguration before constructing the client;
	the config is frozen after __init__.
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
			if s3_bucket_name and not self.config.s3_bucket_name:
				self.config.s3_bucket_name = s3_bucket_name
		# Keys are passed only into boto3 and are not retained on this instance.
		# Omit them to use the default credential chain (env, shared file, role).
		self.sqs = sqs_client if sqs_client is not None else self._build_client(
			"sqs", aws_access_key_id, aws_secret_access_key, aws_region_name
		)
		self.s3 = s3_client if s3_client is not None else self._build_client(
			"s3", aws_access_key_id, aws_secret_access_key, aws_region_name
		)
		self._pool = None
		self._pool_lock = threading.Lock()
		self.config.freeze()

	def _build_client(self, service_name, aws_access_key_id, aws_secret_access_key, aws_region_name):
		kwargs = {"config": self._botocore_config()}
		if self.config.endpoint_url:
			kwargs["endpoint_url"] = self.config.endpoint_url
		if aws_access_key_id and aws_secret_access_key and aws_region_name:
			kwargs.update(
				aws_access_key_id=aws_access_key_id,
				aws_secret_access_key=aws_secret_access_key,
				region_name=aws_region_name,
			)
		elif aws_region_name and not (aws_access_key_id or aws_secret_access_key):
			kwargs["region_name"] = aws_region_name
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
		if callback is not None:
			callback(name, **fields)

	def close(self):
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
		if isinstance(parsed, list) and parsed and parsed[0] in pointer_classes:
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

	def _should_offload(self, size_bytes, message_attributes):
		if self.config.always_through_s3:
			return True
		attr_size = self._get_msg_attributes_size(message_attributes)
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
		if self._should_offload(size_bytes, message_attributes):
			self._ensure_binary_offload_attribute_room(message_attributes, is_binary)
			self._require_s3_bucket()
			pointer = self._store_message_in_s3(payload, size_bytes, is_streamable)
			self._attach_offload_attributes(message_attributes, size_bytes, is_binary)
			return self._to_payload_s3_pointer_json(pointer), message_attributes
		data = self._read_small_payload(payload, is_streamable)
		return self._encode_small_body(data, is_binary, message_attributes)

	def receive_message(self, queue_url, max_number_of_messages=1, wait_time_seconds=10, payload_dir=None, **kwargs):
		"""
		Retrieves one or more messages (up to 10) and hydrates S3 payloads in parallel.

		Returns a list of messages. An empty queue returns ``[]``.

		By default each S3 payload is loaded fully into memory (``message["Body"]`` is
		``str`` or ``bytes``). Pass ``payload_dir`` to stream S3 objects to files with
		``download_file`` instead. Those messages set ``Body`` and ``_payload_path`` to
		the file path. Inline messages are unchanged. Use ``payload_dir`` for
		multi-hundred-MB bodies; the in-memory path is the Java-compatible default.
		"""
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

		parsed = []
		hydrate_jobs = []
		for index, message in enumerate(opt_messages):
			reserved_attribute_name = self._reserved_attribute_name_if_present(message.get("MessageAttributes", {}))
			content_type = self._content_type_of(message)
			if not reserved_attribute_name:
				self._decode_inline_binary(message, content_type)
				parsed.append((index, message, None, None, None, content_type, None))
				continue
			try:
				message_body = self._parse_s3_pointer(message.get("Body"))
			except (ValueError, TypeError):
				raise ValueError("Decoding JSON has failed")
			if "s3BucketName" not in message_body and "s3Key" not in message_body:
				raise ValueError("Detected missing required key attribute s3BucketName and s3Key in s3 payload")
			s3_bucket_name = message_body.get("s3BucketName")
			s3_key = message_body.get("s3Key")
			as_binary = content_type in ("binary", "binary-b64")
			filename = self._payload_download_path(payload_dir, s3_key) if payload_dir else None
			parsed.append((index, message, reserved_attribute_name, s3_bucket_name, s3_key, content_type, filename))
			hydrate_jobs.append((index, s3_bucket_name, s3_key, as_binary, filename))

		bodies = {}
		errors = {}
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

		hydrated = []
		for index, message, reserved_attribute_name, s3_bucket_name, s3_key, content_type, filename in parsed:
			if reserved_attribute_name is None:
				hydrated.append(message)
				continue
			if index in errors:
				exc = errors[index]
				if isinstance(exc, ClientError) and self._is_missing_s3_object(exc) and self.config.ignore_payload_not_found:
					self.sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message.get("ReceiptHandle"))
					logger.warning("Message deleted from SQS since payload with pointer could not be found in S3.")
					continue
				raise exc
			orig_msg_body = bodies.get(index)
			if orig_msg_body is None:
				if self.config.ignore_payload_not_found:
					self.sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message.get("ReceiptHandle"))
					logger.warning("Message deleted from SQS since payload with pointer could not be found in S3.")
					continue
				raise ValueError("S3 payload was not found for key {}".format(s3_key))
			if filename:
				message["Body"] = orig_msg_body
				message["_payload_path"] = orig_msg_body
			else:
				if content_type == "binary-b64":
					orig_msg_body = base64.b64decode(orig_msg_body)
				message["Body"] = orig_msg_body
			if content_type in ("binary", "binary-b64"):
				message["_is_binary"] = True
			message.get("MessageAttributes").pop(reserved_attribute_name)
			message.get("MessageAttributes").pop(SQSExtendedClientConstants.CONTENT_TYPE_ATTRIBUTE.value, None)
			message["ReceiptHandle"] = (
				SQSExtendedClientConstants.S3_BUCKET_NAME_MARKER.value
				+ s3_bucket_name
				+ SQSExtendedClientConstants.S3_BUCKET_NAME_MARKER.value
				+ SQSExtendedClientConstants.S3_KEY_MARKER.value
				+ s3_key
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

	def _is_missing_s3_object(self, exc):
		return exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404")

	def _s3_target_from_receipt_handle(self, receipt_handle):
		bucket = self._get_bucket_marker_from_receipt_handle(
			receipt_handle, SQSExtendedClientConstants.S3_BUCKET_NAME_MARKER.value
		)
		key = self._get_bucket_marker_from_receipt_handle(
			receipt_handle, SQSExtendedClientConstants.S3_KEY_MARKER.value
		)
		return bucket, key

	def _delete_message_payload_from_s3(self, receipt_handle, best_effort=False):
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
			if best_effort:
				logger.warning(
					"Failed to delete S3 payload s3://%s/%s after the SQS message was deleted. "
					"The object is orphaned until a lifecycle rule or a later cleanup removes it.",
					s3_msg_bucket_name,
					s3_msg_key,
					exc_info=True,
				)
				return
			logger.exception("Failed to delete the message content in S3 object.")
			raise

	def _best_effort_delete_pointer(self, body):
		"""Delete an uploaded payload when SQS never accepted the message."""
		try:
			parsed = self._parse_s3_pointer(body)
		except (ValueError, TypeError):
			return
		if not isinstance(parsed, dict):
			return
		bucket = parsed.get("s3BucketName")
		key = parsed.get("s3Key")
		if not bucket or not key:
			return
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

		Default order is SQS first, then best-effort S3 cleanup. If SQS delete
		fails, the S3 object is left in place so the message can be retried.
		If S3 cleanup fails afterward, the SQS delete still stands and the object
		is orphaned (use a lifecycle rule).

		``delete_s3_before_sqs=True`` matches the Java extended client: S3 is
		removed first. If the following SQS delete fails, the message remains and
		points at a missing object (a dead pointer). Receipt-handle markers stay
		compatible with Java either way.
		"""
		s3_handle = None
		sqs_handle = receipt_handle
		if self.config.payload_support_enabled and self._is_s3_receipt_handle(receipt_handle):
			s3_handle = receipt_handle
			sqs_handle = self._get_orig_receipt_handle(receipt_handle)
		logger.debug("receipt_handle=%s", sqs_handle)
		params = {"QueueUrl": queue_url, "ReceiptHandle": sqs_handle}
		params.update(kwargs)
		if s3_handle and self.config.delete_s3_before_sqs:
			removed_s3 = bool(self.config.cleanup_s3_payload)
			self._delete_message_payload_from_s3(s3_handle, best_effort=False)
			try:
				return self.sqs.delete_message(**params)
			except Exception:
				if removed_s3:
					bucket, key = self._s3_target_from_receipt_handle(s3_handle)
					logger.error(
						"SQS delete failed after the S3 payload was removed. "
						"The message now points at a missing object (dead pointer) s3://%s/%s. "
						"This is the Java extended-client order (delete_s3_before_sqs=True). "
						"The default order deletes SQS first.",
						bucket,
						key,
					)
				raise
		result = self.sqs.delete_message(**params)
		if s3_handle:
			self._delete_message_payload_from_s3(s3_handle, best_effort=True)
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
		removed_s3 = bool(s3_handles and self.config.delete_s3_before_sqs and self.config.cleanup_s3_payload)
		if s3_handles and self.config.delete_s3_before_sqs:
			futures = [
				self._executor().submit(self._delete_message_payload_from_s3, handle, False)
				for _, handle in s3_handles
			]
			for future in as_completed(futures):
				future.result()
		params = {"QueueUrl": queue_url, "Entries": prepared}
		params.update(kwargs)
		try:
			result = self.sqs.delete_message_batch(**params)
		except Exception:
			if removed_s3:
				logger.error(
					"SQS DeleteMessageBatch failed after S3 payloads were removed. "
					"Those messages now point at missing objects (dead pointers)."
				)
			raise
		if s3_handles and not self.config.delete_s3_before_sqs:
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
					self._executor().submit(self._delete_message_payload_from_s3, handle, True)
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

		if not all([message_group_id, message_deduplication_id]):
			if any([message_group_id, message_deduplication_id]):
				raise ValueError("message_group_id and message_deduplication_id are conditionally required together")

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
			return self.sqs.send_message(**params)
		except Exception:
			if self._reserved_attribute_name_if_present(message_attributes):
				self._best_effort_delete_pointer(body)
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
		for index, entry in enumerate(entries):
			entry = dict(entry)
			body = entry.get("MessageBody")
			if body is None:
				raise ValueError("message_body required")
			attrs = dict(entry.get("MessageAttributes") or {})
			payload, size_bytes, is_binary, is_streamable = self._normalize_payload(body)
			self._validate_message_attributes(attrs)
			if self._should_offload(size_bytes, attrs):
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
			result = self.sqs.send_message_batch(**params)
		except Exception:
			for entry in sqs_entries:
				if self._reserved_attribute_name_if_present(entry.get("MessageAttributes")):
					self._best_effort_delete_pointer(entry.get("MessageBody"))
			raise
		failed_ids = {item.get("Id") for item in (result or {}).get("Failed") or []}
		if failed_ids:
			for entry in sqs_entries:
				if entry.get("Id") in failed_ids and self._reserved_attribute_name_if_present(entry.get("MessageAttributes")):
					self._best_effort_delete_pointer(entry.get("MessageBody"))
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
