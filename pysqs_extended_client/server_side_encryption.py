"""S3 server-side encryption options for payload uploads.

These match the Java ``ServerSideEncryptionStrategy`` used by the Amazon SQS
extended client (``AwsManagedCmk`` and ``CustomerKey``). ``sse_s3`` is the
boto3 SSE-S3 (AES256) option, which the Java factory does not expose.
"""


class ServerSideEncryptionStrategy:
	"""Produces boto3 ``ExtraArgs`` for an S3 upload."""

	def extra_args(self):
		raise NotImplementedError


class AwsManagedCmk(ServerSideEncryptionStrategy):
	"""SSE-KMS with the AWS-managed ``aws/s3`` key. Matches Java ``AwsManagedCmk``."""

	def extra_args(self):
		return {"ServerSideEncryption": "aws:kms"}


class CustomerKey(ServerSideEncryptionStrategy):
	"""SSE-KMS with a customer-managed key. Matches Java ``CustomerKey``."""

	def __init__(self, aws_kms_key_id):
		if aws_kms_key_id is None or not str(aws_kms_key_id).strip():
			raise ValueError("aws_kms_key_id is required for customer-managed SSE-KMS")
		self.aws_kms_key_id = str(aws_kms_key_id)

	def extra_args(self):
		return {
			"ServerSideEncryption": "aws:kms",
			"SSEKMSKeyId": self.aws_kms_key_id,
		}


class S3ManagedKey(ServerSideEncryptionStrategy):
	"""SSE-S3 (AES256), the bucket-managed S3 key."""

	def extra_args(self):
		return {"ServerSideEncryption": "AES256"}


def aws_managed_cmk():
	return AwsManagedCmk()


def customer_key(aws_kms_key_id):
	return CustomerKey(aws_kms_key_id)


def sse_s3():
	return S3ManagedKey()
