from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import urllib.error

from scripts import application_tracker as tracker


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = json.dumps(payload).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.payload


def http_error(code: int, payload: str = "error", headers=None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.airtable.com/test",
        code,
        payload,
        headers or {},
        io.BytesIO(payload.encode()),
    )


class JobInputTests(unittest.TestCase):
    def test_parse_job_envelope_with_body(self):
        url, body = tracker.parse_job_envelope(
            "---\nurl: 'HTTPS://Example.COM/jobs/42/?utm_source=x'\n---\n\nRole details\n"
        )
        self.assertEqual(url, "https://example.com/jobs/42")
        self.assertEqual(body, "Role details")

    def test_parse_job_envelope_allows_optional_body(self):
        url, body = tracker.parse_job_envelope("---\nurl: https://example.com/job\n---\n")
        self.assertEqual(url, "https://example.com/job")
        self.assertEqual(body, "")

    def test_resolved_snapshot_requires_body(self):
        with self.assertRaisesRegex(tracker.ValidationError, "no description body"):
            tracker.parse_job_envelope("---\nurl: https://example.com/job\n---\n", require_body=True)

    def test_missing_url_is_rejected(self):
        with self.assertRaisesRegex(tracker.ValidationError, "non-empty"):
            tracker.parse_job_envelope("---\nurl: null\n---\nDescription")

    def test_url_normalization_and_id_are_stable(self):
        first = "HTTPS://EXAMPLE.COM:443/jobs/1/?b=2&utm_campaign=x&a=1#apply"
        second = "https://example.com/jobs/1?a=1&b=2"
        self.assertEqual(tracker.normalize_url(first), tracker.normalize_url(second))
        self.assertEqual(tracker.application_id(first), tracker.application_id(second))

    def test_slugify(self):
        self.assertEqual(tracker.slugify("Développeur C++ / Cloud"), "developpeur-c-cloud")
        self.assertEqual(tracker.slugify("***"), "unknown")


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def make_manifest(self, **overrides) -> Path:
        url = "https://example.com/jobs/42"
        (self.root / "job.md").write_text(f"---\nurl: {url}\n---\n\nComplete job description\n", encoding="utf-8")
        (self.root / "resume.tex").write_text("\\documentclass{article}\\begin{document}CV\\end{document}\n")
        (self.root / "resume.pdf").write_bytes(b"%PDF-1.4\nnot-empty")
        manifest = {
            "schema_version": 1,
            "application_id": tracker.application_id(url),
            "job_url": url,
            "company": "Example",
            "role": "Engineer",
            "location": "Paris",
            "compensation": "",
            "created_at": "2026-10-31T12:00:00Z",
            "updated_at": "2026-10-31T12:01:00Z",
            "run_id": "20261031T120000Z",
            "job_path": "job.md",
            "tex_path": "resume.tex",
            "pdf_path": "resume.pdf",
            "verification_notes": ["Verify React usage"],
            "compile": {
                "runtime": "docker",
                "image": "docker.io/texlive/texlive:latest",
                "image_digest": "docker.io/texlive/texlive@sha256:abc",
                "pages": 1,
                "page_limit": 1,
            },
        }
        manifest.update(overrides)
        path = self.root / "application.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_valid_manifest_loads_artifacts(self):
        loaded = tracker.load_manifest(self.make_manifest())
        self.assertEqual(loaded["_job_body"], "Complete job description")
        self.assertEqual(loaded["_pdf_path"].name, "resume.pdf")

    def test_manifest_rejects_wrong_application_id(self):
        with self.assertRaisesRegex(tracker.ValidationError, "application_id must be"):
            tracker.load_manifest(self.make_manifest(application_id="wrong"))

    def test_manifest_rejects_missing_compile_proof(self):
        with self.assertRaisesRegex(tracker.ValidationError, "compilation metadata"):
            tracker.load_manifest(self.make_manifest(compile=None))

    def test_manifest_rejects_page_limit_failure(self):
        compile_info = {
            "runtime": "docker",
            "image": "image",
            "image_digest": "digest",
            "pages": 2,
            "page_limit": 1,
        }
        with self.assertRaisesRegex(tracker.ValidationError, "over the configured page limit"):
            tracker.load_manifest(self.make_manifest(compile=compile_info))

    def test_manifest_rejects_escaping_artifact_path(self):
        outside = self.root.parent / "outside.tex"
        outside.write_text("outside", encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)
        with self.assertRaisesRegex(tracker.ValidationError, "escapes"):
            tracker.load_manifest(self.make_manifest(tex_path="../outside.tex"))

    def test_manifest_rejects_oversized_pdf(self):
        path = self.make_manifest()
        with (self.root / "resume.pdf").open("wb") as pdf:
            pdf.truncate(tracker.MAX_ATTACHMENT_BYTES + 1)
        with self.assertRaisesRegex(tracker.ValidationError, "limited"):
            tracker.load_manifest(path)


