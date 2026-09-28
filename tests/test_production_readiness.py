import json
import logging
import pathlib

import boto3
import pytest

from pysqs_extended_client import aws_managed_cmk, customer_key, sse_s3
from pysqs_extended_client.SQSClientExtended import SQSClientExtended
from pysqs_extended_client.extended_config import ExtendedClientConfiguration
from tests.fakes import EMBEDDED_HANDLE, PAYLOAD_S3_POINTER_CLASS, FakeS3, FakeSqs, make_client


def test_binary_attribute_bytes_do_not_crash_size_calculation():
	client, mock_sqs, _ = make_client()
	blob = b"\x00\x01\xff"
	client.send_message(
		"https://sqs.example/queue",
		"hello",
		message_attributes={"blob": {"DataType": "Binary", "BinaryValue": blob}},
	)
	assert mock_sqs.send_calls[0]["MessageAttributes"]["blob"]["BinaryValue"] == blob


def test_binary_attribute_bytearray_is_sized_as_raw_bytes():
	client, _, _ = make_client(message_size_threshold=10)
	attrs = {"blob": {"DataType": "Binary", "BinaryValue": bytearray(b"x" * 20)}}
	with pytest.raises(ValueError, match="Message attributes"):
		client.send_message("https://sqs.example/queue", "hi", message_attributes=attrs)


def test_existing_path_string_is_text_not_a_file_upload(tmp_path):
	payload_path = tmp_path / "note.txt"
	payload_path.write_bytes(b"file-contents-not-the-body")
	client, mock_sqs, mock_s3 = make_client(always_through_s3=True)
	client.send_message("https://sqs.example/queue", str(payload_path))
	assert mock_s3.put_calls[0]["UploadMethod"] == "upload_fileobj"
	assert mock_s3.put_calls[0]["Body"] == str(payload_path).encode("utf-8")
	assert "SQSExtendedContentType" not in mock_sqs.send_calls[0]["MessageAttributes"]


def test_send_file_and_path_object_upload_the_file(tmp_path):
	payload_path = tmp_path / "video.bin"
	payload_path.write_bytes(b"\xff" * 32)
	client, mock_sqs, mock_s3 = make_client(message_size_threshold=8)
	client.send_file("https://sqs.example/queue", str(payload_path))
	assert mock_s3.put_calls[0]["UploadMethod"] == "upload_file"
	assert mock_s3.put_calls[0]["Body"] == b"\xff" * 32
	assert mock_sqs.send_calls[0]["MessageAttributes"]["SQSExtendedContentType"]["StringValue"] == "binary"

	client.send_message("https://sqs.example/queue", pathlib.Path(payload_path))
	assert mock_s3.put_calls[1]["UploadMethod"] == "upload_file"


def test_send_file_missing_path_raises(tmp_path):
	client, _, mock_s3 = make_client()
	with pytest.raises(ValueError, match="does not exist"):
		client.send_file("https://sqs.example/queue", tmp_path / "missing.bin")
	assert mock_s3.put_calls == []


def test_batch_string_path_stays_text_and_path_object_uploads(tmp_path):
	payload_path = tmp_path / "clip.bin"
	payload_path.write_bytes(b"\x01" * 16)
	client, mock_sqs, mock_s3 = make_client(always_through_s3=True)
	client.send_message_batch(
		"https://sqs.example/queue",
		[
			{"Id": "text", "MessageBody": str(payload_path)},
			{"Id": "file", "MessageBody": payload_path},
		],
	)
	bodies = {call["Body"] for call in mock_s3.put_calls}
	assert str(payload_path).encode("utf-8") in bodies
	assert b"\x01" * 16 in bodies
	methods = {call["UploadMethod"] for call in mock_s3.put_calls}
	assert methods == {"upload_file", "upload_fileobj"}
	entries = {entry["Id"]: entry for entry in mock_sqs.send_batch_calls[0]["Entries"]}
	assert "SQSExtendedContentType" not in entries["text"]["MessageAttributes"]
	assert entries["file"]["MessageAttributes"]["SQSExtendedContentType"]["StringValue"] == "binary"


