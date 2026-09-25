import json
import logging

import boto3
import pytest

from pysqs_extended_client.SQSClientExtended import SQSClientExtended, SQSExtendedClientConstants
from pysqs_extended_client.extended_config import (
	ExtendedClientConfiguration,
	FrozenConfigError,
	MAX_S3_KEY_PREFIX_LENGTH,
)
from tests.fakes import (
	EMBEDDED_HANDLE,
	FakeS3,
	FakeSqs,
	LEGACY_PAYLOAD_S3_POINTER_CLASS,
	PAYLOAD_S3_POINTER_CLASS,
	make_client,
)


def _client(**config_kwargs):
	return make_client(**config_kwargs)


def test_compat_constructor_builds_sqs_and_s3_clients(monkeypatch):
	created = []

	def fake_client(service, *args, **kwargs):
		created.append((service, kwargs.get("config")))
		return FakeSqs() if service == "sqs" else FakeS3()

	monkeypatch.setattr(boto3, "client", fake_client)
	client = SQSClientExtended("key", "secret", "us-east-1", "test-bucket")
	assert [item[0] for item in created] == ["sqs", "s3"]
	assert created[0][1].max_pool_connections == 50
	assert created[0][1].connect_timeout == 3
	assert created[0][1].read_timeout == 60
	assert client.s3_bucket_name == "test-bucket"
	assert client.always_through_s3 is False
	assert client.config.frozen is True


def test_built_clients_use_endpoint_url(monkeypatch):
	created = []

	def fake_client(service, *args, **kwargs):
		created.append(kwargs)
		return FakeSqs() if service == "sqs" else FakeS3()

	monkeypatch.setattr(boto3, "client", fake_client)
	SQSClientExtended(
		"key",
		"secret",
		"us-east-1",
		"test-bucket",
		config=ExtendedClientConfiguration(
			s3_bucket_name="test-bucket",
			endpoint_url="http://127.0.0.1:4566",
		),
	)
	assert created[0]["endpoint_url"] == "http://127.0.0.1:4566"
	assert created[1]["endpoint_url"] == "http://127.0.0.1:4566"
	assert created[0]["config"].s3["addressing_style"] == "path"


def test_injected_clients_are_not_rebuilt(monkeypatch):
	def boom(*args, **kwargs):
		raise AssertionError("injected clients must not call boto3.client")

	monkeypatch.setattr(boto3, "client", boom)
	mock_sqs = FakeSqs()
	mock_s3 = FakeS3()
	client = SQSClientExtended(
		sqs_client=mock_sqs,
		s3_client=mock_s3,
		config=ExtendedClientConfiguration(s3_bucket_name="test-bucket"),
	)
	assert client.sqs is mock_sqs
	assert client.s3 is mock_s3


def test_config_is_frozen_after_client_init():
	client, _, _ = _client()
	with pytest.raises(FrozenConfigError):
		client.set_always_through_s3(True)
	with pytest.raises(FrozenConfigError):
		client.config.s3_bucket_name = "other"


def test_is_large_payload_support_enabled_returns_true():
	client, _, _ = _client()
	assert client.is_large_payload_support_enabled() is True


def test_small_message_is_sent_inline_without_s3_pointer():
	client, mock_sqs, mock_s3 = _client()
	client.send_message("https://sqs.example/queue", "hello")
	assert mock_sqs.send_calls[0]["MessageBody"] == "hello"
	assert "SQSLargePayloadSize" not in mock_sqs.send_calls[0].get("MessageAttributes", {})
	assert mock_s3.put_calls == []


def test_offloaded_send_puts_bytes_and_emits_java_pointer():
	client, mock_sqs, mock_s3 = _client(always_through_s3=True)
	client.send_message("https://sqs.example/queue", "hello")

	assert len(mock_s3.put_calls) == 1
	assert mock_s3.put_calls[0]["Bucket"] == "test-bucket"
	assert mock_s3.put_calls[0]["Body"] == b"hello"
	body = json.loads(mock_sqs.send_calls[0]["MessageBody"])
	assert body[0] == PAYLOAD_S3_POINTER_CLASS
	assert body[1]["s3BucketName"] == "test-bucket"
	assert body[1]["s3Key"] == mock_s3.put_calls[0]["Key"]
	assert mock_sqs.send_calls[0]["MessageAttributes"]["SQSLargePayloadSize"]["DataType"] == "Number"


def test_too_many_attributes_raises_readable_error():
	client, _, _ = _client()
	attrs = {
		"attr{}".format(i): {"DataType": "String", "StringValue": "x"}
		for i in range(SQSExtendedClientConstants.MAX_ALLOWED_ATTRIBUTES.value + 1)
	}
	with pytest.raises(ValueError) as exc:
		client.send_message("https://sqs.example/queue", "hello", message_attributes=attrs)
	assert "Number of message attributes [10]" in str(exc.value)
	assert "9" in str(exc.value)


