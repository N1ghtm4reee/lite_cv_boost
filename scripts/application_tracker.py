#!/usr/bin/env python3
"""Provision and update the Airtable application tracker.

This module intentionally uses only Python's standard library. Resume generation
remains agent-driven; this program validates the generated manifest and performs
the deterministic persistence step.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


API_ROOT = "https://api.airtable.com/v0"
CONTENT_ROOT = "https://content.airtable.com/v0"
DEFAULT_TABLE = "Applications"
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
REPO_ROOT = Path(__file__).resolve().parents[1]
OUTBOX = REPO_ROOT / ".tracking" / "outbox"
CREDENTIALS_FILE = REPO_ROOT / ".tracking" / "airtable_credentials.json"
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "msclkid"}
STATUS_CHOICES = ("Draft", "Applied", "Interview", "Offer", "Rejected", "Withdrawn")


class TrackerError(Exception):
    """Base class for actionable tracker errors."""


class ValidationError(TrackerError):
    """The local input or generated artifact is invalid."""


class ConfigurationError(TrackerError):
    """Required local configuration is absent."""


class AirtableError(TrackerError):
    """Airtable rejected or could not complete a request."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _strip_yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def normalize_url(raw_url: str) -> str:
    """Return a stable posting URL while retaining meaningful query parameters."""
    raw_url = raw_url.strip()
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValidationError("job URL must be an absolute http(s) URL")

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("job URL contains an invalid port") from exc
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")

    query_items = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        query_items.append((key, value))
    query = urllib.parse.urlencode(sorted(query_items))
    return urllib.parse.urlunsplit((scheme, host, path, query, ""))


def application_id(job_url: str) -> str:
    digest = hashlib.sha256(normalize_url(job_url).encode("utf-8")).hexdigest()[:16]
    return f"app_{digest}"


def slugify(value: str, *, fallback: str = "unknown") -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug or fallback