def test_receive_empty_queue_returns_empty_list():
	client, mock_sqs, _ = make_client()
	mock_sqs.receive_response = {}
	assert client.receive_message("https://sqs.example/queue") == []
	mock_sqs.receive_response = {"Messages": []}
	assert client.receive_message("https://sqs.example/queue") == []
	mock_sqs.receive_response = {"Messages": None}
	assert client.receive_message("https://sqs.example/queue") == []


def test_sqs_delete_failure_leaves_s3_object():
	class FailingSqs(FakeSqs):
		def delete_message(self, **kwargs):
			self.delete_calls.append(kwargs)
			raise RuntimeError("sqs down")

	mock_sqs = FailingSqs()
	mock_s3 = FakeS3()
	mock_s3.objects[("test-bucket", "abc-key")] = b"payload"
	client = SQSClientExtended(
		sqs_client=mock_sqs,
		s3_client=mock_s3,
		config=ExtendedClientConfiguration(s3_bucket_name="test-bucket"),
	)
	with pytest.raises(RuntimeError, match="sqs down"):
		client.delete_message("https://sqs.example/queue", EMBEDDED_HANDLE)
	assert mock_s3.delete_calls == []
	assert mock_s3.objects[("test-bucket", "abc-key")] == b"payload"
	assert mock_sqs.delete_calls[0]["ReceiptHandle"] == "orig-handle"


def test_s3_cleanup_failure_after_sqs_delete_is_best_effort(caplog):
	class FailingDeleteS3(FakeS3):
		def delete_object(self, **kwargs):
			self.delete_calls.append(kwargs)
			raise RuntimeError("s3 down")

	mock_sqs = FakeSqs()
	mock_s3 = FailingDeleteS3()
	client = SQSClientExtended(
		sqs_client=mock_sqs,
		s3_client=mock_s3,
		config=ExtendedClientConfiguration(s3_bucket_name="test-bucket"),
	)
	with caplog.at_level(logging.WARNING):
		result = client.delete_message("https://sqs.example/queue", EMBEDDED_HANDLE)
	assert result == {}
	assert mock_sqs.delete_calls[0]["ReceiptHandle"] == "orig-handle"
	assert "orphaned" in caplog.text


def test_default_delete_order_is_sqs_then_s3():
	order = []

	class OrderingSqs(FakeSqs):
		def delete_message(self, **kwargs):
			order.append("sqs")
			return super().delete_message(**kwargs)

	class OrderingS3(FakeS3):
		def delete_object(self, **kwargs):
			order.append("s3")
			return super().delete_object(**kwargs)

	client = SQSClientExtended(
		sqs_client=OrderingSqs(),
		s3_client=OrderingS3(),
		config=ExtendedClientConfiguration(s3_bucket_name="test-bucket"),
	)
	client.delete_message("https://sqs.example/queue", EMBEDDED_HANDLE)
	assert order == ["sqs", "s3"]


def test_s3_before_sqs_logs_dead_pointer_when_queue_delete_fails(caplog):
	class FailingSqs(FakeSqs):
		def delete_message(self, **kwargs):
			self.delete_calls.append(kwargs)
			raise RuntimeError("sqs down")

	mock_sqs = FailingSqs()
	mock_s3 = FakeS3()
	mock_s3.objects[("test-bucket", "abc-key")] = b"payload"
	client = SQSClientExtended(
		sqs_client=mock_sqs,
		s3_client=mock_s3,
		config=ExtendedClientConfiguration(
			s3_bucket_name="test-bucket",
			delete_s3_before_sqs=True,
		),
	)
	with caplog.at_level(logging.ERROR):
		with pytest.raises(RuntimeError, match="sqs down"):
			client.delete_message("https://sqs.example/queue", EMBEDDED_HANDLE)
	assert ("test-bucket", "abc-key") not in mock_s3.objects
	assert "dead pointer" in caplog.text
	assert mock_sqs.delete_calls[0]["ReceiptHandle"] == "orig-handle"


