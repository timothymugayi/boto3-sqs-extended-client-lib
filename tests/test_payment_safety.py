"""Payment-safe receive, FIFO, and redrive checks from the 0.5.0 spec."""
import base64
import binascii
import json
import os

import pytest
from botocore.exceptions import (
	ClientError,
	CredentialRetrievalError,
	EndpointConnectionError,
	MissingParametersError,
	NoCredentialsError,
	ParamValidationError,
	PartialCredentialsError,
	ReadTimeoutError,
	UnknownCredentialError,
	ValidationError,
)

from pysqs_extended_client.SQSClientExtended import (
	EVENT_MESSAGE_LEFT,
	EVENT_S3_OBJECT_ORPHANED,
	INLINE_BINARY_CONTENT_TYPE_ATTRIBUTE_BYTES,
	MESSAGE_LEFT_BROKEN_POINTER,
	MESSAGE_LEFT_S3_NOT_FOUND,
	ORPHAN_AFTER_SQS_SUCCEEDED,
	ORPHAN_AFTER_SQS_UNCERTAIN,
	ORPHAN_AFTER_UPLOAD_SQS_REJECTED,
)
from tests.fakes import EMBEDDED_HANDLE, PAYLOAD_S3_POINTER_CLASS, FakeS3, FakeSqs, make_client


FIFO_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/payments.fifo"
STANDARD_URL = "https://sqs.example/queue"
DLQ_ARN = "arn:aws:sqs:us-east-1:123456789012:payments-dlq.fifo"
PAGE_REASONS = (EVENT_MESSAGE_LEFT, EVENT_S3_OBJECT_ORPHANED)


def _policy(arn=DLQ_ARN, count="5"):
	return json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": count})


def _allow_redrive(mock_sqs, arn=DLQ_ARN, count="5"):
	mock_sqs.queue_attributes = {"RedrivePolicy": _policy(arn, count)}


def _events_of(**config_kwargs):
	events = []
	client, mock_sqs, mock_s3 = make_client(
		on_event=lambda name, **fields: events.append((name, fields)),
		**config_kwargs,
	)
	return client, mock_sqs, mock_s3, events


def _pointer_message(bucket="test-bucket", key="missing-key", receipt="orig-handle", extra=None):
	body = {"s3BucketName": bucket, "s3Key": key}
	if extra:
		body.update(extra)
	return {
		"Body": json.dumps([PAYLOAD_S3_POINTER_CLASS, body]),
		"ReceiptHandle": receipt,
		"MessageAttributes": {
			"SQSLargePayloadSize": {"StringValue": "5", "DataType": "Number"},
		},
	}


def _client_error(code, status=None, retry_attempts=None, operation="GetObject"):
	response = {"Error": {"Code": code, "Message": code}}
	meta = {}
	if status is not None:
		meta["HTTPStatusCode"] = status
	if retry_attempts is not None:
		meta["RetryAttempts"] = retry_attempts
	if meta:
		response["ResponseMetadata"] = meta
	return ClientError(response, operation)


def test_inline_binary_content_type_attribute_is_38_bytes():
	assert INLINE_BINARY_CONTENT_TYPE_ATTRIBUTE_BYTES == 38
	assert (
		len("SQSExtendedContentType") + len("String") + len("binary-b64")
		== INLINE_BINARY_CONTENT_TYPE_ATTRIBUTE_BYTES
	)