def parse_job_envelope(text: str, *, require_body: bool = False) -> tuple[str, str]:
    """Parse the deliberately small job_description.md front-matter contract."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValidationError("job input must begin with YAML front matter ('---')")
    try:
        closing = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValidationError("job input front matter is not closed") from exc

    url_value: str | None = None
    for line in lines[1:closing]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.fullmatch(r"url\s*:\s*(.*)", stripped, flags=re.IGNORECASE)
        if match:
            url_value = _strip_yaml_scalar(match.group(1))

    if not url_value or url_value.lower() in {"null", "none", "~"}:
        raise ValidationError("job input requires a non-empty front-matter 'url'")
    normalized = normalize_url(url_value)
    body = "\n".join(lines[closing + 1 :]).strip()
    if require_body and not body:
        raise ValidationError("resolved job snapshot has no description body")
    return normalized, body


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"manifest field '{field}' must be a non-empty ISO timestamp")
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"manifest field '{field}' is not an ISO timestamp") from exc
    return value


def _artifact_path(manifest_path: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"manifest field '{field}' must be a relative path")
    relative = Path(value)
    if relative.is_absolute():
        raise ValidationError(f"manifest field '{field}' must be relative to application.json")
    root = manifest_path.parent.resolve()
    resolved = (root / relative).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValidationError(f"manifest field '{field}' escapes its run directory")
    if not resolved.is_file():
        raise ValidationError(f"artifact for '{field}' does not exist: {resolved}")
    return resolved


def load_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path).resolve()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read manifest {manifest_path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValidationError("application.json must use schema_version 1")

    for field in ("company", "role", "run_id"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValidationError(f"manifest field '{field}' must be a non-empty string")
    for field in ("location", "compensation"):
        if not isinstance(data.get(field, ""), str):
            raise ValidationError(f"manifest field '{field}' must be a string")

    normalized = normalize_url(str(data.get("job_url", "")))
    expected_id = application_id(normalized)
    if data.get("application_id") != expected_id:
        raise ValidationError(f"application_id must be {expected_id} for the normalized job URL")
    data["job_url"] = normalized
    data["created_at"] = _parse_timestamp(data.get("created_at"), "created_at")
    data["updated_at"] = _parse_timestamp(data.get("updated_at"), "updated_at")

    compile_info = data.get("compile")
    if not isinstance(compile_info, dict):
        raise ValidationError("manifest must contain container compilation metadata")
    for field in ("runtime", "image", "image_digest"):
        if not isinstance(compile_info.get(field), str) or not compile_info[field].strip():
            raise ValidationError(f"manifest compile.{field} must be a non-empty string")
    if compile_info["runtime"] not in {"docker", "podman"}:
        raise ValidationError("manifest compile.runtime must be docker or podman")
    pages = compile_info.get("pages")
    page_limit = compile_info.get("page_limit")
    if not isinstance(pages, int) or isinstance(pages, bool) or pages < 1:
        raise ValidationError("manifest compile.pages must be a positive integer")
    if not isinstance(page_limit, int) or isinstance(page_limit, bool) or page_limit < 1:
        raise ValidationError("manifest compile.page_limit must be a positive integer")
    if pages > page_limit:
        raise ValidationError("manifest reports a PDF over the configured page limit")

    data["_manifest_path"] = manifest_path
    data["_job_path"] = _artifact_path(manifest_path, data.get("job_path"), "job_path")
    data["_tex_path"] = _artifact_path(manifest_path, data.get("tex_path"), "tex_path")
    data["_pdf_path"] = _artifact_path(manifest_path, data.get("pdf_path"), "pdf_path")

    snapshot_url, body = parse_job_envelope(data["_job_path"].read_text(encoding="utf-8"), require_body=True)
    if snapshot_url != normalized:
        raise ValidationError("job snapshot URL does not match manifest job_url")
    data["_job_body"] = body

    pdf_size = data["_pdf_path"].stat().st_size
    if pdf_size <= 0:
        raise ValidationError("resume PDF is empty")
    if pdf_size > MAX_ATTACHMENT_BYTES:
        raise ValidationError(
            f"resume PDF is {pdf_size} bytes; Airtable direct uploads are limited to {MAX_ATTACHMENT_BYTES} bytes"
        )
    if not data["_tex_path"].read_text(encoding="utf-8").strip():
        raise ValidationError("resume LaTeX source is empty")
    return data


def _field_specs() -> list[dict[str, Any]]:
    text = lambda name: {"name": name, "type": "singleLineText"}
    long_text = lambda name: {"name": name, "type": "multilineText"}
    return [
        text("Application ID"),
        text("Company"),
        text("Role"),
        text("Location"),
        text("Compensation"),
        {"name": "Job URL", "type": "url"},
        {
            "name": "Status",
            "type": "singleSelect",
            "options": {"choices": [{"name": choice} for choice in STATUS_CHOICES]},
        },
        {
            "name": "Created At",
            "type": "dateTime",
            "options": {
                "dateFormat": {"name": "iso"},
                "timeFormat": {"name": "24hour"},
                "timeZone": "utc",
            },
        },
        {
            "name": "Updated At",
            "type": "dateTime",
            "options": {
                "dateFormat": {"name": "iso"},
                "timeFormat": {"name": "24hour"},
                "timeZone": "utc",
            },
        },
        {"name": "Applied On", "type": "date", "options": {"dateFormat": {"name": "iso"}}},
        long_text("Job Description"),
        long_text("Resume LaTeX"),
        {"name": "Resume PDF", "type": "multipleAttachments"},
        text("TeX Path"),
        text("PDF Path"),
        long_text("Verification Notes"),
        long_text("Next Action"),
        long_text("Notes"),
    ]


class AirtableClient:
    def __init__(
        self,
        token: str,
        base_id: str,
        table_name: str = DEFAULT_TABLE,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise ConfigurationError("AIRTABLE_TOKEN is required")
        if not base_id:
            raise ConfigurationError("AIRTABLE_BASE_ID is required")
        self.token = token
        self.base_id = base_id
        self.table_name = table_name or DEFAULT_TABLE
        self.opener = opener
        self.sleeper = sleeper

    @classmethod
    def from_environment(cls) -> "AirtableClient":
        stored: dict[str, str] = {}
        if CREDENTIALS_FILE.exists():
            mode = CREDENTIALS_FILE.stat().st_mode & 0o777
            if mode & 0o077:
                raise ConfigurationError(
                    f"stored Airtable credentials must be owner-only (chmod 600 {CREDENTIALS_FILE})"
                )
            try:
                payload = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigurationError(f"cannot read stored Airtable credentials: {exc}") from exc
            if not isinstance(payload, dict):
                raise ConfigurationError("stored Airtable credentials must be a JSON object")
            stored = {key: value for key, value in payload.items() if isinstance(value, str)}

        return cls(
            os.environ.get("AIRTABLE_TOKEN", "").strip() or stored.get("token", "").strip(),
            os.environ.get("AIRTABLE_BASE_ID", "").strip() or stored.get("base_id", "").strip(),
            os.environ.get("AIRTABLE_TABLE_NAME", "").strip()
            or stored.get("table_name", "").strip()
            or DEFAULT_TABLE,
        )

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        *,
        attempts: int = 3,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(attempts):
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with self.opener(request, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw.decode("utf-8")) if raw else {}
            except urllib.error.HTTPError as exc:
                status = exc.code
                response_headers = exc.headers
                detail = exc.read().decode("utf-8", errors="replace")
                exc.close()
                retryable = status == 429 or 500 <= status < 600
                if retryable and attempt + 1 < attempts:
                    retry_after = response_headers.get("Retry-After") if response_headers else None
                    delay = min(float(retry_after), 10.0) if retry_after and retry_after.isdigit() else 2**attempt
                    self.sleeper(delay)
                    continue
                raise AirtableError(f"Airtable HTTP {status}: {detail}", status=status) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt + 1 < attempts:
                    self.sleeper(2**attempt)
                    continue
                raise AirtableError(f"cannot reach Airtable: {exc}") from exc
        raise AirtableError("Airtable request exhausted retries")

    @property
    def records_url(self) -> str:
        return f"{API_ROOT}/{urllib.parse.quote(self.base_id, safe='')}/{urllib.parse.quote(self.table_name, safe='')}"

    def list_tables(self) -> list[dict[str, Any]]:
        result = self._request("GET", f"{API_ROOT}/meta/bases/{urllib.parse.quote(self.base_id, safe='')}/tables")
        return list(result.get("tables", []))

    def setup(self) -> str:
        tables = self.list_tables()
        table = next((item for item in tables if item.get("name") == self.table_name), None)
        specs = _field_specs()
        if table is None:
            result = self._request(
                "POST",
                f"{API_ROOT}/meta/bases/{urllib.parse.quote(self.base_id, safe='')}/tables",
                {"name": self.table_name, "description": "Job applications generated by the resume agent", "fields": specs},
            )
            return str(result.get("id", ""))

        fields = {field.get("name"): field for field in table.get("fields", [])}
        primary_id = table.get("primaryFieldId")
        primary = next((field for field in table.get("fields", []) if field.get("id") == primary_id), None)
        if not primary or primary.get("name") != "Application ID" or primary.get("type") != "singleLineText":
            raise ValidationError("existing Airtable table must use 'Application ID' (single line text) as its primary field")

        table_id = str(table.get("id", ""))
        for spec in specs:
            existing = fields.get(spec["name"])
            if existing:
                if existing.get("type") != spec["type"]:
                    raise ValidationError(
                        f"Airtable field '{spec['name']}' has type {existing.get('type')!r}; expected {spec['type']!r}"
                    )
                if spec["name"] == "Status":
                    existing_choices = {choice.get("name") for choice in existing.get("options", {}).get("choices", [])}
                    missing = set(STATUS_CHOICES) - existing_choices
                    if missing:
                        raise ValidationError(f"Airtable Status field is missing choices: {', '.join(sorted(missing))}")
                continue
            self._request(
                "POST",
                f"{API_ROOT}/meta/bases/{urllib.parse.quote(self.base_id, safe='')}/tables/{urllib.parse.quote(table_id, safe='')}/fields",
                spec,
            )
        return table_id

    def find_record(self, app_id: str) -> dict[str, Any] | None:
        escaped = app_id.replace("\\", "\\\\").replace("'", "\\'")
        query = urllib.parse.urlencode({"maxRecords": 1, "filterByFormula": f"{{Application ID}}='{escaped}'"})
        result = self._request("GET", f"{self.records_url}?{query}")
        records = result.get("records", [])
        return records[0] if records else None

    def create_record(self, fields: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", self.records_url, {"records": [{"fields": fields}], "typecast": True})
        return result["records"][0]

    def update_record(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "PATCH",
            self.records_url,
            {"records": [{"id": record_id, "fields": fields}], "typecast": True},
        )
        return result["records"][0]

    def upload_pdf(self, record_id: str, pdf_path: Path, filename: str) -> dict[str, Any]:
        content = pdf_path.read_bytes()
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValidationError("PDF exceeds Airtable's 5 MB direct attachment upload limit")
        field = urllib.parse.quote("Resume PDF", safe="")
        url = (
            f"{CONTENT_ROOT}/{urllib.parse.quote(self.base_id, safe='')}/"
            f"{urllib.parse.quote(record_id, safe='')}/{field}/uploadAttachment"
        )
        return self._request(
            "POST",
            url,
            {"contentType": "application/pdf", "file": base64.b64encode(content).decode("ascii"), "filename": filename},
        )


def persist_credentials(client: AirtableClient) -> Path:
    """Atomically store validated credentials for unattended future runs."""
    directory = CREDENTIALS_FILE.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(directory, 0o700)
    temporary = directory / f".{CREDENTIALS_FILE.name}.tmp"
    payload = {
        "token": client.token,
        "base_id": client.base_id,
        "table_name": client.table_name,
    }
    try:
        temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        os.replace(temporary, CREDENTIALS_FILE)
        if os.name == "posix":
            os.chmod(CREDENTIALS_FILE, 0o600)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ConfigurationError(f"cannot store Airtable credentials securely: {exc}") from exc
    return CREDENTIALS_FILE


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path.resolve())


def _verification_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(f"- {item}" for item in value)
    raise ValidationError("verification_notes must be a string or list of strings")


def sync_application(client: AirtableClient, manifest_path: str | Path) -> tuple[str, bool]:
    data = load_manifest(manifest_path)
    tex = data["_tex_path"].read_text(encoding="utf-8")
    fields: dict[str, Any] = {
        "Application ID": data["application_id"],
        "Company": data["company"].strip(),
        "Role": data["role"].strip(),
        "Location": data.get("location", "").strip(),
        "Compensation": data.get("compensation", "").strip(),
        "Job URL": data["job_url"],
        "Updated At": data["updated_at"],
        "Job Description": data["_job_body"],
        "Resume LaTeX": tex,
        "TeX Path": _display_path(data["_tex_path"]),
        "PDF Path": _display_path(data["_pdf_path"]),
        "Verification Notes": _verification_text(data.get("verification_notes")),
    }

    existing = client.find_record(data["application_id"])
    if existing is None:
        created = True
        fields["Status"] = "Draft"
        fields["Created At"] = data["created_at"]
        record = client.create_record(fields)
        attachments: list[dict[str, Any]] = []
    else:
        created = False
        record = client.update_record(str(existing["id"]), fields)
        attachments = list(existing.get("fields", {}).get("Resume PDF", []))

    upload_name = f"resume-{data['run_id']}.pdf"
    if not any(item.get("filename") == upload_name for item in attachments):
        client.upload_pdf(str(record["id"]), data["_pdf_path"], upload_name)
    return str(record["id"]), created


def _queue_path(data: dict[str, Any]) -> Path:
    safe_run = re.sub(r"[^A-Za-z0-9_.-]+", "_", data["run_id"])
    return OUTBOX / f"{data['application_id']}-{safe_run}.json"


def queue_manifest(manifest_path: str | Path, error: Exception, *, previous_attempts: int = 0) -> Path:
    data = load_manifest(manifest_path)
    OUTBOX.mkdir(parents=True, exist_ok=True)
    path = _queue_path(data)
    try:
        relative_manifest = str(Path(manifest_path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        relative_manifest = str(Path(manifest_path).resolve())
    payload = {
        "manifest_path": relative_manifest,
        "attempts": previous_attempts + 1,
        "last_attempt_at": _utc_now(),
        "last_error": str(error),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _resolve_queued_manifest(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def retry_outbox(client: AirtableClient) -> tuple[int, int]:
    if not OUTBOX.exists():
        return 0, 0
    succeeded = 0
    failed = 0
    for queue_file in sorted(OUTBOX.glob("*.json")):
        queued: dict[str, Any] = {}
        manifest: Path | None = None
        try:
            queued = json.loads(queue_file.read_text(encoding="utf-8"))
            manifest = _resolve_queued_manifest(str(queued["manifest_path"]))
            sync_application(client, manifest)
        except (OSError, json.JSONDecodeError, KeyError, TrackerError) as exc:
            failed += 1
            attempts = int(queued.get("attempts", 0)) if isinstance(queued, dict) else 0
            if manifest is not None:
                try:
                    queue_manifest(manifest, exc, previous_attempts=attempts)
                except (TrackerError, OSError):
                    pass
            print(f"pending: {queue_file.name}: {exc}", file=sys.stderr)
        else:
            queue_file.unlink()
            succeeded += 1
            print(f"synced: {queue_file.name}")
    return succeeded, failed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="create or validate the Airtable Applications table")
    sync = commands.add_parser("sync", help="upsert one generated application")
    sync.add_argument("manifest", help="path to application.json")
    commands.add_parser("retry", help="retry all queued Airtable writes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "setup":
            client = AirtableClient.from_environment()
            table_id = client.setup()
            credentials_path = persist_credentials(client)
            succeeded, failed = retry_outbox(client)
            print(f"Airtable table ready: {client.table_name} ({table_id})")
            print(f"credentials stored securely: {credentials_path}")
            print(f"outbox drain: {succeeded} synced, {failed} pending")
            return 2 if failed else 0
        if args.command == "retry":
            client = AirtableClient.from_environment()
            succeeded, failed = retry_outbox(client)
            print(f"retry complete: {succeeded} synced, {failed} pending")
            return 2 if failed else 0

        try:
            client = AirtableClient.from_environment()
            record_id, created = sync_application(client, args.manifest)
        except (ConfigurationError, AirtableError) as exc:
            queued = queue_manifest(args.manifest, exc)
            print(f"resume artifacts are valid; Airtable sync is pending in {queued}: {exc}", file=sys.stderr)
            return 2
        print(f"Airtable record {'created' if created else 'updated'}: {record_id}")
        return 0
    except (ConfigurationError, ValidationError, AirtableError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