def test_delete_batch_keeps_s3_object_when_sqs_entry_fails():
	class PartialSqs(FakeSqs):
		def delete_message_batch(self, **kwargs):
			self.delete_batch_calls.append(kwargs)
			return {
				"Successful": [{"Id": "ok"}],
				"Failed": [{"Id": "bad", "Code": "ReceiptHandleIsInvalid", "SenderFault": True}],
			}

	mock_sqs = PartialSqs()
	mock_s3 = FakeS3()
	mock_s3.objects[("test-bucket", "ok-key")] = b"ok"
	mock_s3.objects[("test-bucket", "bad-key")] = b"bad"
	client = SQSClientExtended(
		sqs_client=mock_sqs,
		s3_client=mock_s3,
		config=ExtendedClientConfiguration(s3_bucket_name="test-bucket"),
	)

	def handle(key):
		return (
			"-..s3BucketName..-test-bucket-..s3BucketName..-"
			"-..s3Key..-{}-..s3Key..-orig".format(key)
		)

	client.delete_message_batch(
		"https://sqs.example/queue",
		[
			{"Id": "ok", "ReceiptHandle": handle("ok-key")},
			{"Id": "bad", "ReceiptHandle": handle("bad-key")},
		],
	)
	deleted = {call["Key"] for call in mock_s3.delete_calls}
	assert deleted == {"ok-key"}
	assert mock_s3.objects[("test-bucket", "bad-key")] == b"bad"


def test_send_failure_deletes_uploaded_payload():
	class FailingSqs(FakeSqs):
		def send_message(self, **kwargs):
			self.send_calls.append(kwargs)
			raise RuntimeError("sqs down")

	mock_sqs = FailingSqs()
	mock_s3 = FakeS3()
	client = SQSClientExtended(
		sqs_client=mock_sqs,
		s3_client=mock_s3,
		config=ExtendedClientConfiguration(s3_bucket_name="test-bucket", always_through_s3=True),
	)
	with pytest.raises(RuntimeError, match="sqs down"):
		client.send_message("https://sqs.example/queue", "hello")
	assert mock_s3.objects == {}
	assert len(mock_s3.delete_calls) == 1


def test_send_failure_logs_orphan_when_s3_cleanup_also_fails(caplog):
	class FailingSqs(FakeSqs):
		def send_message(self, **kwargs):
			self.send_calls.append(kwargs)
			raise RuntimeError("sqs down")

	class FailingDeleteS3(FakeS3):
		def delete_object(self, **kwargs):
			self.delete_calls.append(kwargs)
			raise RuntimeError("s3 down")

	client = SQSClientExtended(
		sqs_client=FailingSqs(),
		s3_client=FailingDeleteS3(),
		config=ExtendedClientConfiguration(s3_bucket_name="test-bucket", always_through_s3=True),
	)
	with caplog.at_level(logging.WARNING):
		with pytest.raises(RuntimeError, match="sqs down"):
			client.send_message("https://sqs.example/queue", "hello")
	assert "orphaned" in caplog.text
	assert "lifecycle" in caplog.text


def test_receive_payload_dir_streams_with_download_file(tmp_path):
	client, mock_sqs, mock_s3 = make_client()
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
				"SQSLargePayloadSize": {"StringValue": "13", "DataType": "Number"},
			},
		}]
	}
	messages = client.receive_message("https://sqs.example/queue", payload_dir=str(tmp_path))
	path = messages[0]["Body"]
	assert messages[0]["_payload_path"] == path
	assert isinstance(path, str)
	assert pathlib.Path(path).read_bytes() == b"original-body"
	assert mock_s3.get_calls[0]["DownloadMethod"] == "download_file"


