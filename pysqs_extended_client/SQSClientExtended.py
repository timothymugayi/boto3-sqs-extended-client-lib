import base64
import binascii
import json
import logging
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
		self.aws_access_key_id = aws_access_key_id
		self.aws_secret_access_key = aws_secret_access_key
		self.aws_region_name = aws_region_name
		if config is None:
			self.config = ExtendedClientConfiguration(s3_bucket_name=s3_bucket_name)
		else:
			self.config = config
			if s3_bucket_name and not self.config.s3_bucket_name:
				self.config.s3_bucket_name = s3_bucket_name
		self.sqs = sqs_client if sqs_client is not None else self._build_client("sqs")
		self.s3 = s3_client if s3_client is not None else self._build_client("s3")
		self._pool = None
		self._pool_lock = threading.Lock()
		self.config.freeze()

	def _build_client(self, service_name):
		kwargs = {"config": self._botocore_config()}
		if self.config.endpoint_url:
			kwargs["endpoint_url"] = self.config.endpoint_url
		if self.aws_access_key_id and self.aws_secret_access_key and self.aws_region_name:
			kwargs.update(
				aws_access_key_id=self.aws_access_key_id,
				aws_secret_access_key=self.aws_secret_access_key,
				region_name=self.aws_region_name,
			)
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
		return True

	def set_always_through_s3(self, always_through_s3):
		self.config.always_through_s3 = always_through_s3

	def set_message_size_threshold(self, message_size_threshold):
		self.config.message_size_threshold = message_size_threshold

	def __getattr__(self, name):
		return getattr(self.sqs, name)

	def _get_string_size_in_bytes(self, message_body):
		return len(message_body.encode("utf-8"))

	def _string_to_base64(self, s):
		return base64.b64encode(s.encode("utf-8"))

	def _base64_to_string(self, b):
		return base64.b64decode(b).decode("utf-8")

	def _is_base64(self, value):
		try:
			encoded = self._string_to_base64(self._base64_to_string(value))
			return encoded == value.encode("utf-8")
		except (TypeError, ValueError, UnicodeError, binascii.Error, AttributeError):
			return False

	def _get_msg_attributes_size(self, message_attributes):
		total_msg_attributes_size = 0
		for key, entry in message_attributes.items():
			total_msg_attributes_size += self._get_string_size_in_bytes(key)
			if entry.get("DataType"):
				total_msg_attributes_size += self._get_string_size_in_bytes(entry.get("DataType"))
			if entry.get("StringValue"):
				total_msg_attributes_size += self._get_string_size_in_bytes(entry.get("StringValue"))
			if entry.get("BinaryValue"):
				if self._is_base64(entry.get("BinaryValue")):
					total_msg_attributes_size += len(entry.get("BinaryValue").encode("utf-8"))
				else:
					total_msg_attributes_size += self._get_string_size_in_bytes(entry.get("BinaryValue"))
		return total_msg_attributes_size

	def _is_large(self, message, message_attributes):
		msg_attributes_size = self._get_msg_attributes_size(message_attributes)
		msg_body_size = self._get_string_size_in_bytes(message)
		return (msg_attributes_size + msg_body_size) > self.config.message_size_threshold

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

	def _should_offload(self, message, message_attributes):
		return self.config.always_through_s3 or self._is_large(str(message), message_attributes)

	def _apply_offload_pointer(self, message, message_attributes, pointer):
		message_attributes[self._payload_size_attribute_name()] = {
			"StringValue": str(self._get_string_size_in_bytes(str(message))),
			"DataType": "Number",
		}
		return self._to_payload_s3_pointer_json(pointer), message_attributes

	def _maybe_offload(self, message, message_attributes):
		self._validate_message_attributes(message_attributes)
		if self._should_offload(message, message_attributes):
			if not self.config.s3_bucket_name or not str(self.config.s3_bucket_name).strip():
				raise ValueError("S3 bucket name cannot be null")
			pointer = self._store_message_in_s3(message)
			return self._apply_offload_pointer(message, message_attributes, pointer)
		return message, message_attributes

	def receive_message(self, queue_url, max_number_of_messages=1, wait_time_seconds=10, **kwargs):
		"""
		Retrieves one or more messages (up to 10) and hydrates S3 payloads in parallel.
		"""
		if "max_number_Of_Messages" in kwargs:
			max_number_of_messages = kwargs.pop("max_number_Of_Messages")
		message_attribute_names = list(kwargs.pop("MessageAttributeNames", ["All"]))
		if "All" not in message_attribute_names:
			for reserved in (
				SQSExtendedClientConstants.RESERVED_ATTRIBUTE_NAME.value,
				SQSExtendedClientConstants.RESERVED_ATTRIBUTE_NAME_EXTENDED.value,
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
		opt_messages = response_opt_queue.get("Messages", [])
		if not opt_messages:
			return None

		parsed = []
		hydrate_jobs = []
		for index, message in enumerate(opt_messages):
			reserved_attribute_name = self._reserved_attribute_name_if_present(message.get("MessageAttributes", {}))
			if not reserved_attribute_name:
				parsed.append((index, message, None, None, None))
				continue
			try:
				message_body = self._parse_s3_pointer(message.get("Body"))
			except (ValueError, TypeError):
				raise ValueError("Decoding JSON has failed")
			if "s3BucketName" not in message_body and "s3Key" not in message_body:
				raise ValueError("Detected missing required key attribute s3BucketName and s3Key in s3 payload")
			s3_bucket_name = message_body.get("s3BucketName")
			s3_key = message_body.get("s3Key")
			parsed.append((index, message, reserved_attribute_name, s3_bucket_name, s3_key))
			hydrate_jobs.append((index, s3_bucket_name, s3_key))

		bodies = {}
		errors = {}
		if hydrate_jobs:
			future_to_index = {
				self._executor().submit(self._hydrate_from_s3, bucket, key): index
				for index, bucket, key in hydrate_jobs
			}
			for future in as_completed(future_to_index):
				index = future_to_index[future]
				try:
					bodies[index] = future.result()
				except Exception as exc:
					errors[index] = exc

		hydrated = []
		for index, message, reserved_attribute_name, s3_bucket_name, s3_key in parsed:
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
			message["Body"] = orig_msg_body
			message.get("MessageAttributes").pop(reserved_attribute_name)
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

	def _hydrate_from_s3(self, s3_bucket_name, s3_key):
		return self.get_text_from_s3(s3_bucket_name, s3_key)

	def _is_missing_s3_object(self, exc):
		return exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404")

	def _delete_message_payload_from_s3(self, receipt_handle):
		if not self.config.cleanup_s3_payload:
			return
		s3_msg_bucket_name = self._get_bucket_marker_from_receipt_handle(
			receipt_handle, SQSExtendedClientConstants.S3_BUCKET_NAME_MARKER.value
		)
		s3_msg_key = self._get_bucket_marker_from_receipt_handle(
			receipt_handle, SQSExtendedClientConstants.S3_KEY_MARKER.value
		)
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
			logger.exception("Failed to delete the message content in S3 object.")
			raise

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
		if self._is_s3_receipt_handle(receipt_handle):
			self._delete_message_payload_from_s3(receipt_handle)
			receipt_handle = self._get_orig_receipt_handle(receipt_handle)
		logger.debug("receipt_handle=%s", receipt_handle)
		params = {"QueueUrl": queue_url, "ReceiptHandle": receipt_handle}
		params.update(kwargs)
		return self.sqs.delete_message(**params)

	def delete_message_batch(self, queue_url, entries, **kwargs):
		prepared = []
		delete_handles = []
		for entry in entries:
			entry = dict(entry)
			handle = entry.get("ReceiptHandle")
			if self._is_s3_receipt_handle(handle):
				delete_handles.append(handle)
				entry["ReceiptHandle"] = self._get_orig_receipt_handle(handle)
			prepared.append(entry)
		if delete_handles:
			futures = [self._executor().submit(self._delete_message_payload_from_s3, handle) for handle in delete_handles]
			for future in as_completed(futures):
				future.result()
		params = {"QueueUrl": queue_url, "Entries": prepared}
		params.update(kwargs)
		return self.sqs.delete_message_batch(**params)

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
		return self.sqs.send_message(**params)

	def send_message_batch(self, queue_url, entries, **kwargs):
		prepared = []
		offload_jobs = []
		for index, entry in enumerate(entries):
			entry = dict(entry)
			body = entry.get("MessageBody")
			if body is None:
				raise ValueError("message_body required")
			attrs = dict(entry.get("MessageAttributes") or {})
			self._validate_message_attributes(attrs)
			if self._should_offload(body, attrs):
				if not self.config.s3_bucket_name or not str(self.config.s3_bucket_name).strip():
					raise ValueError("S3 bucket name cannot be null")
				offload_jobs.append((index, body, attrs))
			entry["_attrs"] = attrs
			entry["_body"] = body
			prepared.append(entry)

		if offload_jobs:
			future_to_index = {
				self._executor().submit(self._store_message_in_s3, body): (index, body, attrs)
				for index, body, attrs in offload_jobs
			}
			results = {}
			first_error = None
			for future in as_completed(future_to_index):
				index, body, attrs = future_to_index[future]
				try:
					results[index] = (future.result(), body, attrs)
				except Exception as exc:
					if first_error is None:
						first_error = exc
			if first_error is not None:
				raise first_error
			for index, (pointer, body, attrs) in results.items():
				offloaded, attrs = self._apply_offload_pointer(body, attrs, pointer)
				prepared[index]["MessageBody"] = offloaded
				prepared[index]["_attrs"] = attrs

		sqs_entries = []
		for entry in prepared:
			attrs = entry.pop("_attrs")
			original_body = entry.pop("_body")
			entry.setdefault("MessageBody", original_body)
			if attrs:
				entry["MessageAttributes"] = attrs
			sqs_entries.append(entry)
		params = {"QueueUrl": queue_url, "Entries": sqs_entries}
		params.update(kwargs)
		return self.sqs.send_message_batch(**params)

	def _store_message_in_s3(self, message_body):
		s3_key = self.config.s3_key_prefix + str(uuid.uuid4())
		data = str(message_body).encode("utf-8")
		started = time.time()
		try:
			self.s3.upload_fileobj(
				BytesIO(data),
				self.config.s3_bucket_name,
				s3_key,
				Config=self._transfer_config(),
			)
			self._emit(
				"s3_offload",
				bytes=len(data),
				s3_key=s3_key,
				duration_ms=int((time.time() - started) * 1000),
			)
			return {"s3BucketName": self.config.s3_bucket_name, "s3Key": s3_key}
		except Exception:
			logger.exception(
				"Failed to store the message content in an S3 object. SQS message was not sent."
			)
			raise

	def get_text_from_s3(self, s3_bucket_name, s3_key):
		started = time.time()
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
		if isinstance(body, bytes):
			return body.decode("utf-8")
		return body