def test_binary_196579_offloads_and_196578_stays_inline_base64():
	offload_raw = b"a" * 196579
	inline_raw = b"b" * 196578
	assert 4 * ((len(offload_raw) + 2) // 3) + 38 == 262146
	assert 4 * ((len(inline_raw) + 2) // 3) + 38 == 262142

	client, mock_sqs, mock_s3 = make_client()
	client.send_message(STANDARD_URL, offload_raw)
	sent = mock_sqs.send_calls[0]
	assert mock_s3.put_calls[0]["Body"] == offload_raw
	assert sent["MessageAttributes"]["SQSExtendedContentType"]["StringValue"] == "binary"
	assert sent["MessageAttributes"]["SQSLargePayloadSize"]["StringValue"] == "196579"
	assert json.loads(sent["MessageBody"])[0] == PAYLOAD_S3_POINTER_CLASS

	client.send_message(STANDARD_URL, inline_raw)
	sent = mock_sqs.send_calls[1]
	assert len(mock_s3.put_calls) == 1
	assert base64.b64decode(sent["MessageBody"]) == inline_raw
	assert sent["MessageAttributes"]["SQSExtendedContentType"]["StringValue"] == "binary-b64"


def test_text_of_196579_bytes_stays_inline():
	client, mock_sqs, mock_s3 = make_client()
	body = "t" * 196579
	client.send_message(STANDARD_URL, body)
	assert mock_s3.put_calls == []
	assert mock_sqs.send_calls[0]["MessageBody"] == body
	assert "SQSExtendedContentType" not in mock_sqs.send_calls[0].get("MessageAttributes", {})


def test_existing_attributes_count_toward_the_inline_binary_threshold():
	# "note" (4) + "String" (6) + "x" (1) = 11.
	# 196569 -> base64 262092 + 38 + 11 = 262141, inline.
	# 196570 -> base64 262096 + 38 + 11 = 262145, offload.
	attrs = {"note": {"DataType": "String", "StringValue": "x"}}
	inline_raw = b"c" * 196569
	offload_raw = b"d" * 196570
	client, mock_sqs, mock_s3 = make_client()
	client.send_message(STANDARD_URL, inline_raw, message_attributes=dict(attrs))
	assert mock_s3.put_calls == []
	assert base64.b64decode(mock_sqs.send_calls[0]["MessageBody"]) == inline_raw

	client.send_message(STANDARD_URL, offload_raw, message_attributes=dict(attrs))
	assert mock_s3.put_calls[0]["Body"] == offload_raw
	assert mock_sqs.send_calls[1]["MessageAttributes"]["SQSExtendedContentType"]["StringValue"] == "binary"
	assert "note" in mock_sqs.send_calls[1]["MessageAttributes"]


def test_small_file_in_the_base64_overflow_zone_offloads_raw_bytes(tmp_path):
	payload_path = tmp_path / "wide.bin"
	payload_path.write_bytes(b"z" * 40)
	client, mock_sqs, mock_s3 = make_client(message_size_threshold=50)
	client.send_message(STANDARD_URL, payload_path)
	assert mock_s3.put_calls[0]["UploadMethod"] == "upload_file"
	assert mock_s3.put_calls[0]["Body"] == b"z" * 40
	assert mock_sqs.send_calls[0]["MessageAttributes"]["SQSExtendedContentType"]["StringValue"] == "binary"

	tiny = tmp_path / "tiny.bin"
	tiny.write_bytes(b"xyzw")
	client.send_message(STANDARD_URL, tiny)
	assert len(mock_s3.put_calls) == 1
	assert base64.b64decode(mock_sqs.send_calls[1]["MessageBody"]) == b"xyzw"


@pytest.mark.parametrize(
	"pointer_body",
	[
		{"s3BucketName": "test-bucket"},
		{"s3Key": "only-key"},
		{"s3BucketName": "", "s3Key": "k"},
		{"s3BucketName": "test-bucket", "s3Key": ""},
		{"s3BucketName": "   ", "s3Key": "k"},
		{"s3BucketName": "test-bucket", "s3Key": "   "},
		{"s3BucketName": None, "s3Key": "k"},
		{"s3BucketName": "test-bucket", "s3Key": None},
		"not-a-pointer",
		["software.amazon.payloadoffloading.PayloadS3Pointer"],
	],
)
@pytest.mark.parametrize("ignore", [False, True])
def test_broken_pointer_leaves_the_message_and_emits_message_left(pointer_body, ignore):
	client, mock_sqs, mock_s3, events = _events_of(ignore_payload_not_found=ignore)
	if isinstance(pointer_body, list) and pointer_body and pointer_body[0] == PAYLOAD_S3_POINTER_CLASS:
		body = json.dumps(pointer_body)
	elif isinstance(pointer_body, str):
		body = json.dumps(pointer_body)
	else:
		body = json.dumps([PAYLOAD_S3_POINTER_CLASS, pointer_body])
	mock_sqs.receive_response = {
		"Messages": [{
			"Body": body,
			"ReceiptHandle": "leave-me",
			"MessageAttributes": {
				"SQSLargePayloadSize": {"StringValue": "4", "DataType": "Number"},
			},
		}]
	}
	assert client.receive_message(STANDARD_URL) == []
	assert mock_sqs.delete_calls == []
	assert mock_s3.get_calls == []
	assert mock_sqs.visibility_calls == []
	assert len(events) == 1
	assert events[0][0] == EVENT_MESSAGE_LEFT
	assert events[0][1]["cause"] == MESSAGE_LEFT_BROKEN_POINTER
	assert events[0][1]["receipt_handle"] == "leave-me"
	assert events[0][1]["queue_url"] == STANDARD_URL


def test_broken_pointer_does_not_drop_a_readable_sibling():
	client, mock_sqs, mock_s3, events = _events_of()
	mock_s3.objects[("test-bucket", "good")] = b"payload"
	mock_sqs.receive_response = {
		"Messages": [
			_pointer_message(key="good", receipt="good-handle"),
			{
				"Body": json.dumps([
					PAYLOAD_S3_POINTER_CLASS,
					{"s3BucketName": "test-bucket"},
				]),
				"ReceiptHandle": "broken-handle",
				"MessageAttributes": {
					"SQSLargePayloadSize": {"StringValue": "4", "DataType": "Number"},
				},
			},
		]
	}
	messages = client.receive_message(STANDARD_URL)
	assert [message["Body"] for message in messages] == ["payload"]
	assert mock_sqs.delete_calls == []
	assert [name for name, _ in events] == ["s3_hydrate", EVENT_MESSAGE_LEFT]
	assert events[1][1]["cause"] == MESSAGE_LEFT_BROKEN_POINTER


@pytest.mark.parametrize("ignore", [False, True])
def test_confirmed_not_found_leaves_the_message(ignore):
	client, mock_sqs, _mock_s3, events = _events_of(ignore_payload_not_found=ignore)
	assert client.config.ignore_payload_not_found is ignore
	mock_sqs.receive_response = {"Messages": [_pointer_message()]}
	assert client.receive_message(STANDARD_URL) == []
	assert mock_sqs.delete_calls == []
	assert mock_sqs.visibility_calls == []
	assert events[0][0] == EVENT_MESSAGE_LEFT
	assert events[0][1]["cause"] == MESSAGE_LEFT_S3_NOT_FOUND
	assert events[0][1]["s3_bucket_name"] == "test-bucket"
	assert events[0][1]["s3_key"] == "missing-key"
	assert "payload_missing_deleted" not in [name for name, _ in events]


@pytest.mark.parametrize("code", ["NoSuchKey", "404", "NotFound"])
def test_confirmed_not_found_codes_leave_the_message(code):
	client, mock_sqs, _mock_s3, events = _events_of()

	def missing(*_args, **_kwargs):
		raise _client_error(code, status=404)

	client._hydrate_from_s3 = missing
	mock_sqs.receive_response = {"Messages": [_pointer_message()]}
	assert client.receive_message(STANDARD_URL) == []
	assert mock_sqs.delete_calls == []
	assert events[0][1]["cause"] == MESSAGE_LEFT_S3_NOT_FOUND


def test_timeout_raises_without_deleting_or_emitting_or_undoing_the_receive():
	client, mock_sqs, _mock_s3, events = _events_of(ignore_payload_not_found=True)

	def timed_out(*_args, **_kwargs):
		raise ReadTimeoutError(endpoint_url="https://s3.amazonaws.com/test-bucket")

	client._hydrate_from_s3 = timed_out
	mock_sqs.receive_response = {"Messages": [_pointer_message()]}
	with pytest.raises(ReadTimeoutError):
		client.receive_message(STANDARD_URL)
	# SQS already counted this receive. Nothing here puts the message back.
	assert mock_sqs.receive_calls
	assert mock_sqs.delete_calls == []
	assert mock_sqs.visibility_calls == []
	assert events == []


def test_http_5xx_raises_and_is_not_a_not_found_even_if_the_code_says_so():
	client, mock_sqs, _mock_s3, events = _events_of(ignore_payload_not_found=True)

	def blip(*_args, **_kwargs):
		raise _client_error("NoSuchKey", status=503)

	client._hydrate_from_s3 = blip
	mock_sqs.receive_response = {"Messages": [_pointer_message()]}
	with pytest.raises(ClientError):
		client.receive_message(STANDARD_URL)
	assert mock_sqs.receive_calls
	assert mock_sqs.delete_calls == []
	assert mock_sqs.visibility_calls == []
	assert events == []


def test_other_s3_errors_raise_without_a_message_left_event():
	client, mock_sqs, _mock_s3, events = _events_of()

	def denied(*_args, **_kwargs):
		raise _client_error("AccessDenied", status=403)

	client._hydrate_from_s3 = denied
	mock_sqs.receive_response = {"Messages": [_pointer_message()]}
	with pytest.raises(ClientError):
		client.receive_message(STANDARD_URL)
	assert mock_sqs.delete_calls == []
	assert events == []


def test_invalid_pointer_json_is_a_broken_pointer():
	client, mock_sqs, _mock_s3, events = _events_of()
	mock_sqs.receive_response = {
		"Messages": [{
			"Body": "{not-json",
			"ReceiptHandle": "h",
			"MessageAttributes": {
				"SQSLargePayloadSize": {"StringValue": "1", "DataType": "Number"},
			},
		}]
	}
	assert client.receive_message(STANDARD_URL) == []
	assert mock_sqs.delete_calls == []
	assert mock_sqs.visibility_calls == []
	assert len(events) == 1
	assert events[0][0] == EVENT_MESSAGE_LEFT
	assert events[0][1]["cause"] == MESSAGE_LEFT_BROKEN_POINTER
	assert events[0][1]["receipt_handle"] == "h"


def test_group_id_alone_is_sent_for_an_inline_fifo_body():
	"""SQS accepts this only when the queue uses content-based deduplication."""
	client, mock_sqs, mock_s3, events = _events_of()
	_allow_redrive(mock_sqs)
	result = client.send_message(FIFO_URL, "pay-1", message_group_id="instruction-7")
	assert result == {"MessageId": "mid"}
	sent = mock_sqs.send_calls[0]
	assert sent["MessageBody"] == "pay-1"
	assert sent["MessageGroupId"] == "instruction-7"
	assert "MessageDeduplicationId" not in sent
	assert mock_s3.put_calls == []
	assert events == []
	assert mock_sqs.get_queue_attribute_calls[0]["AttributeNames"] == ["RedrivePolicy"]


def test_sqs_rejecting_an_inline_fifo_send_is_not_a_posted_message():
	class RejectingSqs(FakeSqs):
		def send_message(self, **kwargs):
			self.send_calls.append(kwargs)
			raise ClientError(
				{
					"Error": {
						"Code": "InvalidParameterValue",
						"Message": "MessageDeduplicationId is required for this queue",
					}
				},
				"SendMessage",
			)

	events = []
	mock_sqs = RejectingSqs()
	mock_s3 = FakeS3()
	_allow_redrive(mock_sqs)
	client, _, _, events = _events_of()
	client.sqs = mock_sqs
	client.s3 = mock_s3
	with pytest.raises(ClientError, match="MessageDeduplicationId"):
		client.send_message(FIFO_URL, "pay-1", message_group_id="instruction-7")
	assert mock_s3.put_calls == []
	assert "MessageDeduplicationId" not in mock_sqs.send_calls[0]
	assert events == []


def test_fifo_offload_without_dedup_id_raises_before_upload_on_send_and_batch():
	client, mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	_allow_redrive(mock_sqs)
	with pytest.raises(ValueError, match="does not invent one"):
		client.send_message(FIFO_URL, "large-enough", message_group_id="g")
	assert mock_s3.put_calls == []
	assert mock_sqs.send_calls == []
	assert events == []

	with pytest.raises(ValueError, match="does not invent one"):
		client.send_message_batch(
			FIFO_URL,
			[
				{"Id": "inline", "MessageBody": "small", "MessageGroupId": "g"},
				{"Id": "offload", "MessageBody": "also-small-but-always-s3", "MessageGroupId": "g"},
			],
		)
	assert mock_s3.put_calls == []
	assert mock_sqs.send_batch_calls == []
	assert events == []


def test_fifo_batch_raises_before_upload_when_a_later_entry_lacks_a_dedup_id():
	client, mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	_allow_redrive(mock_sqs)
	with pytest.raises(ValueError, match="message_deduplication_id"):
		client.send_message_batch(
			FIFO_URL,
			[
				{
					"Id": "first",
					"MessageBody": "one",
					"MessageGroupId": "g",
					"MessageDeduplicationId": "caller-dedup",
				},
				{"Id": "second", "MessageBody": "two", "MessageGroupId": "g"},
			],
		)
	assert mock_s3.put_calls == []
	assert mock_sqs.send_batch_calls == []
	assert events == []


def test_fifo_offload_keeps_the_caller_dedup_id_and_does_not_invent_one():
	client, mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	_allow_redrive(mock_sqs)
	client.send_message(
		FIFO_URL,
		"body",
		message_group_id="g",
		message_deduplication_id="caller-dedup",
	)
	sent = mock_sqs.send_calls[0]
	assert sent["MessageDeduplicationId"] == "caller-dedup"
	assert sent["MessageGroupId"] == "g"
	assert len(mock_s3.put_calls) == 1
	assert [name for name, _ in events] == ["s3_offload"]

	client.send_message_batch(
		FIFO_URL,
		[{
			"Id": "1",
			"MessageBody": "batch-body",
			"MessageGroupId": "g",
			"MessageDeduplicationId": "batch-dedup",
		}],
	)
	entry = mock_sqs.send_batch_calls[0]["Entries"][0]
	assert entry["MessageDeduplicationId"] == "batch-dedup"
	assert len(mock_s3.put_calls) == 2


def test_blank_dedup_id_does_not_count_as_supplied_for_a_fifo_offload():
	client, mock_sqs, mock_s3, _events = _events_of(always_through_s3=True)
	_allow_redrive(mock_sqs)
	with pytest.raises(ValueError, match="does not invent one"):
		client.send_message(
			FIFO_URL,
			"body",
			message_group_id="g",
			message_deduplication_id="   ",
		)
	assert mock_s3.put_calls == []
	with pytest.raises(ValueError, match="does not invent one"):
		client.send_message_batch(
			FIFO_URL,
			[{
				"Id": "1",
				"MessageBody": "body",
				"MessageGroupId": "g",
				"MessageDeduplicationId": "  ",
			}],
		)
	assert mock_s3.put_calls == []


def test_dedup_id_without_a_group_id_raises_before_upload():
	client, _mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	with pytest.raises(ValueError, match="message_group_id is required"):
		client.send_message(STANDARD_URL, "body", message_deduplication_id="dedup-only")
	assert mock_s3.put_calls == []
	assert events == []


def test_group_id_kwargs_are_forwarded_for_an_inline_send():
	client, mock_sqs, mock_s3, _events = _events_of()
	_allow_redrive(mock_sqs)
	client.send_message(FIFO_URL, "inline", MessageGroupId="from-kwarg")
	assert mock_sqs.send_calls[0]["MessageGroupId"] == "from-kwarg"
	assert "MessageDeduplicationId" not in mock_sqs.send_calls[0]
	assert mock_s3.put_calls == []


def test_standard_queue_offload_does_not_read_redrive_policy():
	client, mock_sqs, mock_s3, _events = _events_of(always_through_s3=True)
	client.send_message(STANDARD_URL, "body", message_group_id="g")
	client.send_message_batch(STANDARD_URL, [{"Id": "1", "MessageBody": "batch"}])
	mock_sqs.receive_response = {"Messages": []}
	assert client.receive_message(STANDARD_URL) == []
	assert mock_sqs.get_queue_attribute_calls == []
	assert len(mock_s3.put_calls) == 2
	assert "MessageDeduplicationId" not in mock_sqs.send_calls[0]


@pytest.mark.parametrize(
	"attributes,match",
	[
		({}, "is missing"),
		({"RedrivePolicy": ""}, "is missing"),
		({"RedrivePolicy": "   "}, "is missing"),
		({"RedrivePolicy": "{}"}, "is empty"),
		({"RedrivePolicy": "not-json"}, "is not valid JSON"),
		({"RedrivePolicy": "null"}, "lacks deadLetterTargetArn"),
		({"RedrivePolicy": "[]"}, "lacks deadLetterTargetArn"),
		({"RedrivePolicy": "1"}, "lacks deadLetterTargetArn"),
		(
			{"RedrivePolicy": json.dumps({"maxReceiveCount": "5"})},
			"lacks deadLetterTargetArn",
		),
		(
			{"RedrivePolicy": json.dumps({
				"deadLetterTargetArn": "  ",
				"maxReceiveCount": "5",
			})},
			"lacks deadLetterTargetArn",
		),
		(
			{"RedrivePolicy": json.dumps({"deadLetterTargetArn": DLQ_ARN})},
			"lacks maxReceiveCount",
		),
		(
			{"RedrivePolicy": json.dumps({
				"deadLetterTargetArn": DLQ_ARN,
				"maxReceiveCount": "",
			})},
			"lacks maxReceiveCount",
		),
		(
			{"RedrivePolicy": json.dumps({
				"deadLetterTargetArn": DLQ_ARN,
				"maxReceiveCount": None,
			})},
			"lacks maxReceiveCount",
		),
		(
			{"RedrivePolicy": json.dumps({
				"deadLetterTargetArn": DLQ_ARN,
				"maxReceiveCount": 0,
			})},
			"requires maxReceiveCount >= 1",
		),
		(
			{"RedrivePolicy": json.dumps({
				"deadLetterTargetArn": DLQ_ARN,
				"maxReceiveCount": "0",
			})},
			"requires maxReceiveCount >= 1",
		),
		(
			{"RedrivePolicy": json.dumps({
				"deadLetterTargetArn": DLQ_ARN,
				"maxReceiveCount": -1,
			})},
			"requires maxReceiveCount >= 1",
		),
		(
			{"RedrivePolicy": json.dumps({
				"deadLetterTargetArn": DLQ_ARN,
				"maxReceiveCount": "3.0",
			})},
			"integer maxReceiveCount of at least 1",
		),
		(
			{"RedrivePolicy": json.dumps({
				"deadLetterTargetArn": DLQ_ARN,
				"maxReceiveCount": 3.0,
			})},
			"integer maxReceiveCount of at least 1",
		),
		(
			{"RedrivePolicy": json.dumps({
				"deadLetterTargetArn": DLQ_ARN,
				"maxReceiveCount": "²",
			})},
			"lacks maxReceiveCount",
		),
		(
			{"RedrivePolicy": json.dumps({
				"deadLetterTargetArn": DLQ_ARN,
				"maxReceiveCount": "nope",
			})},
			"lacks maxReceiveCount",
		),
	],
)
def test_fifo_send_and_receive_refuse_a_bad_redrive_policy(attributes, match):
	client, mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	mock_sqs.queue_attributes = attributes
	with pytest.raises(ValueError, match=match):
		client.send_message(FIFO_URL, "body", message_group_id="g", message_deduplication_id="d")
	assert mock_s3.put_calls == []
	assert mock_sqs.send_calls == []
	assert events == []

	events.clear()
	with pytest.raises(ValueError, match=match):
		client.send_message_batch(
			FIFO_URL,
			[{"Id": "1", "MessageBody": "body", "MessageGroupId": "g", "MessageDeduplicationId": "d"}],
		)
	assert mock_s3.put_calls == []
	assert mock_sqs.send_batch_calls == []
	assert events == []

	events.clear()
	before = len(mock_sqs.receive_calls)
	with pytest.raises(ValueError, match=match):
		client.receive_message(FIFO_URL)
	assert len(mock_sqs.receive_calls) == before
	assert events == []


def test_fifo_redrive_attribute_call_failure_raises_before_upload_or_receive():
	client, mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	mock_sqs.get_queue_attributes_error = ClientError(
		{"Error": {"Code": "AccessDenied", "Message": "no"}},
		"GetQueueAttributes",
	)
	with pytest.raises(ClientError):
		client.send_message(FIFO_URL, "body", message_group_id="g", message_deduplication_id="d")
	assert mock_s3.put_calls == []
	assert events == []

	mock_sqs.get_queue_attributes_error = RuntimeError("sqs attributes down")
	with pytest.raises(RuntimeError, match="sqs attributes down"):
		client.receive_message(FIFO_URL)
	assert mock_sqs.receive_calls == []
	assert events == []
	with pytest.raises(RuntimeError, match="sqs attributes down"):
		client.send_message_batch(FIFO_URL, [{"Id": "1", "MessageBody": "x"}])
	assert mock_sqs.send_batch_calls == []


def test_redrive_policy_is_not_cached_across_calls():
	client, mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	_allow_redrive(mock_sqs)
	client.send_message(FIFO_URL, "one", message_group_id="g", message_deduplication_id="d1")
	assert len(mock_s3.put_calls) == 1
	mock_sqs.queue_attributes = {}
	with pytest.raises(ValueError, match="is missing"):
		client.send_message(FIFO_URL, "two", message_group_id="g", message_deduplication_id="d2")
	assert len(mock_s3.put_calls) == 1
	assert len(mock_sqs.get_queue_attribute_calls) == 2
	assert events == [] or all(name == "s3_offload" for name, _ in events)
	assert EVENT_S3_OBJECT_ORPHANED not in [name for name, _ in events]
	assert EVENT_MESSAGE_LEFT not in [name for name, _ in events]


@pytest.mark.parametrize(
	"queue_url",
	[
		FIFO_URL + "?foo=1",
		FIFO_URL + "/",
		"payments.fifo",
	],
)
def test_fifo_suffix_is_taken_from_the_queue_name(queue_url):
	client, mock_sqs, _mock_s3, _events = _events_of()
	_allow_redrive(mock_sqs)
	client.send_message(queue_url, "inline", message_group_id="g")
	assert mock_sqs.get_queue_attribute_calls[0]["QueueUrl"] == queue_url


def test_parsed_redrive_policy_dict_and_numeric_count_are_accepted():
	client, mock_sqs, mock_s3, _events = _events_of()
	mock_sqs.queue_attributes = {
		"RedrivePolicy": {"deadLetterTargetArn": DLQ_ARN, "maxReceiveCount": 0},
	}
	with pytest.raises(ValueError, match="requires maxReceiveCount >= 1") as caught:
		client.send_message(FIFO_URL, "inline", message_group_id="g")
	assert "maxReceiveCount of at least 1" in str(caught.value)
	assert mock_sqs.send_calls == []
	assert mock_s3.put_calls == []

	mock_sqs.queue_attributes = {
		"RedrivePolicy": {"deadLetterTargetArn": DLQ_ARN, "maxReceiveCount": 1},
	}
	client.send_message(FIFO_URL, "inline", message_group_id="g")
	assert mock_sqs.send_calls
	assert mock_s3.put_calls == []

	mock_sqs.queue_attributes = {"RedrivePolicy": _policy(count="5")}
	client.receive_message(FIFO_URL)
	assert mock_sqs.receive_calls


def test_missing_attribute_map_is_a_missing_policy():
	client, mock_sqs, mock_s3, events = _events_of()

	def odd_response(**kwargs):
		mock_sqs.get_queue_attribute_calls.append(kwargs)
		return None

	mock_sqs.get_queue_attributes = odd_response
	with pytest.raises(ValueError, match="is missing"):
		client.send_message(FIFO_URL, "inline", message_group_id="g")
	assert mock_s3.put_calls == []
	assert events == []

	def attributes_not_a_dict(**kwargs):
		mock_sqs.get_queue_attribute_calls.append(kwargs)
		return {"Attributes": ["RedrivePolicy"]}

	mock_sqs.get_queue_attributes = attributes_not_a_dict
	with pytest.raises(ValueError, match="is missing"):
		client.receive_message(FIFO_URL)
	assert mock_sqs.receive_calls == []


def test_queue_name_helper_ignores_non_strings():
	client, _mock_sqs, _mock_s3 = make_client()
	assert client._queue_name(None) == ""
	assert client._queue_name("") == ""
	assert client._is_fifo_queue(None) is False


def test_successful_offload_hydrate_and_delete_do_not_use_page_reasons():
	client, mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	client.send_message(STANDARD_URL, "hello")
	body = mock_sqs.send_calls[0]["MessageBody"]
	attrs = mock_sqs.send_calls[0]["MessageAttributes"]
	mock_sqs.receive_response = {
		"Messages": [{
			"Body": body,
			"ReceiptHandle": "h",
			"MessageAttributes": attrs,
		}]
	}
	messages = client.receive_message(STANDARD_URL)
	client.delete_message(STANDARD_URL, messages[0]["ReceiptHandle"])
	names = [name for name, _ in events]
	assert "s3_offload" in names
	assert "s3_hydrate" in names
	assert "s3_delete" in names
	assert EVENT_MESSAGE_LEFT not in names
	assert EVENT_S3_OBJECT_ORPHANED not in names
	assert ("test-bucket", json.loads(body)[1]["s3Key"]) not in mock_s3.objects


def test_send_cleanup_success_does_not_emit_the_orphan_reason():
	class FailingSqs(FakeSqs):
		def send_message(self, **kwargs):
			self.send_calls.append(kwargs)
			raise _client_error(
				"InvalidParameterValue", status=400, retry_attempts=0, operation="SendMessage"
			)

	events = []
	client, _mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	client.sqs = FailingSqs()
	with pytest.raises(ClientError):
		client.send_message(STANDARD_URL, "hello")
	assert mock_s3.objects == {}
	assert len(mock_s3.delete_calls) == 1
	assert EVENT_S3_OBJECT_ORPHANED not in [name for name, _ in events]
	assert EVENT_MESSAGE_LEFT not in [name for name, _ in events]


def test_batch_send_rejection_cleanup_failure_names_the_orphan_case():
	class FailingSqs(FakeSqs):
		def send_message_batch(self, **kwargs):
			self.send_batch_calls.append(kwargs)
			raise _client_error(
				"InvalidParameterValue", status=400, retry_attempts=0, operation="SendMessageBatch"
			)

	class CleanupFails(FakeS3):
		def delete_object(self, **kwargs):
			self.delete_calls.append(kwargs)
			raise RuntimeError("cleanup down")

	events = []
	mock_s3 = CleanupFails()
	client, _mock_sqs, _mock_s3, events = _events_of(always_through_s3=True)
	client.sqs = FailingSqs()
	client.s3 = mock_s3
	with pytest.raises(ClientError):
		client.send_message_batch(
			STANDARD_URL,
			[{"Id": "1", "MessageBody": "one"}, {"Id": "2", "MessageBody": "two"}],
		)
	cases = [fields["case"] for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert cases == [ORPHAN_AFTER_UPLOAD_SQS_REJECTED, ORPHAN_AFTER_UPLOAD_SQS_REJECTED]
	assert EVENT_MESSAGE_LEFT not in [name for name, _ in events]


def test_partial_batch_failure_orphan_is_only_the_rejected_entry():
	class PartialSqs(FakeSqs):
		def send_message_batch(self, **kwargs):
			self.send_batch_calls.append(kwargs)
			return {
				"Successful": [{"Id": "ok"}],
				"Failed": [{"Id": "bad", "Code": "InvalidParameterValue", "SenderFault": True}],
			}

	class CleanupFails(FakeS3):
		def delete_object(self, **kwargs):
			self.delete_calls.append(kwargs)
			raise RuntimeError("cleanup down")

	events = []
	mock_sqs = PartialSqs()
	mock_s3 = CleanupFails()
	client, _, _, events = _events_of(always_through_s3=True)
	client.sqs = mock_sqs
	client.s3 = mock_s3
	client.send_message_batch(
		STANDARD_URL,
		[{"Id": "ok", "MessageBody": "kept"}, {"Id": "bad", "MessageBody": "drop"}],
	)
	orphan = [fields for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert len(orphan) == 1
	assert orphan[0]["case"] == ORPHAN_AFTER_UPLOAD_SQS_REJECTED
	assert orphan[0]["s3_key"]
	kept = [
		key for (_bucket, key), body in mock_s3.objects.items() if body == b"kept"
	]
	assert kept


def test_failed_batch_offload_cleanup_emits_orphan_and_does_not_send():
	class FlakyS3(FakeS3):
		def put_object(self, **kwargs):
			if kwargs.get("Body") == b"bad":
				raise RuntimeError("s3 down")
			return FakeS3.put_object(self, **kwargs)

		def delete_object(self, **kwargs):
			self.delete_calls.append(kwargs)
			raise RuntimeError("cleanup down")

	events = []
	mock_sqs = FakeSqs()
	mock_s3 = FlakyS3()
	client, _, _, events = _events_of(always_through_s3=True)
	client.sqs = mock_sqs
	client.s3 = mock_s3
	with pytest.raises(RuntimeError, match="s3 down"):
		client.send_message_batch(
			STANDARD_URL,
			[{"Id": "1", "MessageBody": "ok"}, {"Id": "2", "MessageBody": "bad"}],
		)
	assert mock_sqs.send_batch_calls == []
	assert [fields["case"] for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED] == [
		ORPHAN_AFTER_UPLOAD_SQS_REJECTED
	]


def test_delete_batch_sqs_failure_does_not_delete_s3_or_emit():
	class FailingSqs(FakeSqs):
		def delete_message_batch(self, **kwargs):
			self.delete_batch_calls.append(kwargs)
			raise RuntimeError("batch down")

	events = []
	mock_sqs = FailingSqs()
	mock_s3 = FakeS3()
	mock_s3.objects[("test-bucket", "abc-key")] = b"payload"
	from tests.fakes import EMBEDDED_HANDLE
	from pysqs_extended_client import SQSClientExtended
	from pysqs_extended_client.extended_config import ExtendedClientConfiguration

	client = SQSClientExtended(
		sqs_client=mock_sqs,
		s3_client=mock_s3,
		config=ExtendedClientConfiguration(
			s3_bucket_name="test-bucket",
			on_event=lambda name, **fields: events.append(name),
		),
	)
	with pytest.raises(RuntimeError, match="batch down"):
		client.delete_message_batch(STANDARD_URL, [{"Id": "1", "ReceiptHandle": EMBEDDED_HANDLE}])
	assert mock_s3.delete_calls == []
	assert mock_s3.objects[("test-bucket", "abc-key")] == b"payload"
	assert events == []


def test_payload_support_disabled_still_checks_fifo_redrive_before_send():
	client, mock_sqs, mock_s3, events = _events_of(payload_support_enabled=False, s3_bucket_name=None)
	with pytest.raises(ValueError, match="RedrivePolicy"):
		client.send_message(FIFO_URL, "small", message_group_id="g")
	assert mock_sqs.send_calls == []
	assert mock_s3.put_calls == []
	assert events == []

	_allow_redrive(mock_sqs)
	client.send_message(FIFO_URL, "small", message_group_id="g")
	assert mock_sqs.send_calls[0]["MessageBody"] == "small"
	assert "MessageDeduplicationId" not in mock_sqs.send_calls[0]


def test_confirmed_not_found_classifier_rejects_non_client_errors_and_blank_responses():
	client, _mock_sqs, _mock_s3 = make_client()
	assert client._is_confirmed_s3_not_found(RuntimeError("timeout-like")) is False
	exc = _client_error("NoSuchKey", status=404)
	exc.response = None
	assert client._is_confirmed_s3_not_found(exc) is False
	exc = _client_error("NoSuchKey", status=404)
	exc.response = {"Error": "nope"}
	assert client._is_confirmed_s3_not_found(exc) is False
	assert client._is_confirmed_s3_not_found(_client_error("InternalError", status=500)) is False


def test_non_string_dedup_id_is_forwarded_unchanged():
	client, mock_sqs, _mock_s3 = make_client()
	client.send_message(
		STANDARD_URL,
		"inline",
		message_group_id="g",
		message_deduplication_id=12345,
	)
	assert mock_sqs.send_calls[0]["MessageDeduplicationId"] == 12345


def _real_s3_404(code, operation):
	return ClientError(
		{
			"Error": {
				"Code": code,
				"Message": "The specified key does not exist.",
				"Key": "missing-key",
			},
			"ResponseMetadata": {
				"RequestId": "4442587FB7D0A2F9",
				"HostId": "9+host",
				"HTTPStatusCode": 404,
				"HTTPHeaders": {
					"x-amz-request-id": "4442587FB7D0A2F9",
					"x-amz-id-2": "9+host",
					"content-type": "application/xml",
				},
				"RetryAttempts": 0,
			},
		},
		operation,
	)


@pytest.mark.parametrize("keys", [("boom", "missing", "good"), ("missing", "boom", "good")])
def test_mixed_batch_emits_message_left_for_not_found_in_either_order(keys, tmp_path):
	client, mock_sqs, _mock_s3, events = _events_of()

	def hydrate(_bucket, key, as_binary=False, filename=None):
		if filename:
			with open(filename, "wb") as handle:
				handle.write(key.encode("utf-8"))
		if key == "missing":
			raise _client_error("NoSuchKey", status=404)
		if key == "boom":
			raise _client_error("InternalError", status=503)
		return filename

	client._hydrate_from_s3 = hydrate
	mock_sqs.receive_response = {
		"Messages": [_pointer_message(key=key, receipt="h-" + key) for key in keys]
	}
	with pytest.raises(ClientError) as caught:
		client.receive_message(STANDARD_URL, payload_dir=str(tmp_path))
	assert caught.value.response["ResponseMetadata"]["HTTPStatusCode"] == 503
	left = [fields for name, fields in events if name == EVENT_MESSAGE_LEFT]
	assert len(left) == 1
	assert left[0]["cause"] == MESSAGE_LEFT_S3_NOT_FOUND
	assert left[0]["s3_key"] == "missing"
	assert mock_sqs.delete_calls == []
	assert mock_sqs.visibility_calls == []
	assert list(tmp_path.iterdir()) == []


def _broken_pointer_message(receipt="broken-handle"):
	return {
		"Body": json.dumps([
			PAYLOAD_S3_POINTER_CLASS,
			{"s3BucketName": "test-bucket"},
		]),
		"ReceiptHandle": receipt,
		"MessageAttributes": {
			"SQSLargePayloadSize": {"StringValue": "4", "DataType": "Number"},
		},
	}


def _bad_inline_binary_message(receipt="bad-inline"):
	return {
		"Body": "abc",
		"ReceiptHandle": receipt,
		"MessageAttributes": {
			"SQSExtendedContentType": {"StringValue": "binary-b64", "DataType": "String"},
		},
	}


@pytest.mark.parametrize("other_last", [False, True])
def test_broken_pointer_still_emits_when_another_message_errors(other_last):
	client, mock_sqs, _mock_s3, events = _events_of()

	def hydrate(_bucket, key, as_binary=False, filename=None):
		raise _client_error("InternalError", status=503)

	client._hydrate_from_s3 = hydrate
	broken = _broken_pointer_message()
	other = _pointer_message(key="boom", receipt="boom-handle")
	mock_sqs.receive_response = {
		"Messages": [broken, other] if other_last else [other, broken]
	}
	with pytest.raises(ClientError) as caught:
		client.receive_message(STANDARD_URL)
	assert caught.value.response["ResponseMetadata"]["HTTPStatusCode"] == 503
	left = [fields for name, fields in events if name == EVENT_MESSAGE_LEFT]
	assert len(left) == 1
	assert left[0]["cause"] == MESSAGE_LEFT_BROKEN_POINTER
	assert mock_sqs.delete_calls == []
	assert mock_sqs.visibility_calls == []


@pytest.mark.parametrize("bad_inline_last", [False, True])
def test_bad_inline_binary_does_not_drop_sibling_message_left_events(bad_inline_last):
	client, mock_sqs, _mock_s3, events = _events_of()

	def hydrate(_bucket, key, as_binary=False, filename=None):
		raise _client_error("NoSuchKey", status=404)

	client._hydrate_from_s3 = hydrate
	broken = _broken_pointer_message()
	missing = _pointer_message(key="missing", receipt="missing-handle")
	bad = _bad_inline_binary_message()
	ordered = [broken, missing, bad] if bad_inline_last else [bad, broken, missing]
	mock_sqs.receive_response = {"Messages": ordered}
	with pytest.raises(binascii.Error):
		client.receive_message(STANDARD_URL)
	causes = [fields["cause"] for name, fields in events if name == EVENT_MESSAGE_LEFT]
	assert causes == [MESSAGE_LEFT_BROKEN_POINTER, MESSAGE_LEFT_S3_NOT_FOUND]
	assert mock_sqs.delete_calls == []
	assert mock_sqs.visibility_calls == []


@pytest.mark.parametrize(
	"raise_error",
	[
		lambda: ReadTimeoutError(endpoint_url="https://sqs.us-east-1.amazonaws.com/"),
		lambda: _client_error("InternalError", status=500),
		lambda: _client_error("InternalError", status=503),
		lambda: ConnectionError("connection reset"),
		lambda: _client_error("RequestTimeout"),
	],
)
def test_uncertain_send_keeps_the_object_and_names_the_orphan_case(raise_error):
	error = raise_error()

	class FailingSqs(FakeSqs):
		def send_message(self, **kwargs):
			self.send_calls.append(kwargs)
			raise error

	client, _mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	client.sqs = FailingSqs()
	with pytest.raises(type(error)):
		client.send_message(STANDARD_URL, "hello")
	assert mock_s3.delete_calls == []
	assert len(mock_s3.objects) == 1
	orphans = [fields for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert len(orphans) == 1
	assert orphans[0]["case"] == ORPHAN_AFTER_SQS_UNCERTAIN
	assert orphans[0]["s3_bucket_name"] == "test-bucket"
	assert orphans[0]["s3_key"]
	assert EVENT_MESSAGE_LEFT not in [name for name, _ in events]


def test_definite_batch_rejection_deletes_offloaded_objects():
	class FailingSqs(FakeSqs):
		def send_message_batch(self, **kwargs):
			self.send_batch_calls.append(kwargs)
			raise _client_error(
				"InvalidParameterValue", status=400, retry_attempts=0, operation="SendMessageBatch"
			)

	client, _mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	client.sqs = FailingSqs()
	with pytest.raises(ClientError):
		client.send_message_batch(
			STANDARD_URL,
			[{"Id": "1", "MessageBody": "one"}, {"Id": "2", "MessageBody": "two"}],
		)
	assert mock_s3.objects == {}
	assert len(mock_s3.delete_calls) == 2
	assert EVENT_S3_OBJECT_ORPHANED not in [name for name, _ in events]


def test_uncertain_batch_send_keeps_every_offloaded_object():
	class FailingSqs(FakeSqs):
		def send_message_batch(self, **kwargs):
			self.send_batch_calls.append(kwargs)
			raise _client_error("ServiceUnavailable", status=503)

	client, _mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	client.sqs = FailingSqs()
	with pytest.raises(ClientError):
		client.send_message_batch(
			STANDARD_URL,
			[{"Id": "1", "MessageBody": "one"}, {"Id": "2", "MessageBody": "two"}],
		)
	assert mock_s3.delete_calls == []
	assert len(mock_s3.objects) == 2
	cases = [fields["case"] for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert cases == [ORPHAN_AFTER_SQS_UNCERTAIN, ORPHAN_AFTER_SQS_UNCERTAIN]
	assert EVENT_MESSAGE_LEFT not in [name for name, _ in events]


@pytest.mark.parametrize(
	"failed_entry",
	[
		{"Id": "bad", "Code": "InternalError", "SenderFault": False},
		{"Id": "bad", "Code": "InternalError"},
	],
)
def test_uncertain_batch_failed_entry_keeps_the_object(failed_entry):
	class PartialSqs(FakeSqs):
		def send_message_batch(self, **kwargs):
			self.send_batch_calls.append(kwargs)
			return {"Successful": [{"Id": "ok"}], "Failed": [failed_entry]}

	client, mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	client.sqs = PartialSqs()
	result = client.send_message_batch(
		STANDARD_URL,
		[{"Id": "ok", "MessageBody": "kept"}, {"Id": "bad", "MessageBody": "drop"}],
	)
	assert result["Failed"] == [failed_entry]
	assert mock_s3.delete_calls == []
	bodies = {body for body in mock_s3.objects.values()}
	assert bodies == {b"kept", b"drop"}
	orphans = [fields for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert len(orphans) == 1
	assert orphans[0]["case"] == ORPHAN_AFTER_SQS_UNCERTAIN
	kept_keys = {key for (_bucket, key), body in mock_s3.objects.items() if body == b"kept"}
	assert orphans[0]["s3_key"] not in kept_keys


def test_delete_message_batch_s3_failure_emits_the_orphan_event(caplog):
	class FailingDeleteS3(FakeS3):
		def delete_object(self, **kwargs):
			self.delete_calls.append(kwargs)
			raise RuntimeError("s3 down")

	events = []
	mock_sqs = FakeSqs()
	mock_s3 = FailingDeleteS3()
	mock_s3.objects[("test-bucket", "abc-key")] = b"payload"
	from pysqs_extended_client import SQSClientExtended
	from pysqs_extended_client.extended_config import ExtendedClientConfiguration

	client = SQSClientExtended(
		sqs_client=mock_sqs,
		s3_client=mock_s3,
		config=ExtendedClientConfiguration(
			s3_bucket_name="test-bucket",
			on_event=lambda name, **fields: events.append((name, fields)),
		),
	)
	with caplog.at_level("WARNING"):
		result = client.delete_message_batch(
			STANDARD_URL,
			[{"Id": "1", "ReceiptHandle": EMBEDDED_HANDLE}],
		)
	assert result["Successful"][0]["Id"] == "1"
	assert mock_sqs.delete_batch_calls
	orphans = [fields for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert len(orphans) == 1
	assert orphans[0]["case"] == ORPHAN_AFTER_SQS_SUCCEEDED
	assert orphans[0]["s3_key"] == "abc-key"
	assert "orphaned" in caplog.text
	assert EVENT_MESSAGE_LEFT not in [name for name, _ in events]


def test_download_fileobj_404_leaves_the_message():
	client, mock_sqs, mock_s3, events = _events_of()

	def download_fileobj(Bucket, Key, Fileobj, ExtraArgs=None, Callback=None, Config=None):
		raise _real_s3_404("NoSuchKey", "GetObject")

	mock_s3.download_fileobj = download_fileobj
	mock_sqs.receive_response = {"Messages": [_pointer_message()]}
	assert client.receive_message(STANDARD_URL) == []
	assert mock_sqs.delete_calls == []
	assert mock_sqs.visibility_calls == []
	assert events[0][0] == EVENT_MESSAGE_LEFT
	assert events[0][1]["cause"] == MESSAGE_LEFT_S3_NOT_FOUND
	assert events[0][1]["s3_key"] == "missing-key"


def test_download_file_404_leaves_the_message_and_removes_the_partial_file(tmp_path):
	client, mock_sqs, mock_s3, events = _events_of()

	def download_file(Bucket, Key, Filename, ExtraArgs=None, Callback=None, Config=None):
		parent = os.path.dirname(Filename)
		if parent:
			os.makedirs(parent, exist_ok=True)
		with open(Filename, "wb") as handle:
			handle.write(b"partial")
		raise _real_s3_404("404", "HeadObject")

	mock_s3.download_file = download_file
	mock_sqs.receive_response = {"Messages": [_pointer_message()]}
	assert client.receive_message(STANDARD_URL, payload_dir=str(tmp_path)) == []
	assert mock_sqs.delete_calls == []
	assert mock_sqs.visibility_calls == []
	assert list(tmp_path.iterdir()) == []
	assert events[0][0] == EVENT_MESSAGE_LEFT
	assert events[0][1]["cause"] == MESSAGE_LEFT_S3_NOT_FOUND
	assert events[0][1]["s3_key"] == "missing-key"


def test_on_event_exception_is_logged_and_does_not_drop_siblings(caplog):
	seen = []

	def boom(name, **fields):
		seen.append(name)
		raise RuntimeError("callback failed")

	client, mock_sqs, mock_s3 = make_client(on_event=boom)
	mock_s3.objects[("test-bucket", "good")] = b"payload"
	broken = {
		"Body": "{not-json",
		"MessageAttributes": {
			"SQSLargePayloadSize": {"StringValue": "1", "DataType": "Number"},
		},
	}
	mock_sqs.receive_response = {
		"Messages": [
			_pointer_message(key="good", receipt="good-handle"),
			dict(broken, ReceiptHandle="b1"),
			dict(broken, ReceiptHandle="b2"),
		]
	}
	with caplog.at_level("ERROR"):
		messages = client.receive_message(STANDARD_URL)
	assert [message["Body"] for message in messages] == ["payload"]
	assert seen == ["s3_hydrate", EVENT_MESSAGE_LEFT, EVENT_MESSAGE_LEFT]
	assert "on_event callback raised" in caplog.text
	assert mock_sqs.delete_calls == []


def test_on_event_exception_after_sqs_delete_is_swallowed(caplog):
	seen = []

	def boom(name, **fields):
		seen.append((name, fields.get("case")))
		raise RuntimeError("callback failed")

	class FailingDeleteS3(FakeS3):
		def delete_object(self, **kwargs):
			self.delete_calls.append(kwargs)
			raise RuntimeError("s3 down")

	mock_sqs = FakeSqs()
	mock_s3 = FailingDeleteS3()
	mock_s3.objects[("test-bucket", "abc-key")] = b"payload"
	from pysqs_extended_client import SQSClientExtended
	from pysqs_extended_client.extended_config import ExtendedClientConfiguration

	client = SQSClientExtended(
		sqs_client=mock_sqs,
		s3_client=mock_s3,
		config=ExtendedClientConfiguration(s3_bucket_name="test-bucket", on_event=boom),
	)
	with caplog.at_level("ERROR"):
		result = client.delete_message(STANDARD_URL, EMBEDDED_HANDLE)
	assert result == {}
	assert mock_sqs.delete_calls[0]["ReceiptHandle"] == "orig-handle"
	assert seen == [(EVENT_S3_OBJECT_ORPHANED, ORPHAN_AFTER_SQS_SUCCEEDED)]
	assert "on_event callback raised" in caplog.text
	assert mock_s3.objects[("test-bucket", "abc-key")] == b"payload"


def test_not_found_payload_file_missing_on_disk_is_still_left(tmp_path):
	client, mock_sqs, _mock_s3, events = _events_of()

	def hydrate(*_args, **_kwargs):
		raise _client_error("404", status=404)

	client._hydrate_from_s3 = hydrate
	mock_sqs.receive_response = {"Messages": [_pointer_message()]}
	assert client.receive_message(STANDARD_URL, payload_dir=str(tmp_path)) == []
	assert events[0][1]["cause"] == MESSAGE_LEFT_S3_NOT_FOUND
	assert list(tmp_path.iterdir()) == []
	assert mock_sqs.delete_calls == []


def test_stuck_payload_file_is_logged_and_the_receive_error_still_raises(tmp_path, monkeypatch, caplog):
	client, mock_sqs, _mock_s3, events = _events_of()

	def hydrate(_bucket, _key, as_binary=False, filename=None):
		if filename:
			with open(filename, "wb") as handle:
				handle.write(b"x")
		raise _client_error("InternalError", status=500)

	client._hydrate_from_s3 = hydrate
	mock_sqs.receive_response = {"Messages": [_pointer_message()]}

	def stuck(_path):
		raise OSError("busy")

	monkeypatch.setattr(os, "remove", stuck)
	with caplog.at_level("WARNING"):
		with pytest.raises(ClientError):
			client.receive_message(STANDARD_URL, payload_dir=str(tmp_path))
	assert "Failed to remove payload file" in caplog.text
	assert events == []
	assert mock_sqs.delete_calls == []


def test_uncertain_retain_ignores_a_body_that_is_not_a_pointer():
	client, _mock_sqs, mock_s3, events = _events_of()
	client._retain_uncertain_upload("not-json")
	client._retain_uncertain_upload(None)
	assert mock_s3.delete_calls == []
	assert events == []


def test_client_error_without_an_integer_status_is_not_a_definite_rejection():
	client, _mock_sqs, _mock_s3 = make_client()
	assert client._client_error_status(NoCredentialsError()) is None
	missing = _client_error("InternalError", status=400)
	missing.response = None
	assert client._is_definite_sqs_rejection(missing) is False
	text_status = _client_error("InternalError")
	text_status.response = {"ResponseMetadata": {"HTTPStatusCode": "400"}}
	assert client._is_definite_sqs_rejection(text_status) is False
	bool_status = _client_error("InternalError")
	bool_status.response = {"ResponseMetadata": {"HTTPStatusCode": True}}
	assert client._is_definite_sqs_rejection(bool_status) is False
	assert client._is_definite_sqs_rejection(
		_client_error("InvalidParameterValue", status=400, operation="SendMessage")
	) is False
	assert client._is_definite_sqs_rejection(
		_client_error("InvalidParameterValue", status=400, retry_attempts=0, operation="SendMessage")
	) is True
	assert client._is_definite_sqs_rejection(
		_client_error("InvalidParameterValue", status=400, retry_attempts=0, operation="SendMessageBatch")
	) is True
	assert client._is_definite_sqs_rejection(
		_client_error("AccessDenied", status=403, retry_attempts=0, operation="AssumeRole")
	) is False
	assert client._is_definite_sqs_rejection(
		_client_error("AccessDenied", status=403, retry_attempts=0, operation="GetObject")
	) is False
	assert client._is_definite_sqs_rejection(
		_client_error("RequestThrottled", status=400, retry_attempts=3, operation="SendMessage")
	) is False
	assert client._is_definite_sqs_rejection(
		_client_error("InternalError", status=500, operation="SendMessage")
	) is False
	blank = _client_error("RequestThrottled", status=400)
	blank.response = None
	assert client._retry_attempts(blank) is None
	blank = _client_error("RequestThrottled", status=400)
	blank.response = {"ResponseMetadata": "nope"}
	assert client._retry_attempts(blank) is None
	blank = _client_error("RequestThrottled", status=400)
	blank.response = {"ResponseMetadata": {"HTTPStatusCode": 400, "RetryAttempts": None}}
	assert client._retry_attempts(blank) is None
	assert client._is_definite_sqs_rejection(blank) is False
	assert client._is_definite_sqs_rejection(ParamValidationError(report="DelaySeconds")) is True
	assert client._is_definite_sqs_rejection(
		ValidationError(value="x", param="DelaySeconds", type_name="integer")
	) is True
	assert client._is_definite_sqs_rejection(
		MissingParametersError(object_name="SendMessage", missing=["QueueUrl"])
	) is True


def test_retried_400_keeps_the_object_on_send_and_batch():
	error = _client_error(
		"RequestThrottled", status=400, retry_attempts=3, operation="SendMessage"
	)

	class FailingSqs(FakeSqs):
		def send_message(self, **kwargs):
			self.send_calls.append(kwargs)
			raise error

		def send_message_batch(self, **kwargs):
			self.send_batch_calls.append(kwargs)
			raise error

	client, _mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	client.sqs = FailingSqs()
	with pytest.raises(ClientError):
		client.send_message(STANDARD_URL, "hello")
	with pytest.raises(ClientError):
		client.send_message_batch(
			STANDARD_URL,
			[{"Id": "1", "MessageBody": "one"}, {"Id": "2", "MessageBody": "two"}],
		)
	assert mock_s3.delete_calls == []
	assert len(mock_s3.objects) == 3
	cases = [fields["case"] for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert cases == [
		ORPHAN_AFTER_SQS_UNCERTAIN,
		ORPHAN_AFTER_SQS_UNCERTAIN,
		ORPHAN_AFTER_SQS_UNCERTAIN,
	]
	assert EVENT_MESSAGE_LEFT not in [name for name, _ in events]


def test_pre_send_validation_error_deletes_the_object():
	class RejectingSqs(FakeSqs):
		def send_message(self, **kwargs):
			self.send_calls.append(kwargs)
			raise ParamValidationError(report="DelaySeconds must be an integer")

		def send_message_batch(self, **kwargs):
			self.send_batch_calls.append(kwargs)
			raise ValidationError(value="x", param="DelaySeconds", type_name="integer")

	client, _mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	client.sqs = RejectingSqs()
	with pytest.raises(ParamValidationError):
		client.send_message(STANDARD_URL, "hello")
	assert mock_s3.objects == {}
	assert len(mock_s3.delete_calls) == 1
	assert EVENT_S3_OBJECT_ORPHANED not in [name for name, _ in events]

	class CleanupFails(FakeS3):
		def delete_object(self, **kwargs):
			self.delete_calls.append(kwargs)
			raise RuntimeError("cleanup down")

	client, _mock_sqs, _mock_s3, events = _events_of(always_through_s3=True)
	failing_s3 = CleanupFails()
	client.sqs = RejectingSqs()
	client.s3 = failing_s3
	with pytest.raises(ValidationError):
		client.send_message_batch(
			STANDARD_URL,
			[{"Id": "1", "MessageBody": "one"}, {"Id": "2", "MessageBody": "two"}],
		)
	assert len(failing_s3.delete_calls) == 2
	cases = [fields["case"] for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert cases == [ORPHAN_AFTER_UPLOAD_SQS_REJECTED, ORPHAN_AFTER_UPLOAD_SQS_REJECTED]
	assert ORPHAN_AFTER_SQS_UNCERTAIN not in cases


def test_4xx_without_retry_attempts_keeps_the_object():
	"""A 4xx is definite only when RetryAttempts is explicitly 0."""
	error = _client_error("InvalidParameterValue", status=400, operation="SendMessage")
	assert "RetryAttempts" not in error.response["ResponseMetadata"]

	class FailingSqs(FakeSqs):
		def send_message(self, **kwargs):
			self.send_calls.append(kwargs)
			raise error

	client, _mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	assert client._is_definite_sqs_rejection(error) is False
	client.sqs = FailingSqs()
	with pytest.raises(ClientError):
		client.send_message(STANDARD_URL, "hello")
	assert mock_s3.delete_calls == []
	assert len(mock_s3.objects) == 1
	cases = [fields["case"] for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert cases == [ORPHAN_AFTER_SQS_UNCERTAIN]


def test_credential_error_before_send_deletes_the_object_and_connection_error_does_not():
	"""Injected clients without meta.events keep the type fallback.

	A real botocore client records before-send and can see the same exception
	after attempt 1 was already sent. Those cases are covered separately.
	"""
	class NoCreds(FakeSqs):
		def send_message(self, **kwargs):
			self.send_calls.append(kwargs)
			raise NoCredentialsError()

	client, _mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	client.sqs = NoCreds()
	with pytest.raises(NoCredentialsError):
		client.send_message(STANDARD_URL, "hello")
	assert mock_s3.objects == {}
	assert len(mock_s3.delete_calls) == 1
	assert EVENT_S3_OBJECT_ORPHANED not in [name for name, _ in events]

	assert client._is_definite_sqs_rejection(
		PartialCredentialsError(provider="env", cred_var="aws_secret_access_key")
	) is False
	assert client._is_definite_sqs_rejection(UnknownCredentialError(name="env")) is True
	connection = EndpointConnectionError(endpoint_url="https://sqs.us-east-1.amazonaws.com/")
	assert client._is_definite_sqs_rejection(connection) is False

	class Unreachable(FakeSqs):
		def send_message(self, **kwargs):
			self.send_calls.append(kwargs)
			raise connection

	client, _mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	client.sqs = Unreachable()
	with pytest.raises(EndpointConnectionError):
		client.send_message(STANDARD_URL, "hello")
	assert mock_s3.delete_calls == []
	assert len(mock_s3.objects) == 1
	cases = [fields["case"] for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert cases == [ORPHAN_AFTER_SQS_UNCERTAIN]


def _refresh_failure_after_attempt_reached_sqs(error):
	"""boto3's retry loop lives inside one send call.

	Attempt 1 is recorded as having reached SQS. The error raised is the
	credential-refresh failure botocore surfaces while re-signing the retry.
	"""

	class RefreshFailsOnRetry(FakeSqs):
		def send_message(self, **kwargs):
			self.send_calls.append(kwargs)
			raise error

		def send_message_batch(self, **kwargs):
			self.send_batch_calls.append(kwargs)
			raise error

	return RefreshFailsOnRetry()


def _assert_refresh_failure_keeps_payloads(error):
	client, _mock_sqs, mock_s3, events = _events_of(always_through_s3=True)
	assert client._is_definite_sqs_rejection(error) is False
	failing = _refresh_failure_after_attempt_reached_sqs(error)
	client.sqs = failing
	with pytest.raises(type(error)):
		client.send_message(STANDARD_URL, "hello")
	with pytest.raises(type(error)):
		client.send_message_batch(
			STANDARD_URL,
			[{"Id": "1", "MessageBody": "one"}, {"Id": "2", "MessageBody": "two"}],
		)
	assert len(failing.send_calls) == 1
	assert len(failing.send_batch_calls) == 1
	assert mock_s3.delete_calls == []
	assert len(mock_s3.objects) == 3
	cases = [fields["case"] for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert cases == [
		ORPHAN_AFTER_SQS_UNCERTAIN,
		ORPHAN_AFTER_SQS_UNCERTAIN,
		ORPHAN_AFTER_SQS_UNCERTAIN,
	]


def test_credential_retrieval_error_after_attempt_one_keeps_the_object():
	_assert_refresh_failure_keeps_payloads(
		CredentialRetrievalError(provider="env", error_msg="refresh failed")
	)


def test_partial_credentials_error_after_attempt_one_keeps_the_object():
	_assert_refresh_failure_keeps_payloads(
		PartialCredentialsError(provider="env", cred_var="aws_secret_access_key")
	)


def test_sts_4xx_during_credential_refresh_keeps_the_object():
	_assert_refresh_failure_keeps_payloads(
		_client_error("AccessDenied", status=403, retry_attempts=0, operation="AssumeRole")
	)