class AirtableClientTests(unittest.TestCase):
    def test_configuration_is_required(self):
        with self.assertRaises(tracker.ConfigurationError):
            tracker.AirtableClient("", "base")

    def test_stored_credentials_support_unattended_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            credentials = Path(temporary) / "credentials.json"
            credentials.write_text(
                json.dumps({"token": "stored-token", "base_id": "stored-base", "table_name": "Stored Table"})
            )
            os.chmod(credentials, 0o600)
            with mock.patch.object(tracker, "CREDENTIALS_FILE", credentials), mock.patch.dict(
                os.environ, {}, clear=True
            ):
                client = tracker.AirtableClient.from_environment()
        self.assertEqual(client.token, "stored-token")
        self.assertEqual(client.base_id, "stored-base")
        self.assertEqual(client.table_name, "Stored Table")

    def test_environment_overrides_stored_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            credentials = Path(temporary) / "credentials.json"
            credentials.write_text(json.dumps({"token": "stored-token", "base_id": "stored-base"}))
            os.chmod(credentials, 0o600)
            environment = {"AIRTABLE_TOKEN": "env-token", "AIRTABLE_BASE_ID": "env-base"}
            with mock.patch.object(tracker, "CREDENTIALS_FILE", credentials), mock.patch.dict(
                os.environ, environment, clear=True
            ):
                client = tracker.AirtableClient.from_environment()
        self.assertEqual(client.token, "env-token")
        self.assertEqual(client.base_id, "env-base")

    @unittest.skipUnless(os.name == "posix", "POSIX permission check")
    def test_permissive_credential_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            credentials = Path(temporary) / "credentials.json"
            credentials.write_text(json.dumps({"token": "token", "base_id": "base"}))
            os.chmod(credentials, 0o644)
            with mock.patch.object(tracker, "CREDENTIALS_FILE", credentials), mock.patch.dict(
                os.environ, {}, clear=True
            ):
                with self.assertRaisesRegex(tracker.ConfigurationError, "owner-only"):
                    tracker.AirtableClient.from_environment()

    def test_persist_credentials_uses_owner_only_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            credentials = Path(temporary) / ".tracking" / "credentials.json"
            client = tracker.AirtableClient("token", "base", "Applications")
            with mock.patch.object(tracker, "CREDENTIALS_FILE", credentials):
                tracker.persist_credentials(client)
            payload = json.loads(credentials.read_text())
            self.assertEqual(payload["token"], "token")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(credentials.stat().st_mode), 0o600)

    def test_rate_limit_is_retried(self):
        responses = [http_error(429, headers={"Retry-After": "0"}), FakeResponse({"records": []})]
        sleeps = []

        def opener(request, timeout):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        client = tracker.AirtableClient("token", "base", opener=opener, sleeper=sleeps.append)
        self.assertIsNone(client.find_record("app_123"))
        self.assertEqual(sleeps, [0.0])

    def test_authentication_error_is_not_retried(self):
        calls = []

        def opener(request, timeout):
            calls.append(request)
            raise http_error(401, "unauthorized")

        client = tracker.AirtableClient("token", "base", opener=opener, sleeper=lambda _: None)
        with self.assertRaisesRegex(tracker.AirtableError, "401"):
            client.find_record("app_123")
        self.assertEqual(len(calls), 1)

    def test_setup_creates_missing_table(self):
        requests = []
        responses = [FakeResponse({"tables": []}), FakeResponse({"id": "tbl_new"})]

        def opener(request, timeout):
            requests.append(request)
            return responses.pop(0)

        client = tracker.AirtableClient("token", "base", opener=opener)
        self.assertEqual(client.setup(), "tbl_new")
        payload = json.loads(requests[1].data)
        self.assertEqual(payload["fields"][0]["name"], "Application ID")
        self.assertEqual(payload["fields"][0]["type"], "singleLineText")

    def test_setup_rejects_conflicting_primary_field(self):
        table = {
            "id": "tbl",
            "name": "Applications",
            "primaryFieldId": "fld_name",
            "fields": [{"id": "fld_name", "name": "Name", "type": "singleLineText"}],
        }
        client = tracker.AirtableClient(
            "token", "base", opener=lambda request, timeout: FakeResponse({"tables": [table]})
        )
        with self.assertRaisesRegex(tracker.ValidationError, "primary field"):
            client.setup()

    def test_pdf_upload_uses_base64_attachment_endpoint(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return FakeResponse({"id": "att"})

        client = tracker.AirtableClient("token", "base", opener=opener)
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "resume.pdf"
            pdf.write_bytes(b"%PDF")
            client.upload_pdf("rec123", pdf, "resume-run.pdf")
        request = requests[0]
        self.assertIn("/rec123/Resume%20PDF/uploadAttachment", request.full_url)
        payload = json.loads(request.data)
        self.assertEqual(payload["contentType"], "application/pdf")
        self.assertEqual(payload["filename"], "resume-run.pdf")
        self.assertEqual(payload["file"], "JVBERg==")


class FakeSyncClient:
    def __init__(self, existing=None):
        self.existing = existing
        self.created_fields = None
        self.updated_fields = None
        self.uploads = []

    def find_record(self, app_id):
        return self.existing

    def create_record(self, fields):
        self.created_fields = fields
        return {"id": "rec_new", "fields": fields}

    def update_record(self, record_id, fields):
        self.updated_fields = fields
        return {"id": record_id, "fields": fields}

    def upload_pdf(self, record_id, pdf_path, filename):
        self.uploads.append((record_id, pdf_path, filename))
        return {}


class SyncTests(ManifestTests):
    def test_new_record_is_draft_and_uploads_pdf(self):
        client = FakeSyncClient()
        record_id, created = tracker.sync_application(client, self.make_manifest())
        self.assertTrue(created)
        self.assertEqual(record_id, "rec_new")
        self.assertEqual(client.created_fields["Status"], "Draft")
        self.assertIn("\\documentclass", client.created_fields["Resume LaTeX"])
        self.assertEqual(client.uploads[0][2], "resume-20261031T120000Z.pdf")

    def test_existing_record_status_is_preserved(self):
        existing = {
            "id": "rec_existing",
            "fields": {"Status": "Applied", "Resume PDF": []},
        }
        client = FakeSyncClient(existing)
        tracker.sync_application(client, self.make_manifest())
        self.assertNotIn("Status", client.updated_fields)
        self.assertNotIn("Created At", client.updated_fields)

    def test_retry_does_not_duplicate_same_pdf(self):
        existing = {
            "id": "rec_existing",
            "fields": {
                "Status": "Draft",
                "Resume PDF": [{"filename": "resume-20261031T120000Z.pdf"}],
            },
        }
        client = FakeSyncClient(existing)
        tracker.sync_application(client, self.make_manifest())
        self.assertEqual(client.uploads, [])

    def test_failed_sync_can_be_queued_and_retried(self):
        manifest = self.make_manifest()
        outbox = self.root / ".tracking" / "outbox"
        with mock.patch.object(tracker, "OUTBOX", outbox), mock.patch.object(tracker, "REPO_ROOT", self.root):
            queued = tracker.queue_manifest(manifest, tracker.AirtableError("offline"))
            self.assertTrue(queued.is_file())
            succeeded, failed = tracker.retry_outbox(FakeSyncClient())
            self.assertEqual((succeeded, failed), (1, 0))
            self.assertFalse(queued.exists())


if __name__ == "__main__":
    unittest.main()
