import os
import uuid

import pytest
import urllib.request
from botocore.exceptions import ClientError

from pysqs_extended_client import SQSClientExtended, ExtendedClientConfiguration
from pysqs_extended_client.SQSClientExtended import SQSExtendedClientConstants

LOCALSTACK_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://127.0.0.1:4566")
S3_BUCKET = os.environ.get("SQS_EXTENDED_S3_BUCKET", "sqs-extended-payloads")


def _localstack_available():
	try:
		urllib.request.urlopen(LOCALSTACK_ENDPOINT.rstrip("/") + "/_localstack/health", timeout=1)
		return True
	except Exception:
		return False


pytestmark = [
	pytest.mark.localstack,
	pytest.mark.skipif(
		not _localstack_available(),
		reason="LocalStack is not running on {}".format(LOCALSTACK_ENDPOINT),
	),
]


def _client(**config_kwargs):
	config_kwargs.setdefault("s3_bucket_name", S3_BUCKET)
	config_kwargs.setdefault("endpoint_url", LOCALSTACK_ENDPOINT)
	config_kwargs.setdefault("s3_key_prefix", "tests/")
	client = SQSClientExtended(
		"test",
		"test",
		"us-east-1",
		config=ExtendedClientConfiguration(**config_kwargs),
	)
	try:
		client.s3.create_bucket(Bucket=S3_BUCKET)
	except ClientError:
		pass
	return client


def _queue(client):
	name = "sqs-extended-test-" + uuid.uuid4().hex[:10]
	return client.create_queue(QueueName=name)["QueueUrl"]


def test_localstack_offload_round_trip_and_s3_cleanup():
	client = _client(always_through_s3=True)
	queue_url = _queue(client)
	payload = "localstack-" + uuid.uuid4().hex
	try:
		client.send_message(queue_url, payload)
		messages = client.receive_message(queue_url, max_number_of_messages=1, wait_time_seconds=5)
		assert messages
		message = messages[0]
		assert message["Body"] == payload
		assert SQSExtendedClientConstants.S3_KEY_MARKER.value in message["ReceiptHandle"]
		s3_key = message["ReceiptHandle"].split(SQSExtendedClientConstants.S3_KEY_MARKER.value)[1]
		client.s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
		client.delete_message(queue_url, message["ReceiptHandle"])
		with pytest.raises(ClientError):
			client.s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
	finally:
		client.close()


def test_localstack_small_message_stays_inline():
	client = _client(always_through_s3=False)
	queue_url = _queue(client)
	try:
		client.send_message(queue_url, "hello-localstack")
		messages = client.receive_message(queue_url, max_number_of_messages=1, wait_time_seconds=5)
		assert messages[0]["Body"] == "hello-localstack"
		assert SQSExtendedClientConstants.S3_KEY_MARKER.value not in messages[0]["ReceiptHandle"]
		client.delete_message(queue_url, messages[0]["ReceiptHandle"])
	finally:
		client.close()
