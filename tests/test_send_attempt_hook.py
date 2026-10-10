"""before-send decides whether a non-ClientError is a definite SQS rejection.

The SQS client here is a real botocore client. Attempt 1 hits a local HTTP
server; the retry re-signs and the refresh callback raises.
"""
import datetime
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import boto3
import botocore.session
import pytest
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import ClientError, NoCredentialsError, ParamValidationError

from pysqs_extended_client.SQSClientExtended import (
	EVENT_S3_OBJECT_ORPHANED,
	ORPHAN_AFTER_SQS_UNCERTAIN,
	SQSClientExtended,
)
from pysqs_extended_client.extended_config import ExtendedClientConfiguration
from tests.fakes import FakeS3


QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/queue"
_ERROR_XML = (
	b'<?xml version="1.0"?>'
	b"<ErrorResponse><Error><Type>%s</Type><Code>%s</Code>"
	b"<Message>stub</Message></Error><RequestId>req</RequestId></ErrorResponse>"
)


class _Handler(BaseHTTPRequestHandler):
	def do_POST(self):
		length = int(self.headers.get("Content-Length", "0"))
		if length:
			self.rfile.read(length)
		self.server.hits.append(self.path)
		status = self.server.status
		if status >= 500:
			body = _ERROR_XML % (b"Receiver", b"InternalError")
		else:
			body = _ERROR_XML % (b"Sender", b"InvalidParameterValue")
		self.send_response(status)
		self.send_header("Content-Type", "text/xml")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def log_message(self, format, *args):
		return


@contextmanager
def _sqs_server(status):
	server = HTTPServer(("127.0.0.1", 0), _Handler)
	server.status = status
	server.hits = []
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	try:
		yield server
	finally:
		server.shutdown()
		server.server_close()
		thread.join(timeout=5)


def _credentials(refresh):
	expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=30)
	# Timeouts larger than the credential lifetime force a refresh on every sign,
	# including the re-sign botocore does before each retry.
	return RefreshableCredentials(
		"AKIAtesttesttest",
		"secretsecretsecret",
		"token",
		expiry,
		refresh,
		method="custom-test",
		advisory_timeout=10 ** 9,
		mandatory_timeout=10 ** 9,
	)


def _extended_client(server, refresh, events, max_attempts=2):
	botocore_session = botocore.session.get_session()
	botocore_session._credentials = _credentials(refresh)
	sqs = boto3.Session(botocore_session=botocore_session).client(
		"sqs",
		region_name="us-east-1",
		endpoint_url="http://127.0.0.1:%d" % server.server_address[1],
		config=Config(
			retries={"max_attempts": max_attempts, "mode": "standard"},
			connect_timeout=2,
			read_timeout=2,
		),
	)
	s3 = FakeS3()
	client = SQSClientExtended(
		sqs_client=sqs,
		s3_client=s3,
		config=ExtendedClientConfiguration(
			s3_bucket_name="test-bucket",
			always_through_s3=True,
			on_event=lambda name, **fields: events.append((name, fields)),
		),
	)
	return client, s3


def _refresh(state):
	def refresh():
		state["calls"] += 1
		if state["fail_immediately"] or state["calls"] > 1:
			raise state["error"]
		expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=30)
		return {
			"access_key": "AKIAtesttesttest",
			"secret_key": "secretsecretsecret",
			"token": "token",
			"expiry_time": expiry.isoformat(),
		}

	return refresh


def _offloaded_count(operation):
	if operation == "send_message":
		return 1
	return 2


def _send(client, operation):
	if operation == "send_message":
		client.send_message(QUEUE_URL, "hello")
		return
	client.send_message_batch(
		QUEUE_URL,
		[{"Id": "1", "MessageBody": "one"}, {"Id": "2", "MessageBody": "two"}],
	)


