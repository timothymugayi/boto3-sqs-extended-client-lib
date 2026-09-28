import base64
import json
from io import BytesIO

import pytest

from pysqs_extended_client.SQSClientExtended import SQSExtendedClientConstants
from tests.fakes import PAYLOAD_S3_POINTER_CLASS, make_client


def test_small_text_stays_a_string_without_content_type():
	client, mock_sqs, mock_s3 = make_client()
	client.send_message("https://sqs.example/queue", "hello")
	sent = mock_sqs.send_calls[0]
	assert sent["MessageBody"] == "hello"
	assert "SQSExtendedContentType" not in sent.get("MessageAttributes", {})
	assert mock_s3.put_calls == []


def test_offloaded_text_omits_content_type_and_receives_as_str():
	client, mock_sqs, mock_s3 = make_client(always_through_s3=True)
	client.send_message("https://sqs.example/queue", "hello")
	sent = mock_sqs.send_calls[0]
	assert "SQSExtendedContentType" not in sent["MessageAttributes"]
	assert sent["MessageAttributes"]["SQSLargePayloadSize"]["StringValue"] == "5"
	body = json.loads(sent["MessageBody"])
	assert body[0] == PAYLOAD_S3_POINTER_CLASS
	mock_sqs.receive_response = {"Messages": [{
		"Body": sent["MessageBody"],
		"ReceiptHandle": "h",
		"MessageAttributes": sent["MessageAttributes"],
	}]}
	messages = client.receive_message("https://sqs.example/queue")
	assert messages[0]["Body"] == "hello"
	assert "_is_binary" not in messages[0]


def test_small_bytes_round_trip_as_base64_in_sqs():
	client, mock_sqs, mock_s3 = make_client()
	payload = b"\x00\x01\x02"
	client.send_message("https://sqs.example/queue", payload)
	sent = mock_sqs.send_calls[0]
	assert base64.b64decode(sent["MessageBody"]) == payload
	assert sent["MessageAttributes"]["SQSExtendedContentType"]["StringValue"] == "binary-b64"
	assert mock_s3.put_calls == []
	mock_sqs.receive_response = {"Messages": [{
		"Body": sent["MessageBody"],
		"ReceiptHandle": "h",
		"MessageAttributes": dict(sent["MessageAttributes"]),
	}]}
	messages = client.receive_message("https://sqs.example/queue")
	assert messages[0]["Body"] == payload
	assert messages[0]["_is_binary"] is True
	assert "SQSExtendedContentType" not in messages[0]["MessageAttributes"]


def test_large_bytes_stream_to_s3_and_receive_as_bytes():
	client, mock_sqs, mock_s3 = make_client(message_size_threshold=4)
	payload = b"\x00\x01\x02\x03\x04"
	client.send_message("https://sqs.example/queue", payload)
	sent = mock_sqs.send_calls[0]
	assert mock_s3.put_calls[0]["UploadMethod"] == "upload_fileobj"
	assert mock_s3.put_calls[0]["Body"] == payload
	assert sent["MessageAttributes"]["SQSExtendedContentType"]["StringValue"] == "binary"
	assert sent["MessageAttributes"]["SQSLargePayloadSize"]["StringValue"] == "5"
	mock_sqs.receive_response = {"Messages": [{
		"Body": sent["MessageBody"],
		"ReceiptHandle": "h",
		"MessageAttributes": dict(sent["MessageAttributes"]),
	}]}
	messages = client.receive_message("https://sqs.example/queue")
	assert messages[0]["Body"] == payload
	assert isinstance(messages[0]["Body"], bytes)


def test_file_path_uses_upload_file(tmp_path):
	payload_path = tmp_path / "video.bin"
	payload_path.write_bytes(b"\xff" * 32)
	client, mock_sqs, mock_s3 = make_client(message_size_threshold=8)
	client.send_message("https://sqs.example/queue", payload_path)
	assert mock_s3.put_calls[0]["UploadMethod"] == "upload_file"
	assert mock_s3.put_calls[0]["Body"] == b"\xff" * 32
	sent = mock_sqs.send_calls[0]
	assert sent["MessageAttributes"]["SQSExtendedContentType"]["StringValue"] == "binary"
	assert json.loads(sent["MessageBody"])[0] == PAYLOAD_S3_POINTER_CLASS


def test_file_object_uses_upload_fileobj():
	handle = BytesIO(b"\x10\x20" * 20)
	client, _, mock_s3 = make_client(message_size_threshold=8)
	client.send_message("https://sqs.example/queue", handle)
	assert mock_s3.put_calls[0]["UploadMethod"] == "upload_fileobj"
	assert mock_s3.put_calls[0]["Body"] == b"\x10\x20" * 20


def test_non_seekable_file_is_forced_to_s3():
	class Reader:
		def read(self, size=-1):
			return b"abc"

	client, _, mock_s3 = make_client(message_size_threshold=1000)
	client.send_message("https://sqs.example/queue", Reader())
	assert mock_s3.put_calls[0]["Body"] == b"abc"
	assert mock_s3.put_calls[0]["UploadMethod"] == "upload_fileobj"


def test_small_file_stays_in_sqs_as_base64(tmp_path):
	payload_path = tmp_path / "tiny.bin"
	payload_path.write_bytes(b"xy")
	client, mock_sqs, mock_s3 = make_client()
	client.send_message("https://sqs.example/queue", payload_path)
	assert mock_s3.put_calls == []
	assert base64.b64decode(mock_sqs.send_calls[0]["MessageBody"]) == b"xy"


def test_unsupported_message_type_raises():
	client, _, _ = make_client()
	with pytest.raises(TypeError, match="file-like"):
		client.send_message("https://sqs.example/queue", 12)


def test_content_type_attribute_name_is_reserved():
	client, _, _ = make_client()
	attrs = {
		SQSExtendedClientConstants.CONTENT_TYPE_ATTRIBUTE.value: {
			"DataType": "String",
			"StringValue": "text",
		}
	}
	with pytest.raises(ValueError, match="SQSExtendedContentType"):
		client.send_message("https://sqs.example/queue", "hello", message_attributes=attrs)


def test_message_without_content_type_still_returns_str():
	client, mock_sqs, mock_s3 = make_client()
	mock_s3.objects[("test-bucket", "old")] = "legacy-text".encode("utf-8")
	mock_sqs.receive_response = {"Messages": [{
		"Body": json.dumps([
			PAYLOAD_S3_POINTER_CLASS,
			{"s3BucketName": "test-bucket", "s3Key": "old"},
		]),
		"ReceiptHandle": "h",
		"MessageAttributes": {
			"SQSLargePayloadSize": {"StringValue": "11", "DataType": "Number"},
		},
	}]}
	messages = client.receive_message("https://sqs.example/queue")
	assert messages[0]["Body"] == "legacy-text"
	assert isinstance(messages[0]["Body"], str)
