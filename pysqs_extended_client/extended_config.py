import re

from pysqs_extended_client.server_side_encryption import ServerSideEncryptionStrategy


DEFAULT_MESSAGE_SIZE_THRESHOLD = 262144
# SQS rejects a message body larger than 256 KiB. A threshold above that would
# try to send inline and fail at the service instead of offloading.
MIN_MESSAGE_SIZE_THRESHOLD = 1
MAX_MESSAGE_SIZE_THRESHOLD = DEFAULT_MESSAGE_SIZE_THRESHOLD
DEFAULT_MAX_POOL_CONNECTIONS = 50
DEFAULT_CONNECT_TIMEOUT = 3
DEFAULT_READ_TIMEOUT = 60
DEFAULT_RETRY_MAX_ATTEMPTS = 5
DEFAULT_S3_MAX_CONCURRENCY = 10
DEFAULT_MULTIPART_THRESHOLD = 8 * 1024 * 1024
INVALID_S3_PREFIX_KEY_CHARACTERS_PATTERN = re.compile(r"[^a-zA-Z0-9./_-]")
UUID_LENGTH = 36
MAX_S3_KEY_LENGTH = 1024
MAX_S3_KEY_PREFIX_LENGTH = MAX_S3_KEY_LENGTH - UUID_LENGTH


class FrozenConfigError(RuntimeError):
	"""Raised when an ExtendedClientConfiguration is mutated after the client is created."""


