"""before-send decides whether a non-ClientError is a definite SQS rejection.

The SQS client here is a real botocore client. Attempt 1 hits a local HTTP
server; the retry re-signs and the refresh callback raises.
"""
import datetime
import gc
import threading
import weakref
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

import boto3
import botocore.session
import pytest
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import (
	ClientError,
	NoCredentialsError,
	ParamValidationError,
	ReadTimeoutError,
)

from pysqs_extended_client.SQSClientExtended import (
	EVENT_S3_OBJECT_ORPHANED,
	ORPHAN_AFTER_SQS_UNCERTAIN,
	SQSClientExtended,
	_SEND_ATTEMPT_TRACKERS,
	_SEND_ATTEMPT_TRACKERS_LOCK,
	_SQS_SEND_ATTEMPT_HOOK_PREFIX,
	_tracker_for,
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
		if self.server.hold_first and len(self.server.hits) == 1:
			self.server.first_hit.set()
			self.server.allow_response.wait(timeout=5)
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
def _sqs_server(status, hold_first=False):
	server = HTTPServer(("127.0.0.1", 0), _Handler)
	server.status = status
	server.hits = []
	server.hold_first = hold_first
	server.first_hit = threading.Event()
	server.allow_response = threading.Event()
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


def _extended_client(server, refresh, events, max_attempts=2, read_timeout=2):
	botocore_session = botocore.session.get_session()
	botocore_session._credentials = _credentials(refresh)
	sqs = boto3.Session(botocore_session=botocore_session).client(
		"sqs",
		region_name="us-east-1",
		endpoint_url="http://127.0.0.1:%d" % server.server_address[1],
		config=Config(
			retries={"max_attempts": max_attempts, "mode": "standard"},
			connect_timeout=2,
			read_timeout=read_timeout,
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


def _bare_sqs():
	return boto3.client(
		"sqs",
		region_name="us-east-1",
		aws_access_key_id="AKIAtesttesttest",
		aws_secret_access_key="secretsecretsecret",
		endpoint_url="http://127.0.0.1:9",
	)


def _wrapped(sqs, events=None):
	return SQSClientExtended(
		sqs_client=sqs,
		s3_client=FakeS3(),
		config=ExtendedClientConfiguration(
			s3_bucket_name="test-bucket",
			always_through_s3=True,
			on_event=None if events is None else (lambda name, **fields: events.append((name, fields))),
		),
	)


def _before_send_hook_ids(sqs):
	handlers = sqs.meta.events._emitter._unique_id_handlers
	return sorted(key for key in handlers if str(key).startswith(_SQS_SEND_ATTEMPT_HOOK_PREFIX))


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
	sqs = _bare_sqs()
	client = _wrapped(sqs)
	client._register_sqs_send_attempt_hook()
	client._reset_sqs_send_attempts()
	client.sqs.meta.events.emit("before-send.sqs.SendMessage", request=None)
	client.sqs.meta.events.emit("before-send.sqs.SendMessageBatch", request=None)
	assert client._sqs_send_attempt_count() == 2
	client.sqs.meta.events.emit("before-send.sqs.GetQueueAttributes", request=None)
	assert client._sqs_send_attempt_count() == 2
	assert _before_send_hook_ids(sqs) == ["pysqs-extended-client-before-send"]


def test_before_send_count_stays_on_the_calling_thread():
	sqs = _bare_sqs()
	first = _wrapped(sqs)
	second = _wrapped(sqs)
	assert first._send_attempt_tracker is second._send_attempt_tracker
	barrier = threading.Barrier(2)
	results = {}

	def run(client, name, reset):
		client._reset_sqs_send_attempts()
		barrier.wait()
		client.sqs.meta.events.emit("before-send.sqs.SendMessage", request=None)
		barrier.wait()
		if reset:
			client._reset_sqs_send_attempts()
		barrier.wait()
		results[name] = client._sqs_send_was_attempted()

	threads = [
		threading.Thread(target=run, args=(first, "reset", True)),
		threading.Thread(target=run, args=(second, "keep", False)),
	]
	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join()
	assert results == {"reset": False, "keep": True}


@pytest.mark.parametrize("operation", ["send_message", "send_message_batch"])
def test_magicmock_read_timeout_keeps_the_object(operation):
	"""A mock register never emits, so a timeout must stay uncertain."""
	sqs = MagicMock()
	error = ReadTimeoutError(endpoint_url="https://sqs.us-east-1.amazonaws.com/")
	getattr(sqs, operation).side_effect = error
	events = []
	client = _wrapped(sqs, events)
	s3 = client.s3
	expected_objects = _offloaded_count(operation)
	assert client._sqs_attempt_hook_registered is False
	assert sqs.meta.events.register.call_count == 0
	with pytest.raises(ReadTimeoutError):
		_send(client, operation)
	assert s3.delete_calls == []
	assert len(s3.objects) == expected_objects
	assert client._sqs_send_was_attempted() is False
	assert client._sqs_send_attempt_count() == 0
	cases = [fields["case"] for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert cases == [ORPHAN_AFTER_SQS_UNCERTAIN] * expected_objects
	client.close()
	client.close()


def test_botocore_client_with_replaced_events_keeps_a_timeout():
	"""BaseClient alone is not enough; meta.events must be a real emitter."""
	sqs = _bare_sqs()
	sqs.meta.events = MagicMock()

	def send_message(**kwargs):
		raise ReadTimeoutError(endpoint_url="https://sqs.us-east-1.amazonaws.com/")

	sqs.send_message = send_message
	events = []
	client = _wrapped(sqs, events)
	assert client._sqs_attempt_hook_registered is False
	assert sqs.meta.events.register.call_count == 0
	with pytest.raises(ReadTimeoutError):
		client.send_message(QUEUE_URL, "hello")
	assert client.s3.delete_calls == []
	assert len(client.s3.objects) == 1
	cases = [fields["case"] for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert cases == [ORPHAN_AFTER_SQS_UNCERTAIN]
	client.close()


def test_close_leaves_the_shared_before_send_handler():
	sqs = _bare_sqs()
	first = _wrapped(sqs)
	second = _wrapped(sqs)
	assert first._send_attempt_tracker is second._send_attempt_tracker
	assert _before_send_hook_ids(sqs) == ["pysqs-extended-client-before-send"]
	first.close()
	second.close()
	assert first._sqs_attempt_hook_registered is True
	assert first._send_attempt_tracker is second._send_attempt_tracker
	assert _before_send_hook_ids(sqs) == ["pysqs-extended-client-before-send"]
	first._reset_sqs_send_attempts()
	sqs.meta.events.emit("before-send.sqs.SendMessage", request=None)
	assert first._sqs_send_was_attempted() is True
	assert second._sqs_send_was_attempted() is True


def test_extended_clients_sharing_one_sqs_client_are_collected():
	sqs = _bare_sqs()
	refs = []
	for _ in range(200):
		client = _wrapped(sqs)
		refs.append(weakref.ref(client))
	del client
	hook_ids = _before_send_hook_ids(sqs)
	assert hook_ids == ["pysqs-extended-client-before-send"]
	handlers = sqs.meta.events._emitter._unique_id_handlers
	for hook_id in hook_ids:
		assert not isinstance(handlers[hook_id]["handler"].__self__, SQSClientExtended)
	gc.collect()
	assert [ref() for ref in refs] == [None] * len(refs)
	assert _before_send_hook_ids(sqs) == ["pysqs-extended-client-before-send"]


def _assert_refresh_retry_kept_the_object(server, state, client, s3, events, operation):
	expected_objects = _offloaded_count(operation)
	assert server.hits == ["/"]
	assert state["calls"] == 2
	assert client._sqs_attempt_hook_registered is True
	assert client._send_attempt_tracker is not None
	assert client._sqs_send_was_attempted() is True
	assert s3.delete_calls == []
	assert len(s3.objects) == expected_objects
	assert _before_send_hook_ids(client.sqs) == ["pysqs-extended-client-before-send"]
	cases = [fields["case"] for name, fields in events if name == EVENT_S3_OBJECT_ORPHANED]
	assert cases == [ORPHAN_AFTER_SQS_UNCERTAIN] * expected_objects


@pytest.mark.parametrize("operation", ["send_message", "send_message_batch"])
def test_send_after_close_keeps_the_object_when_refresh_fails_on_retry(operation):
	state = {"calls": 0, "fail_immediately": False, "error": NoCredentialsError()}
	events = []
	with _sqs_server(500) as server:
		client, s3 = _extended_client(server, _refresh(state), events)
		client.close()
		with pytest.raises(NoCredentialsError):
			_send(client, operation)
		_assert_refresh_retry_kept_the_object(server, state, client, s3, events, operation)


@pytest.mark.parametrize("operation", ["send_message", "send_message_batch"])
def test_close_from_another_thread_during_send_keeps_the_object(operation):
	state = {"calls": 0, "fail_immediately": False, "error": NoCredentialsError()}
	events = []
	with _sqs_server(500, hold_first=True) as server:
		client, s3 = _extended_client(server, _refresh(state), events, read_timeout=10)

		def close_after_attempt():
			assert server.first_hit.wait(timeout=5)
			client.close()
			server.allow_response.set()

		closer = threading.Thread(target=close_after_attempt)
		closer.start()
		try:
			with pytest.raises(NoCredentialsError):
				_send(client, operation)
		finally:
			server.allow_response.set()
			closer.join(timeout=5)
		assert not closer.is_alive()
		_assert_refresh_retry_kept_the_object(server, state, client, s3, events, operation)


def test_unhashable_sqs_client_still_records_before_send():
	sqs = _bare_sqs()

	class _Eq(type(sqs)):
		def __eq__(self, other):
			return self is other

	sqs.__class__ = _Eq
	with pytest.raises(TypeError):
		hash(sqs)
	first = _wrapped(sqs)
	second = _wrapped(sqs)
	assert first._sqs_attempt_hook_registered is True
	assert first._send_attempt_tracker is second._send_attempt_tracker
	first._reset_sqs_send_attempts()
	sqs.meta.events.emit("before-send.sqs.SendMessage", request=None)
	assert first._sqs_send_was_attempted() is True
	assert second._sqs_send_was_attempted() is True
	assert _before_send_hook_ids(sqs) == ["pysqs-extended-client-before-send"]
	first.close()
	assert first._sqs_attempt_hook_registered is True
	assert _before_send_hook_ids(sqs) == ["pysqs-extended-client-before-send"]


def test_cyclic_client_finalizer_does_not_deadlock_under_the_tracker_lock():
	sqs = _bare_sqs()
	wrapped = _wrapped(sqs)
	client_id = id(sqs)
	assert client_id in _SEND_ATTEMPT_TRACKERS
	cycle = [sqs]
	sqs._test_cycle = cycle
	# Cyclic garbage is not collected while the GC is disabled, so the
	# finalizer cannot run until this thread holds the tracker lock.
	worker = None
	done = {}
	gc.disable()
	try:
		del wrapped
		del sqs
		del cycle
		assert client_id in _SEND_ATTEMPT_TRACKERS

		def collect_while_holding_the_lock():
			_SEND_ATTEMPT_TRACKERS_LOCK.acquire()
			try:
				gc.collect()
				_tracker_for(_bare_sqs())
				done["ok"] = client_id not in _SEND_ATTEMPT_TRACKERS
			finally:
				_SEND_ATTEMPT_TRACKERS_LOCK.release()

		worker = threading.Thread(target=collect_while_holding_the_lock)
		worker.daemon = True
		worker.start()
		worker.join(timeout=5)
	finally:
		gc.enable()
	assert worker is not None and not worker.is_alive()
	assert done.get("ok") is True


def test_tracker_is_dropped_when_the_sqs_client_is_collected():
	sqs = _bare_sqs()
	client = _wrapped(sqs)
	client_id = id(sqs)
	assert client_id in _SEND_ATTEMPT_TRACKERS
	del client
	del sqs
	gc.collect()
	assert client_id not in _SEND_ATTEMPT_TRACKERS