def test_receive_hydrates_extended_payload_size_and_java_pointer():
	client, mock_sqs, mock_s3 = _client()
	mock_s3.objects[("test-bucket", "abc-key")] = b"original-body"
	pointer = json.dumps([
		PAYLOAD_S3_POINTER_CLASS,
		{"s3BucketName": "test-bucket", "s3Key": "abc-key"},
	])
	mock_sqs.receive_response = {
		"Messages": [{
			"Body": pointer,
			"ReceiptHandle": "orig-handle",
			"MessageAttributes": {
				"ExtendedPayloadSize": {"StringValue": "5", "DataType": "Number"},
			},
		}]
	}
	messages = client.receive_message("https://sqs.example/queue")
	assert messages[0]["Body"] == "original-body"
	assert "ExtendedPayloadSize" not in messages[0]["MessageAttributes"]
	assert messages[0]["ReceiptHandle"].startswith("-..s3BucketName..-test-bucket-")
	assert messages[0]["ReceiptHandle"].endswith("orig-handle")
	assert mock_s3.get_calls[0]["Key"] == "abc-key"


def test_receive_hydrates_legacy_message_s3_pointer_class():
	client, mock_sqs, mock_s3 = _client()
	mock_s3.objects[("test-bucket", "abc-key")] = b"legacy-body"
	pointer = json.dumps([
		LEGACY_PAYLOAD_S3_POINTER_CLASS,
		{"s3BucketName": "test-bucket", "s3Key": "abc-key"},
	])
	mock_sqs.receive_response = {
		"Messages": [{
			"Body": pointer,
			"ReceiptHandle": "orig-handle",
			"MessageAttributes": {
				"SQSLargePayloadSize": {"StringValue": "5", "DataType": "Number"},
			},
		}]
	}
	messages = client.receive_message("https://sqs.example/queue")
	assert messages[0]["Body"] == "legacy-body"


def test_change_message_visibility_strips_s3_receipt_handle():
	client, mock_sqs, mock_s3 = _client()
	client.change_message_visibility("https://sqs.example/queue", EMBEDDED_HANDLE, 60)
	assert mock_sqs.visibility_calls[0]["ReceiptHandle"] == "orig-handle"
	assert mock_sqs.visibility_calls[0]["VisibilityTimeout"] == 60
	assert mock_s3.delete_calls == []


def test_change_message_visibility_batch_strips_each_handle():
	client, mock_sqs, _ = _client()
	client.change_message_visibility_batch(
		"https://sqs.example/queue",
		[
			{"Id": "1", "ReceiptHandle": EMBEDDED_HANDLE, "VisibilityTimeout": 30},
			{"Id": "2", "ReceiptHandle": "plain-handle", "VisibilityTimeout": 10},
		],
	)
	entries = mock_sqs.visibility_batch_calls[0]["Entries"]
	assert entries[0]["ReceiptHandle"] == "orig-handle"
	assert entries[1]["ReceiptHandle"] == "plain-handle"


def test_delete_message_batch_deletes_s3_and_strips_handles():
	client, mock_sqs, mock_s3 = _client()
	mock_s3.objects[("test-bucket", "abc-key")] = b"payload"
	client.delete_message_batch(
		"https://sqs.example/queue",
		[{"Id": "1", "ReceiptHandle": EMBEDDED_HANDLE}],
	)
	assert mock_s3.delete_calls[0]["Bucket"] == "test-bucket"
	assert mock_s3.delete_calls[0]["Key"] == "abc-key"
	assert mock_sqs.delete_batch_calls[0]["Entries"][0]["ReceiptHandle"] == "orig-handle"


def test_send_message_batch_offloads_only_large_or_always_s3_entries():
	client, mock_sqs, mock_s3 = _client(always_through_s3=False)
	client.send_message_batch(
		"https://sqs.example/queue",
		[
			{"Id": "small", "MessageBody": "hello"},
			{"Id": "large", "MessageBody": "x" * (SQSExtendedClientConstants.DEFAULT_MESSAGE_SIZE_THRESHOLD.value + 1)},
		],
	)
	entries = mock_sqs.send_batch_calls[0]["Entries"]
	assert entries[0]["MessageBody"] == "hello"
	assert "SQSLargePayloadSize" not in entries[0].get("MessageAttributes", {})
	large_body = json.loads(entries[1]["MessageBody"])
	assert large_body[0] == PAYLOAD_S3_POINTER_CLASS
	assert "SQSLargePayloadSize" in entries[1]["MessageAttributes"]
	assert len(mock_s3.put_calls) == 1


def test_purge_queue_warns_that_s3_payloads_leak(caplog):
	client, mock_sqs, _ = _client()
	with caplog.at_level(logging.WARNING):
		client.purge_queue("https://sqs.example/queue")
	assert mock_sqs.purge_calls[0]["QueueUrl"] == "https://sqs.example/queue"
	assert "without deleting their payload from S3" in caplog.text


