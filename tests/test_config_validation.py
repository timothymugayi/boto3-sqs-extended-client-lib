import json
import logging

import boto3
import pytest

from pysqs_extended_client import sse_s3
from pysqs_extended_client.SQSClientExtended import SQSClientExtended
from pysqs_extended_client.extended_config import (
	DEFAULT_MESSAGE_SIZE_THRESHOLD,
	MAX_MESSAGE_SIZE_THRESHOLD,
	MIN_MESSAGE_SIZE_THRESHOLD,
	ExtendedClientConfiguration,
)
from pysqs_extended_client.server_side_encryption import ServerSideEncryptionStrategy
from tests.fakes import PAYLOAD_S3_POINTER_CLASS, FakeS3, FakeSqs


def _injected(**config_kwargs):
	config_kwargs.setdefault("s3_bucket_name", "test-bucket")
	return SQSClientExtended(
		sqs_client=FakeSqs(),
		s3_client=FakeS3(),
		config=ExtendedClientConfiguration(**config_kwargs),
	)


def _capture_boto3(monkeypatch):
	created = []

	def fake_client(service, *args, **kwargs):
		created.append((service, kwargs))
		return FakeSqs() if service == "sqs" else FakeS3()

	monkeypatch.setattr(boto3, "client", fake_client)
	return created


def _pointer_message(bucket="test-bucket", key="missing-key", receipt="orig-handle"):
	pointer = json.dumps([
		PAYLOAD_S3_POINTER_CLASS,
		{"s3BucketName": bucket, "s3Key": key},
	])
	return {
		"Body": pointer,
		"ReceiptHandle": receipt,
		"MessageAttributes": {
			"SQSLargePayloadSize": {"StringValue": "5", "DataType": "Number"},
		},
	}


def test_message_size_threshold_accepts_documented_range():
	assert MIN_MESSAGE_SIZE_THRESHOLD == 1
	assert MAX_MESSAGE_SIZE_THRESHOLD == 262144
	assert ExtendedClientConfiguration().message_size_threshold == DEFAULT_MESSAGE_SIZE_THRESHOLD
	assert ExtendedClientConfiguration(message_size_threshold=None).message_size_threshold == 262144
	assert ExtendedClientConfiguration(message_size_threshold=1).message_size_threshold == 1
	assert ExtendedClientConfiguration(message_size_threshold=262144).message_size_threshold == 262144

	config = ExtendedClientConfiguration(s3_bucket_name="test-bucket", message_size_threshold=10)
	config.message_size_threshold = 100
	assert config.message_size_threshold == 100


@pytest.mark.parametrize("value", [0, -1, 262145, 10 ** 7])
def test_message_size_threshold_rejects_out_of_range(value):
	with pytest.raises(ValueError, match="message_size_threshold"):
		ExtendedClientConfiguration(message_size_threshold=value)
	config = ExtendedClientConfiguration(message_size_threshold=8)
	with pytest.raises(ValueError, match="message_size_threshold"):
		config.message_size_threshold = value
	assert config.message_size_threshold == 8


@pytest.mark.parametrize("value", [True, False, 1.5, "256"])
def test_message_size_threshold_rejects_non_ints(value):
	with pytest.raises(TypeError, match="message_size_threshold"):
		ExtendedClientConfiguration(message_size_threshold=value)


def test_s3_bucket_name_strips_and_rejects_blank_or_non_str():
	config = ExtendedClientConfiguration(s3_bucket_name="  payload-bucket  ")
	assert config.s3_bucket_name == "payload-bucket"
	config.s3_bucket_name = None
	assert config.s3_bucket_name is None

	with pytest.raises(ValueError, match="cannot be blank"):
		ExtendedClientConfiguration(s3_bucket_name="   ")
	with pytest.raises(ValueError, match="cannot be blank"):
		config.s3_bucket_name = "\t"
	assert config.s3_bucket_name is None

	with pytest.raises(TypeError, match="s3_bucket_name"):
		ExtendedClientConfiguration(s3_bucket_name=123)