class ExtendedClientConfiguration:
	"""Options that control large-payload offload and worker-scale client settings.

	Checked at construction (and on later assignment, until the client freezes the
	config):

	* ``message_size_threshold`` is an int from 1 to 262144, or ``None`` for 262144.
	* ``s3_bucket_name`` is ``None`` or a non-blank ``str``. Surrounding whitespace
	  is stripped. A blank string raises ``ValueError``.
	* ``server_side_encryption`` is ``None`` or a ``ServerSideEncryptionStrategy``.

	``SQSClientExtended`` additionally requires a bucket when
	``payload_support_enabled`` is true, because that is when a send can offload.
	"""

	def __init__(
		self,
		s3_bucket_name=None,
		always_through_s3=False,
		message_size_threshold=None,
		cleanup_s3_payload=True,
		use_legacy_attribute=True,
		ignore_payload_not_found=False,
		s3_key_prefix="",
		max_pool_connections=DEFAULT_MAX_POOL_CONNECTIONS,
		connect_timeout=DEFAULT_CONNECT_TIMEOUT,
		read_timeout=DEFAULT_READ_TIMEOUT,
		retry_max_attempts=DEFAULT_RETRY_MAX_ATTEMPTS,
		s3_max_concurrency=DEFAULT_S3_MAX_CONCURRENCY,
		multipart_threshold=DEFAULT_MULTIPART_THRESHOLD,
		endpoint_url=None,
		s3_canned_acl=None,
		server_side_encryption=None,
		payload_support_enabled=True,
		delete_s3_before_sqs=False,
		on_event=None,
	):
		object.__setattr__(self, "_frozen", False)
		if message_size_threshold is None:
			message_size_threshold = DEFAULT_MESSAGE_SIZE_THRESHOLD
		self.s3_bucket_name = s3_bucket_name
		self.always_through_s3 = always_through_s3
		self.message_size_threshold = message_size_threshold
		self.cleanup_s3_payload = cleanup_s3_payload
		self.use_legacy_attribute = use_legacy_attribute
		# Retained so existing callers still construct. It does not delete the SQS
		# message. A confirmed missing object leaves that message in the queue.
		self.ignore_payload_not_found = ignore_payload_not_found
		self.s3_key_prefix = self.validate_s3_key_prefix(s3_key_prefix)
		self.max_pool_connections = max_pool_connections
		self.connect_timeout = connect_timeout
		self.read_timeout = read_timeout
		self.retry_max_attempts = retry_max_attempts
		self.s3_max_concurrency = s3_max_concurrency
		self.multipart_threshold = multipart_threshold
		self.endpoint_url = endpoint_url
		self.s3_canned_acl = s3_canned_acl
		self.server_side_encryption = server_side_encryption
		self.payload_support_enabled = payload_support_enabled
		# True is refused. delete_message and delete_message_batch raise before
		# either delete so the caller is not told the object was removed first.
		# There is no S3-then-SQS path and no dead-pointer path.
		self.delete_s3_before_sqs = delete_s3_before_sqs
		self.on_event = on_event

	def freeze(self):
		object.__setattr__(self, "_frozen", True)

	@property
	def frozen(self):
		return self._frozen

	def __setattr__(self, name, value):
		if name != "_frozen" and getattr(self, "_frozen", False):
			raise FrozenConfigError(
				"ExtendedClientConfiguration is frozen after SQSClientExtended is created. "
				"Pass options to ExtendedClientConfiguration before constructing the client."
			)
		if name == "s3_key_prefix":
			value = self.validate_s3_key_prefix(value)
		elif name == "message_size_threshold":
			value = self.validate_message_size_threshold(value)
		elif name == "s3_bucket_name":
			value = self.validate_s3_bucket_name(value)
		elif name == "server_side_encryption":
			value = self.validate_server_side_encryption(value)
		object.__setattr__(self, name, value)

	@staticmethod
	def validate_message_size_threshold(message_size_threshold):
		if isinstance(message_size_threshold, bool) or not isinstance(message_size_threshold, int):
			raise TypeError(
				"message_size_threshold must be an int from {} to {} "
				"(the SQS maximum message size is {} bytes), got {}.".format(
					MIN_MESSAGE_SIZE_THRESHOLD,
					MAX_MESSAGE_SIZE_THRESHOLD,
					MAX_MESSAGE_SIZE_THRESHOLD,
					type(message_size_threshold).__name__,
				)
			)
		if (
			message_size_threshold < MIN_MESSAGE_SIZE_THRESHOLD
			or message_size_threshold > MAX_MESSAGE_SIZE_THRESHOLD
		):
			raise ValueError(
				"message_size_threshold must be from {} to {} bytes "
				"(the SQS message size limit). Got {}.".format(
					MIN_MESSAGE_SIZE_THRESHOLD,
					MAX_MESSAGE_SIZE_THRESHOLD,
					message_size_threshold,
				)
			)
		return message_size_threshold

	@staticmethod
	def validate_s3_bucket_name(s3_bucket_name):
		if s3_bucket_name is None:
			return None
		if not isinstance(s3_bucket_name, str):
			raise TypeError(
				"s3_bucket_name must be a str or None, got {}.".format(type(s3_bucket_name).__name__)
			)
		trimmed = s3_bucket_name.strip()
		if not trimmed:
			raise ValueError(
				"s3_bucket_name cannot be blank. Omit it (None) or pass a non-empty bucket name."
			)
		return trimmed

	@staticmethod
	def validate_server_side_encryption(server_side_encryption):
		if server_side_encryption is None:
			return None
		if not isinstance(server_side_encryption, ServerSideEncryptionStrategy):
			raise TypeError(
				"server_side_encryption must be a ServerSideEncryptionStrategy "
				"or None, got {}. Use aws_managed_cmk(), customer_key(...), or sse_s3().".format(
					type(server_side_encryption).__name__
				)
			)
		return server_side_encryption

	@staticmethod
	def validate_s3_key_prefix(s3_key_prefix):
		trimmed = "" if s3_key_prefix is None else str(s3_key_prefix).strip()
		if len(trimmed) > MAX_S3_KEY_PREFIX_LENGTH:
			raise ValueError(
				"The S3 key prefix length must not be greater than {}".format(MAX_S3_KEY_PREFIX_LENGTH)
			)
		if trimmed.startswith(".") or trimmed.startswith("/"):
			raise ValueError("The S3 key prefix must not start with '.' or '/'")
		if ".." in trimmed:
			raise ValueError("The S3 key prefix must not contain the string '..'")
		if trimmed and INVALID_S3_PREFIX_KEY_CHARACTERS_PATTERN.search(trimmed):
			raise ValueError(
				"The S3 key prefix contains invalid characters. "
				"The allowed characters are: letters, digits, '/', '_', '-', and '.'"
			)
		return trimmed
