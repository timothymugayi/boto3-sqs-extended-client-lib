import threading

from botocore.exceptions import ClientError

from pysqs_extended_client.SQSClientExtended import SQSClientExtended
from pysqs_extended_client.extended_config import ExtendedClientConfiguration


PAYLOAD_S3_POINTER_CLASS = "software.amazon.payloadoffloading.PayloadS3Pointer"
LEGACY_PAYLOAD_S3_POINTER_CLASS = "com.amazon.sqs.javamessaging.MessageS3Pointer"
EMBEDDED_HANDLE = (
	"-..s3BucketName..-test-bucket-..s3BucketName..-"
	"-..s3Key..-abc-key-..s3Key..-orig-handle"
)


class FakeBody:
	def __init__(self, payload):
		self._payload = payload if isinstance(payload, bytes) else payload.encode("utf-8")

	def read(self):
		return self._payload


class FakeS3:
	def __init__(self, sleep_seconds=0):
		self.objects = {}
		self.put_calls = []
		self.get_calls = []
		self.delete_calls = []
		self.sleep_seconds = sleep_seconds
		self._lock = threading.Lock()

	def put_object(self, **kwargs):
		if self.sleep_seconds:
			import time
			time.sleep(self.sleep_seconds)
		with self._lock:
			self.put_calls.append(kwargs)
			body = kwargs["Body"]
			self.objects[(kwargs["Bucket"], kwargs["Key"])] = body
		return {}

	def upload_fileobj(self, Fileobj, Bucket, Key, ExtraArgs=None, Callback=None, Config=None):
		return self.put_object(Bucket=Bucket, Key=Key, Body=Fileobj.read())

	def get_object(self, **kwargs):
		if self.sleep_seconds:
			import time
			time.sleep(self.sleep_seconds)
		with self._lock:
			self.get_calls.append(kwargs)
			key = (kwargs["Bucket"], kwargs["Key"])
			if key not in self.objects:
				raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "Not found"}}, "GetObject")
			payload = self.objects[key]
		return {"Body": FakeBody(payload)}

	def download_fileobj(self, Bucket, Key, Fileobj, ExtraArgs=None, Callback=None, Config=None):
		resp = self.get_object(Bucket=Bucket, Key=Key)
		Fileobj.write(resp["Body"].read())

	def delete_object(self, **kwargs):
		if self.sleep_seconds:
			import time
			time.sleep(self.sleep_seconds)
		with self._lock:
			self.delete_calls.append(kwargs)
			self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)
		return {}


class FakeSqs:
	def __init__(self):
		self.send_calls = []
		self.send_batch_calls = []
		self.receive_calls = []
		self.receive_response = {"Messages": []}
		self.delete_calls = []
		self.delete_batch_calls = []
		self.visibility_calls = []
		self.visibility_batch_calls = []
		self.purge_calls = []
		self.create_queue_calls = []
		self.get_queue_url_calls = []

	def send_message(self, **kwargs):
		self.send_calls.append(kwargs)
		return {"MessageId": "mid"}

	def send_message_batch(self, **kwargs):
		self.send_batch_calls.append(kwargs)
		return {"Successful": [], "Failed": []}

	def receive_message(self, **kwargs):
		self.receive_calls.append(kwargs)
		return self.receive_response

	def delete_message(self, **kwargs):
		self.delete_calls.append(kwargs)
		return {}

	def delete_message_batch(self, **kwargs):
		self.delete_batch_calls.append(kwargs)
		return {"Successful": [], "Failed": []}

	def change_message_visibility(self, **kwargs):
		self.visibility_calls.append(kwargs)
		return {}

	def change_message_visibility_batch(self, **kwargs):
		self.visibility_batch_calls.append(kwargs)
		return {"Successful": [], "Failed": []}

	def purge_queue(self, **kwargs):
		self.purge_calls.append(kwargs)
		return {}

	def create_queue(self, **kwargs):
		self.create_queue_calls.append(kwargs)
		return {"QueueUrl": "https://sqs.example/" + kwargs.get("QueueName", "q")}

	def get_queue_url(self, **kwargs):
		self.get_queue_url_calls.append(kwargs)
		return {"QueueUrl": "https://sqs.example/" + kwargs.get("QueueName", "q")}


def make_client(**config_kwargs):
	config_kwargs.setdefault("s3_bucket_name", "test-bucket")
	mock_sqs = FakeSqs()
	mock_s3 = FakeS3()
	client = SQSClientExtended(
		sqs_client=mock_sqs,
		s3_client=mock_s3,
		config=ExtendedClientConfiguration(**config_kwargs),
	)
	return client, mock_sqs, mock_s3
