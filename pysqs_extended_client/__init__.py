from pysqs_extended_client.SQSClientExtended import SQSClientExtended, SQSExtendedClientConstants
from pysqs_extended_client.extended_config import ExtendedClientConfiguration, FrozenConfigError
from pysqs_extended_client.server_side_encryption import (
	AwsManagedCmk,
	CustomerKey,
	S3ManagedKey,
	ServerSideEncryptionStrategy,
	aws_managed_cmk,
	customer_key,
	sse_s3,
)

__title__ = "pysqs_client_extended"
__version__ = "0.4.0"

__all__ = [
	"SQSClientExtended",
	"SQSExtendedClientConstants",
	"ExtendedClientConfiguration",
	"FrozenConfigError",
	"ServerSideEncryptionStrategy",
	"AwsManagedCmk",
	"CustomerKey",
	"S3ManagedKey",
	"aws_managed_cmk",
	"customer_key",
	"sse_s3",
]
