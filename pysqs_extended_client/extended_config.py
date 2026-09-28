import re


DEFAULT_MESSAGE_SIZE_THRESHOLD = 262144
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
	"""Options that control large-payload offload and worker-scale client settings."""

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
		# Java deletes the S3 object before DeleteMessage. That leaves a dead
		# pointer when SQS delete fails. The default here deletes SQS first and
		# then best-effort cleans S3. Set delete_s3_before_sqs=True for the Java order.
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
		object.__setattr__(self, name, value)

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
