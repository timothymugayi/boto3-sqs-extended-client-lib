# Binary and files

Text stays text. `bytes`, files, and binary file objects are stored and returned as binary, and large S3 bodies can be streamed to disk.

[README](../README.md) · [Configuration](configuration.md) · [IAM](iam.md) · [FIFO and redrive](fifo-and-redrive.md) · [Events](events.md) · Binary and files · [Java compatibility](java-compatibility.md) · [Migration](migration.md) · [Testing](testing.md)

<a id="large-payloads-and-files"></a>

### Large payloads and files

`send_message(queue_url, message)` treats every `str` as text, including a string that happens to be a path to a file on disk. Small messages stay in SQS. Messages over 256 KB (or `always_through_s3=True`) are stored in S3 as UTF-8. Receive of those messages, including ones sent by older versions of this library, returns a `str`. The Java `PayloadS3Pointer` format is unchanged.

`bytes` and binary file-like objects are binary. To upload a file, call `send_file(queue_url, path)` or pass a `pathlib.Path` (any `os.PathLike`). Those use `upload_file` / `upload_fileobj` (multipart above ~8 MB) and are not loaded into memory when they go to S3. Receive returns `bytes` for those payloads unless you pass `payload_dir`. Messages without `SQSExtendedContentType` stay text.

**0.4.0 migration:** code that passed an existing path string to `send_message` now sends that path as text. Switch those calls to `send_file` or `pathlib.Path`. `receive_message` on an empty queue returns `[]` instead of `None`.

```python
from pathlib import Path

from pysqs_extended_client import SQSClientExtended, ExtendedClientConfiguration

sqs = SQSClientExtended(
    aws_access_key_id,
    aws_secret_access_key,
    aws_region_name,
    config=ExtendedClientConfiguration(
        s3_bucket_name="my-payload-bucket",
        s3_key_prefix="payloads/",
        # optional: always_through_s3=True,
        # optional: message_size_threshold=262144,  # int, 1..262144; default 256 KiB
    ),
)

sqs.send_message(queue_url, "small body")
sqs.send_message(queue_url, b"\x00\x01\x02")
sqs.send_file(queue_url, "/path/to/large-video.mp4")
sqs.send_message(queue_url, Path("/path/to/large-video.mp4"))
with open("huge.zip", "rb") as handle:
    sqs.send_message(queue_url, handle)

messages = sqs.receive_message(queue_url, wait_time_seconds=10)
for message in messages:
    body = message["Body"]  # str for text, bytes for binary
    sqs.delete_message(queue_url, message["ReceiptHandle"])

# Stream a large S3 payload to disk instead of holding it in memory.
# Body and _payload_path are the file path. Inline messages are unchanged.
large = sqs.receive_message(queue_url, payload_dir="/tmp/sqs-payloads")
```

SQS still has a 256 KB **message** limit; only the payload bytes go to S3. The default receive path loads the payload into memory, same as the Java client. That is a poor fit for multi-hundred-MB objects: pass `payload_dir` (or `get_payload_from_s3(..., filename=...)`) so boto3 streams the object to a file.

The 0.4.0 path-string and empty-queue notes are also in [Migration](migration.md#v040).