def test_server_side_encryption_must_be_a_strategy():
	config = ExtendedClientConfiguration(s3_bucket_name="test-bucket", server_side_encryption=None)
	assert config.server_side_encryption is None
	config.server_side_encryption = sse_s3()
	assert config.server_side_encryption.extra_args()["ServerSideEncryption"] == "AES256"

	class Custom(ServerSideEncryptionStrategy):
		def extra_args(self):
			return {"ServerSideEncryption": "aws:kms"}

	config.server_side_encryption = Custom()
	config.server_side_encryption = None

	with pytest.raises(TypeError, match="ServerSideEncryptionStrategy"):
		ExtendedClientConfiguration(s3_bucket_name="test-bucket", server_side_encryption="AES256")
	with pytest.raises(TypeError, match="ServerSideEncryptionStrategy"):
		ExtendedClientConfiguration(server_side_encryption=object())
	with pytest.raises(TypeError, match="ServerSideEncryptionStrategy"):
		config.server_side_encryption = {"ServerSideEncryption": "AES256"}
	assert config.server_side_encryption is None


def test_client_requires_bucket_when_payload_support_can_offload(monkeypatch):
	def boom(*args, **kwargs):
		raise AssertionError("boto3.client should not be called when bucket validation fails")

	monkeypatch.setattr(boto3, "client", boom)

	with pytest.raises(ValueError, match="payload_support_enabled=True"):
		SQSClientExtended()
	with pytest.raises(ValueError, match="always_through_s3=True"):
		SQSClientExtended(
			sqs_client=FakeSqs(),
			s3_client=FakeS3(),
			config=ExtendedClientConfiguration(always_through_s3=True),
		)
	with pytest.raises(ValueError, match="cannot be blank"):
		SQSClientExtended(s3_bucket_name="   ", sqs_client=FakeSqs(), s3_client=FakeS3())


def test_payload_support_disabled_allows_missing_bucket_for_small_only_use():
	client = SQSClientExtended(
		sqs_client=FakeSqs(),
		s3_client=FakeS3(),
		config=ExtendedClientConfiguration(payload_support_enabled=False),
	)
	assert client.s3_bucket_name is None
	client.send_message("https://sqs.example/queue", "small")
	assert client.sqs.send_calls[0]["MessageBody"] == "small"

	always = SQSClientExtended(
		sqs_client=FakeSqs(),
		s3_client=FakeS3(),
		config=ExtendedClientConfiguration(
			payload_support_enabled=False,
			always_through_s3=True,
		),
	)
	always.send_message("https://sqs.example/queue", "still-inline")
	assert always.sqs.send_calls[0]["MessageBody"] == "still-inline"
	assert always.s3.put_calls == []


def test_constructor_bucket_fills_empty_config_and_matching_names_agree():
	config = ExtendedClientConfiguration(always_through_s3=True)
	client = SQSClientExtended(
		s3_bucket_name=" from-ctor ",
		config=config,
		sqs_client=FakeSqs(),
		s3_client=FakeS3(),
	)
	assert client.s3_bucket_name == "from-ctor"
	assert config.s3_bucket_name == "from-ctor"
	assert config.frozen is True

	same = ExtendedClientConfiguration(s3_bucket_name="test-bucket")
	client = SQSClientExtended(
		s3_bucket_name=" test-bucket ",
		config=same,
		sqs_client=FakeSqs(),
		s3_client=FakeS3(),
	)
	assert client.s3_bucket_name == "test-bucket"


def test_constructor_bucket_that_differs_from_config_raises(monkeypatch):
	def boom(*args, **kwargs):
		raise AssertionError("boto3.client should not be called when bucket names conflict")

	monkeypatch.setattr(boto3, "client", boom)
	config = ExtendedClientConfiguration(s3_bucket_name="config-bucket")
	with pytest.raises(ValueError, match="does not match"):
		SQSClientExtended(
			s3_bucket_name="ctor-bucket",
			config=config,
			sqs_client=FakeSqs(),
			s3_client=FakeS3(),
		)
	assert config.s3_bucket_name == "config-bucket"
	assert config.frozen is False


