import json
import time

from pysqs_extended_client.SQSClientExtended import SQSClientExtended
from pysqs_extended_client.extended_config import ExtendedClientConfiguration
from tests.fakes import FakeS3, FakeSqs, PAYLOAD_S3_POINTER_CLASS, make_client


def test_send_batch_offloads_in_parallel():
	mock_sqs = FakeSqs()
	mock_s3 = FakeS3(sleep_seconds=0.12)
	client = SQSClientExtended(
		sqs_client=mock_sqs,
		s3_client=mock_s3,
		config=ExtendedClientConfiguration(
			s3_bucket_name="test-bucket",
			always_through_s3=True,
			s3_max_concurrency=3,
		),
	)
	started = time.time()
	client.send_message_batch(
		"https://sqs.example/queue",
		[
			{"Id": "1", "MessageBody": "one"},
			{"Id": "2", "MessageBody": "two"},
			{"Id": "3", "MessageBody": "three"},
		],
	)
	elapsed = time.time() - started
	assert len(mock_s3.put_calls) == 3
	assert len(mock_sqs.send_batch_calls) == 1
	assert elapsed < 0.28


def test_send_batch_fails_entire_batch_when_one_offload_fails():
	class FailingS3(FakeS3):
		def put_object(self, **kwargs):
			with self._lock:
				self.put_calls.append("attempt")
				should_fail = sum(1 for item in self.put_calls if item == "attempt") >= 2
			if should_fail:
				raise RuntimeError("s3 down")
			return FakeS3.put_object(self, **kwargs)

	mock_sqs = FakeSqs()
	mock_s3 = FailingS3()
	client = SQSClientExtended(
		sqs_client=mock_sqs,
		s3_client=mock_s3,
		config=ExtendedClientConfiguration(s3_bucket_name="test-bucket", always_through_s3=True),
	)
	try:
		client.send_message_batch(
			"https://sqs.example/queue",
			[
				{"Id": "1", "MessageBody": "one"},
				{"Id": "2", "MessageBody": "two"},
			],
		)
		raised = False
	except RuntimeError:
		raised = True
	assert raised
	assert mock_sqs.send_batch_calls == []


def test_receive_hydrates_multiple_pointers():
	client, mock_sqs, mock_s3 = make_client()
	mock_s3.objects[("test-bucket", "k1")] = b"body-one"
	mock_s3.objects[("test-bucket", "k2")] = b"body-two"
	mock_s3.objects[("test-bucket", "k3")] = b"body-three"
	messages = []
	for key, handle in (("k1", "h1"), ("k2", "h2"), ("k3", "h3")):
		messages.append({
			"Body": json.dumps([
				PAYLOAD_S3_POINTER_CLASS,
				{"s3BucketName": "test-bucket", "s3Key": key},
			]),
			"ReceiptHandle": handle,
			"MessageAttributes": {
				"SQSLargePayloadSize": {"StringValue": "4", "DataType": "Number"},
			},
		})
	mock_sqs.receive_response = {"Messages": messages}
	result = client.receive_message("https://sqs.example/queue", max_number_of_messages=3)
	assert [item["Body"] for item in result] == ["body-one", "body-two", "body-three"]
	assert len(mock_s3.get_calls) == 3


def test_delete_batch_issues_one_sqs_batch_then_parallel_s3_deletes():
	client, mock_sqs, mock_s3 = make_client()
	handles = []
	for key in ("a", "b", "c"):
		mock_s3.objects[("test-bucket", key)] = b"x"
		handles.append({
			"Id": key,
			"ReceiptHandle": (
				"-..s3BucketName..-test-bucket-..s3BucketName..-"
				"-..s3Key..-{}-..s3Key..-orig-{}".format(key, key)
			),
		})
	client.delete_message_batch("https://sqs.example/queue", handles)
	assert len(mock_s3.delete_calls) == 3
	assert len(mock_sqs.delete_batch_calls) == 1


def test_multipart_threshold_round_trip_keeps_java_pointer():
	client, mock_sqs, mock_s3 = make_client(always_through_s3=True, multipart_threshold=8)
	payload = "x" * 16
	client.send_message("https://sqs.example/queue", payload)
	body = json.loads(mock_sqs.send_calls[0]["MessageBody"])
	assert body[0] == PAYLOAD_S3_POINTER_CLASS
	key = body[1]["s3Key"]
	assert mock_s3.objects[("test-bucket", key)] == payload.encode("utf-8")


def test_on_event_emits_offload_metrics():
	events = []
	client, _, _ = make_client(
		always_through_s3=True,
		on_event=lambda name, **fields: events.append((name, fields)),
	)
	client.send_message("https://sqs.example/queue", "hello")
	names = [name for name, _ in events]
	assert "s3_offload" in names
	offload = dict(events)["s3_offload"]
	assert offload["bytes"] == 5
	assert "s3_key" in offload
	assert "duration_ms" in offload