def test_in_memory_receive_uses_download_fileobj():
	client, mock_sqs, mock_s3 = make_client()
	mock_s3.objects[("test-bucket", "abc-key")] = b"original-body"
	mock_sqs.receive_response = {
		"Messages": [{
			"Body": json.dumps([
				PAYLOAD_S3_POINTER_CLASS,
				{"s3BucketName": "test-bucket", "s3Key": "abc-key"},
			]),
			"ReceiptHandle": "orig-handle",
			"MessageAttributes": {
				"SQSLargePayloadSize": {"StringValue": "13", "DataType": "Number"},
			},
		}]
	}
	messages = client.receive_message("https://sqs.example/queue")
	assert messages[0]["Body"] == "original-body"
	assert mock_s3.get_calls[0]["DownloadMethod"] == "download_fileobj"


def test_sse_kms_customer_key_and_acl_are_upload_extra_args():
	client, _, mock_s3 = make_client(
		always_through_s3=True,
		s3_canned_acl="bucket-owner-read",
		server_side_encryption=customer_key("alias/payloads"),
	)
	client.send_message("https://sqs.example/queue", "hello")
	assert mock_s3.put_calls[0]["ExtraArgs"] == {
		"ACL": "bucket-owner-read",
		"ServerSideEncryption": "aws:kms",
		"SSEKMSKeyId": "alias/payloads",
	}


def test_aws_managed_cmk_and_sse_s3_extra_args():
	client, _, mock_s3 = make_client(always_through_s3=True, server_side_encryption=aws_managed_cmk())
	client.send_message("https://sqs.example/queue", "one")
	assert mock_s3.put_calls[0]["ExtraArgs"] == {"ServerSideEncryption": "aws:kms"}
	other, _, other_s3 = make_client(always_through_s3=True, server_side_encryption=sse_s3())
	other.send_message("https://sqs.example/queue", "two")
	assert other_s3.put_calls[0]["ExtraArgs"] == {"ServerSideEncryption": "AES256"}


def test_payload_support_disabled_skips_offload_hydrate_and_s3_delete():
	client, mock_sqs, mock_s3 = make_client(payload_support_enabled=False, always_through_s3=True)
	assert client.is_large_payload_support_enabled() is False
	client.send_message("https://sqs.example/queue", "x" * 300)
	assert mock_s3.put_calls == []
	assert mock_sqs.send_calls[0]["MessageBody"] == "x" * 300

	pointer = json.dumps([
		PAYLOAD_S3_POINTER_CLASS,
		{"s3BucketName": "test-bucket", "s3Key": "abc-key"},
	])
	mock_s3.objects[("test-bucket", "abc-key")] = b"original-body"
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
	assert messages[0]["Body"] == pointer
	assert mock_s3.get_calls == []

	client.delete_message("https://sqs.example/queue", EMBEDDED_HANDLE)
	assert mock_s3.delete_calls == []
	assert mock_sqs.delete_calls[0]["ReceiptHandle"] == EMBEDDED_HANDLE


def test_builtin_clients_do_not_retain_access_keys(monkeypatch):
	created = []

	def fake_client(service, *args, **kwargs):
		created.append(kwargs)
		return FakeSqs() if service == "sqs" else FakeS3()

	monkeypatch.setattr(boto3, "client", fake_client)
	client = SQSClientExtended("key", "secret", "us-east-1", "test-bucket")
	assert created[0]["aws_access_key_id"] == "key"
	assert created[0]["aws_secret_access_key"] == "secret"
	assert "aws_access_key_id" not in vars(client)
	assert "aws_secret_access_key" not in vars(client)

	created.clear()
	client = SQSClientExtended(s3_bucket_name="test-bucket")
	assert "aws_access_key_id" not in created[0]
	assert "aws_secret_access_key" not in created[0]
	assert "aws_access_key_id" not in vars(client)