def test_s3_key_prefix_validation():
	with pytest.raises(ValueError, match="must not start"):
		ExtendedClientConfiguration(s3_key_prefix=".hidden")
	with pytest.raises(ValueError, match="must not start"):
		ExtendedClientConfiguration(s3_key_prefix="/abs")
	with pytest.raises(ValueError, match="must not contain"):
		ExtendedClientConfiguration(s3_key_prefix="a../b")
	with pytest.raises(ValueError, match="invalid characters"):
		ExtendedClientConfiguration(s3_key_prefix="bad prefix")
	with pytest.raises(ValueError, match="must not be greater than"):
		ExtendedClientConfiguration(s3_key_prefix="a" * (MAX_S3_KEY_PREFIX_LENGTH + 1))
	config = ExtendedClientConfiguration(s3_key_prefix=" payloads/v1/")
	assert config.s3_key_prefix == "payloads/v1/"


def test_offloaded_send_uses_extended_attribute_when_legacy_disabled():
	client, mock_sqs, _ = _client(always_through_s3=True, use_legacy_attribute=False)
	client.send_message("https://sqs.example/queue", "hello")
	attrs = mock_sqs.send_calls[0]["MessageAttributes"]
	assert "ExtendedPayloadSize" in attrs
	assert "SQSLargePayloadSize" not in attrs


def test_offloaded_send_uses_s3_key_prefix():
	client, _, mock_s3 = _client(always_through_s3=True, s3_key_prefix="inbox/")
	client.send_message("https://sqs.example/queue", "hello")
	assert mock_s3.put_calls[0]["Key"].startswith("inbox/")


def test_offloaded_send_omits_acl_extra_args_by_default():
	client, _, mock_s3 = _client(always_through_s3=True)
	client.send_message("https://sqs.example/queue", "hello")
	assert "ExtraArgs" not in mock_s3.put_calls[0]


def test_offloaded_send_passes_s3_canned_acl():
	client, _, mock_s3 = _client(always_through_s3=True, s3_canned_acl="bucket-owner-read")
	client.send_message("https://sqs.example/queue", "hello")
	assert mock_s3.put_calls[0]["ExtraArgs"] == {"ACL": "bucket-owner-read"}


def test_delete_skips_s3_when_cleanup_disabled():
	client, mock_sqs, mock_s3 = _client(cleanup_s3_payload=False)
	mock_s3.objects[("test-bucket", "abc-key")] = b"payload"
	client.delete_message("https://sqs.example/queue", EMBEDDED_HANDLE)
	assert mock_s3.delete_calls == []
	assert mock_sqs.delete_calls[0]["ReceiptHandle"] == "orig-handle"


def test_receive_drops_message_when_payload_missing_and_ignore_enabled():
	client, mock_sqs, _ = _client(ignore_payload_not_found=True)
	pointer = json.dumps([
		PAYLOAD_S3_POINTER_CLASS,
		{"s3BucketName": "test-bucket", "s3Key": "missing-key"},
	])
	mock_sqs.receive_response = {
		"Messages": [{
			"Body": pointer,
			"ReceiptHandle": "orig-handle",
			"MessageAttributes": {
				"SQSLargePayloadSize": {"StringValue": "5", "DataType": "Number"},
			},
		}]
	}
	messages = client.receive_message("https://sqs.example/queue")
	assert messages == []
	assert mock_sqs.delete_calls[0]["ReceiptHandle"] == "orig-handle"


def test_send_forwards_delay_seconds():
	client, mock_sqs, _ = _client()
	client.send_message("https://sqs.example/queue", "hello", DelaySeconds=12)
	assert mock_sqs.send_calls[0]["DelaySeconds"] == 12


def test_receive_forwards_visibility_timeout_and_reserved_attribute_names():
	client, mock_sqs, _ = _client()
	client.receive_message(
		"https://sqs.example/queue",
		VisibilityTimeout=15,
		MessageAttributeNames=["CustomAttr"],
	)
	call = mock_sqs.receive_calls[0]
	assert call["VisibilityTimeout"] == 15
	assert "CustomAttr" in call["MessageAttributeNames"]
	assert "SQSLargePayloadSize" in call["MessageAttributeNames"]
	assert "ExtendedPayloadSize" in call["MessageAttributeNames"]
	assert "SQSExtendedContentType" in call["MessageAttributeNames"]


def test_send_message_does_not_reuse_mutable_default_attributes():
	client, mock_sqs, _ = _client(always_through_s3=True)
	client.send_message("https://sqs.example/queue", "first")
	client.send_message("https://sqs.example/queue", "second")
	assert mock_sqs.send_calls[0]["MessageAttributes"] is not mock_sqs.send_calls[1]["MessageAttributes"]


def test_getattr_delegates_queue_apis():
	client, mock_sqs, _ = _client()
	assert client.create_queue(QueueName="Demo")["QueueUrl"].endswith("Demo")
	assert mock_sqs.create_queue_calls[0]["QueueName"] == "Demo"
	assert client.get_queue_url(QueueName="Demo")["QueueUrl"].endswith("Demo")