def test_half_specified_credentials_raise_and_are_not_passed_to_boto3(monkeypatch):
	def boom(*args, **kwargs):
		raise AssertionError("half-specified keys must not build a boto3 client")

	monkeypatch.setattr(boto3, "client", boom)
	with pytest.raises(ValueError, match="aws_access_key_id is set but aws_secret_access_key is missing"):
		SQSClientExtended(aws_access_key_id="key", s3_bucket_name="test-bucket")
	with pytest.raises(ValueError, match="aws_secret_access_key is set but aws_access_key_id is missing"):
		SQSClientExtended(
			aws_secret_access_key="secret",
			aws_region_name="us-east-1",
			sqs_client=FakeSqs(),
			s3_client=FakeS3(),
			config=ExtendedClientConfiguration(s3_bucket_name="test-bucket"),
		)
	with pytest.raises(ValueError, match="aws_secret_access_key is set"):
		SQSClientExtended(
			aws_access_key_id="   ",
			aws_secret_access_key="secret",
			sqs_client=FakeSqs(),
			s3_client=FakeS3(),
			config=ExtendedClientConfiguration(payload_support_enabled=False),
		)
	with pytest.raises(TypeError, match="must be str or None"):
		SQSClientExtended(
			aws_access_key_id=b"key",
			aws_secret_access_key="secret",
			sqs_client=FakeSqs(),
			s3_client=FakeS3(),
			config=ExtendedClientConfiguration(s3_bucket_name="test-bucket"),
		)
	with pytest.raises(TypeError, match="must be str or None"):
		SQSClientExtended(aws_region_name=1, s3_bucket_name="test-bucket")


def test_complete_keys_are_passed_without_region_and_blank_keys_are_omitted(monkeypatch):
	created = _capture_boto3(monkeypatch)

	SQSClientExtended(aws_access_key_id=" key ", aws_secret_access_key=" secret ", s3_bucket_name="test-bucket")
	assert created[0][1]["aws_access_key_id"] == "key"
	assert created[0][1]["aws_secret_access_key"] == "secret"
	assert "region_name" not in created[0][1]
	assert created[1][1]["aws_access_key_id"] == "key"

	created.clear()
	SQSClientExtended(
		aws_access_key_id="  ",
		aws_secret_access_key="",
		aws_region_name=" eu-west-1 ",
		s3_bucket_name="test-bucket",
	)
	assert "aws_access_key_id" not in created[0][1]
	assert "aws_secret_access_key" not in created[0][1]
	assert created[0][1]["region_name"] == "eu-west-1"

	created.clear()
	SQSClientExtended(
		aws_access_key_id="key",
		aws_secret_access_key="secret",
		aws_region_name="   ",
		s3_bucket_name="test-bucket",
	)
	assert created[0][1]["aws_access_key_id"] == "key"
	assert "region_name" not in created[0][1]


def test_delete_s3_before_sqs_logs_one_warning_at_init(caplog):
	with caplog.at_level(logging.WARNING):
		client = _injected(delete_s3_before_sqs=True)
		client.delete_message("https://sqs.example/queue", "plain-handle")
	warnings = [
		record for record in caplog.records
		if record.levelno == logging.WARNING and "dead pointer" in record.getMessage()
	]
	assert len(warnings) == 1
	assert "delete_s3_before_sqs=True" in warnings[0].getMessage()

	caplog.clear()
	with caplog.at_level(logging.WARNING):
		_injected(delete_s3_before_sqs=False)
	assert not any("dead pointer" in record.getMessage() for record in caplog.records)


def test_ignore_payload_not_found_emits_payload_missing_deleted(caplog):
	events = []
	client = _injected(
		ignore_payload_not_found=True,
		on_event=lambda name, **fields: events.append((name, fields)),
	)
	client.sqs.receive_response = {"Messages": [_pointer_message()]}
	with caplog.at_level(logging.WARNING):
		assert client.receive_message("https://sqs.example/queue") == []
	assert client.sqs.delete_calls[0]["ReceiptHandle"] == "orig-handle"
	assert "could not be found in S3" in caplog.text
	assert events == [(
		"payload_missing_deleted",
		{
			"queue_url": "https://sqs.example/queue",
			"s3_bucket_name": "test-bucket",
			"s3_key": "missing-key",
			"receipt_handle": "orig-handle",
		},
	)]


def test_ignore_payload_not_found_emits_when_hydrate_returns_none():
	events = []
	client = _injected(
		ignore_payload_not_found=True,
		on_event=lambda name, **fields: events.append((name, fields)),
	)
	client._hydrate_from_s3 = lambda *args, **kwargs: None
	client.sqs.receive_response = {
		"Messages": [_pointer_message(key="empty-body", receipt="receipt-2")]
	}
	assert client.receive_message("https://sqs.example/queue") == []
	assert client.sqs.delete_calls[0]["ReceiptHandle"] == "receipt-2"
	assert events[0][0] == "payload_missing_deleted"
	assert events[0][1]["s3_key"] == "empty-body"
	assert events[0][1]["receipt_handle"] == "receipt-2"
	client.close()
