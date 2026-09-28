#!/usr/bin/env python3
"""Round-trip the extended client against a local LocalStack container.

Start LocalStack first:

    docker compose up -d

Then:

    python scripts/localstack_smoke.py
"""
import os
import sys
import uuid
from io import BytesIO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
	sys.path.insert(0, ROOT)

from botocore.exceptions import ClientError

from pysqs_extended_client import ExtendedClientConfiguration, SQSClientExtended
from pysqs_extended_client.SQSClientExtended import SQSExtendedClientConstants


ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://127.0.0.1:4566")
BUCKET = os.environ.get("SQS_EXTENDED_S3_BUCKET", "sqs-extended-payloads")


def _client(**config_kwargs):
	config_kwargs.setdefault("s3_bucket_name", BUCKET)
	config_kwargs.setdefault("endpoint_url", ENDPOINT)
	config_kwargs.setdefault("s3_key_prefix", "smoke/")
	client = SQSClientExtended(
		"test",
		"test",
		"us-east-1",
		config=ExtendedClientConfiguration(**config_kwargs),
	)
	try:
		client.s3.create_bucket(Bucket=BUCKET)
	except ClientError:
		pass
	return client


def _queue(client):
	name = "sqs-extended-smoke-" + uuid.uuid4().hex[:10]
	return client.create_queue(QueueName=name)["QueueUrl"]


def _receive_one(client, queue_url):
	messages = client.receive_message(queue_url, max_number_of_messages=1, wait_time_seconds=5)
	if not messages:
		raise RuntimeError("no message received from {}".format(queue_url))
	return messages[0]


def check_small_text():
	client = _client()
	queue_url = _queue(client)
	try:
		client.send_message(queue_url, "hello-localstack")
		message = _receive_one(client, queue_url)
		assert message["Body"] == "hello-localstack"
		assert isinstance(message["Body"], str)
		assert SQSExtendedClientConstants.S3_KEY_MARKER.value not in message["ReceiptHandle"]
		client.delete_message(queue_url, message["ReceiptHandle"])
	finally:
		client.close()
	print("ok  small text stays in SQS and returns str")


def check_large_text():
	client = _client(message_size_threshold=64)
	queue_url = _queue(client)
	payload = "text-" + ("x" * 80)
	try:
		client.send_message(queue_url, payload)
		message = _receive_one(client, queue_url)
		assert message["Body"] == payload
		assert isinstance(message["Body"], str)
		assert "_is_binary" not in message
		client.delete_message(queue_url, message["ReceiptHandle"])
	finally:
		client.close()
	print("ok  large text offloads to S3 and returns str")


def check_bytes():
	client = _client()
	queue_url = _queue(client)
	payload = b"\x00\x01\x02"
	try:
		client.send_message(queue_url, payload)
		message = _receive_one(client, queue_url)
		assert message["Body"] == payload
		assert message["_is_binary"] is True
		client.delete_message(queue_url, message["ReceiptHandle"])
	finally:
		client.close()
	print("ok  small bytes round-trip as bytes")


def check_file_path_and_fileobj():
	client = _client(always_through_s3=True)
	queue_url = _queue(client)
	path_payload = b"path-" + os.urandom(64)
	file_payload = b"file-" + os.urandom(64)
	path = os.path.join(ROOT, ".localstack-smoke.bin")
	try:
		with open(path, "wb") as handle:
			handle.write(path_payload)
		client.send_file(queue_url, path)
		client.send_message(queue_url, BytesIO(file_payload))
		messages = client.receive_message(queue_url, max_number_of_messages=2, wait_time_seconds=5)
		if not messages or len(messages) != 2:
			raise RuntimeError("expected 2 messages, got {}".format(messages))
		assert {item["Body"] for item in messages} == {path_payload, file_payload}
		for message in messages:
			assert isinstance(message["Body"], bytes)
			client.delete_message(queue_url, message["ReceiptHandle"])
	finally:
		client.close()
		if os.path.exists(path):
			os.remove(path)
	print("ok  send_file and file object round-trip as bytes")


def check_multipart():
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
		if not multipart_calls:
			raise RuntimeError("boto3 did not start a multipart upload")
		message = _receive_one(client, queue_url)
		assert message["Body"] == payload
		assert isinstance(message["Body"], bytes)
		client.delete_message(queue_url, message["ReceiptHandle"])
	finally:
		client.close()
	print("ok  multipart upload (threshold 1024, 16 KiB payload) round-trips")


def main():
	print("LocalStack endpoint {}".format(ENDPOINT))
	check_small_text()
	check_large_text()
	check_bytes()
	check_file_path_and_fileobj()
	check_multipart()
	print("all localstack smoke checks passed")


if __name__ == "__main__":
	try:
		main()
	except Exception as exc:
		print("FAIL {}".format(exc))
		raise
