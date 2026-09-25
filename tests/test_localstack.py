import os
import uuid
from io import BytesIO

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


def test_localstack_large_text_returns_str():
	client = _client(message_size_threshold=64)
	queue_url = _queue(client)
	payload = "text-" + ("x" * 80)
	try:
		client.send_message(queue_url, payload)
		messages = client.receive_message(queue_url, max_number_of_messages=1, wait_time_seconds=5)
		assert messages[0]["Body"] == payload
		assert isinstance(messages[0]["Body"], str)
		assert "_is_binary" not in messages[0]
		client.delete_message(queue_url, messages[0]["ReceiptHandle"])
	finally:
		client.close()


def test_localstack_bytes_round_trip():
	client = _client(always_through_s3=False)
	queue_url = _queue(client)
	payload = b"\x00\x01\x02"
	try:
		client.send_message(queue_url, payload)
		messages = client.receive_message(queue_url, max_number_of_messages=1, wait_time_seconds=5)
		assert messages[0]["Body"] == payload
		assert messages[0]["_is_binary"] is True
		assert SQSExtendedClientConstants.S3_KEY_MARKER.value not in messages[0]["ReceiptHandle"]
		client.delete_message(queue_url, messages[0]["ReceiptHandle"])
	finally:
		client.close()


def test_localstack_file_path_and_fileobj_round_trip(tmp_path):
	client = _client(always_through_s3=True, message_size_threshold=8)
	queue_url = _queue(client)
	path_payload = b"path-bytes-" + os.urandom(32)
	file_payload = b"file-bytes-" + os.urandom(32)
	payload_path = tmp_path / "video.bin"
	payload_path.write_bytes(path_payload)
	try:
		client.send_message(queue_url, str(payload_path))
		client.send_message(queue_url, BytesIO(file_payload))
		messages = client.receive_message(queue_url, max_number_of_messages=2, wait_time_seconds=5)
		assert messages and len(messages) == 2
		bodies = sorted(messages, key=lambda item: item["Body"][:4])
		assert {item["Body"] for item in bodies} == {path_payload, file_payload}
		for message in messages:
			assert isinstance(message["Body"], bytes)
			assert message["_is_binary"] is True
			client.delete_message(queue_url, message["ReceiptHandle"])
	finally:
		client.close()


def test_localstack_multipart_upload_round_trip():
	client = _client(always_through_s3=True, multipart_threshold=1024, message_size_threshold=256)
	queue_url = _queue(client)
	multipart_calls = []
	client.s3.meta.events.register(
		"before-call.s3.CreateMultipartUpload",
		lambda **kwargs: multipart_calls.append(kwargs),
	)
	payload = os.urandom(16 * 1024)
	try:
		client.send_message(queue_url, payload)
		assert multipart_calls, "expected boto3 to start a multipart upload"
		messages = client.receive_message(queue_url, max_number_of_messages=1, wait_time_seconds=5)
		assert messages[0]["Body"] == payload
		assert isinstance(messages[0]["Body"], bytes)
		client.delete_message(queue_url, messages[0]["ReceiptHandle"])
	finally:
		client.close()