@pytest.mark.parametrize("operation", ["send_message", "send_message_batch"])
@pytest.mark.parametrize(
	"error",
	[
		NoCredentialsError(),
		ParamValidationError(report="refresh callback"),
	],
)
def test_refresh_callback_error_on_retry_keeps_the_object(operation, error):
	state = {"calls": 0, "fail_immediately": False, "error": error}
	events = []
	with _sqs_server(500) as server:
		client, s3 = _extended_client(server, _refresh(state), events)
		expected_objects = _offloaded_count(operation)
		with pytest.raises(type(error)):
			_send(client, operation)
		assert server.hits == ["/"]
		assert state["calls"] == 2
		assert client._sqs_send_was_attempted() is True
		assert s3.delete_calls == []
		assert len(s3.objects) == expected_objects
		cases = [fields["case"] for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
		assert cases == [ORPHAN_AFTER_SQS_UNCERTAIN] * expected_objects


@pytest.mark.parametrize("operation", ["send_message", "send_message_batch"])
@pytest.mark.parametrize(
	"error",
	[
		NoCredentialsError(),
		ParamValidationError(report="before first attempt"),
	],
)
def test_refresh_callback_error_before_any_attempt_deletes_the_object(operation, error):
	state = {"calls": 0, "fail_immediately": True, "error": error}
	events = []
	with _sqs_server(500) as server:
		client, s3 = _extended_client(server, _refresh(state), events)
		expected_objects = _offloaded_count(operation)
		with pytest.raises(type(error)):
			_send(client, operation)
		assert server.hits == []
		assert state["calls"] == 1
		assert client._sqs_send_was_attempted() is False
		assert len(s3.delete_calls) == expected_objects
		assert s3.objects == {}
		assert EVENT_S3_OBJECT_ORPHANED not in [name for name, _ in events]


def test_presend_param_validation_deletes_the_object():
	"""botocore rejects a bad DelaySeconds type before before-send."""
	state = {"calls": 0, "fail_immediately": False, "error": NoCredentialsError()}
	events = []
	with _sqs_server(500) as server:
		client, s3 = _extended_client(server, _refresh(state), events)
		with pytest.raises(ParamValidationError):
			client.send_message(QUEUE_URL, "hello", DelaySeconds="abc")
		assert server.hits == []
		assert client._sqs_send_was_attempted() is False
		assert s3.objects == {}
		assert len(s3.delete_calls) == 1
		assert EVENT_S3_OBJECT_ORPHANED not in [name for name, _ in events]


def test_sqs_400_after_before_send_still_deletes_the_object():
	state = {"calls": 0, "fail_immediately": False, "error": NoCredentialsError()}
	events = []
	with _sqs_server(400) as server:
		client, s3 = _extended_client(server, _refresh(state), events)
		with pytest.raises(ClientError):
			client.send_message(QUEUE_URL, "hello")
		assert server.hits == ["/"]
		assert client._sqs_send_was_attempted() is True
		assert s3.objects == {}
		assert len(s3.delete_calls) == 1
		assert EVENT_S3_OBJECT_ORPHANED not in [name for name, _ in events]


def test_attempt_flag_resets_before_the_next_send():
	state = {"calls": 0, "fail_immediately": False, "error": NoCredentialsError()}
	events = []
	with _sqs_server(500) as server:
		client, s3 = _extended_client(server, _refresh(state), events)
		with pytest.raises(NoCredentialsError):
			client.send_message(QUEUE_URL, "hello")
		kept = set(s3.objects)
		assert len(kept) == 1
		assert server.hits == ["/"]
		state["fail_immediately"] = True
		with pytest.raises(NoCredentialsError):
			client.send_message(QUEUE_URL, "again")
		assert server.hits == ["/"]
		assert set(s3.objects) == kept
		assert len(s3.delete_calls) == 1
		cases = [fields["case"] for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
		assert cases == [ORPHAN_AFTER_SQS_UNCERTAIN]


def test_before_send_hook_is_registered_once_per_operation():
	sqs = boto3.client(
		"sqs",
		region_name="us-east-1",
		aws_access_key_id="AKIAtesttesttest",
		aws_secret_access_key="secretsecretsecret",
		endpoint_url="http://127.0.0.1:9",
	)
	client = SQSClientExtended(
		sqs_client=sqs,
		s3_client=FakeS3(),
		config=ExtendedClientConfiguration(s3_bucket_name="test-bucket"),
	)
	client._register_sqs_send_attempt_hook()
	client._reset_sqs_send_attempts()
	client.sqs.meta.events.emit("before-send.sqs.SendMessage", request=None)
	client.sqs.meta.events.emit("before-send.sqs.SendMessageBatch", request=None)
	assert client._sqs_send_attempts.count == 2


def test_before_send_count_stays_on_the_calling_thread():
	sqs = boto3.client(
		"sqs",
		region_name="us-east-1",
		aws_access_key_id="AKIAtesttesttest",
		aws_secret_access_key="secretsecretsecret",
		endpoint_url="http://127.0.0.1:9",
	)
	client = SQSClientExtended(
		sqs_client=sqs,
		s3_client=FakeS3(),
		config=ExtendedClientConfiguration(s3_bucket_name="test-bucket"),
	)
	barrier = threading.Barrier(2)
	results = {}

	def run(name, reset):
		client._reset_sqs_send_attempts()
		barrier.wait()
		client.sqs.meta.events.emit("before-send.sqs.SendMessage", request=None)
		barrier.wait()
		if reset:
			client._reset_sqs_send_attempts()
		barrier.wait()
		results[name] = client._sqs_send_was_attempted()

	threads = [
		threading.Thread(target=run, args=("reset", True)),
		threading.Thread(target=run, args=("keep", False)),
	]
	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join()
	assert results == {"reset": False, "keep": True}
