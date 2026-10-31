# Airtable application tracking

The resume agent creates an immutable local bundle for each successful run and
then upserts one Airtable record per normalized job-posting URL. Local bundles,
credentials, and retry state are gitignored because they contain sensitive data.

## One-time Airtable setup

1. Create an Airtable personal access token with access to the target base and
   these scopes: `data.records:read`, `data.records:write`,
   `schema.bases:read`, and `schema.bases:write`.
2. Export the credentials in a trusted terminal; never paste real values into
   this document or `.env.example`:

   ```bash
   export AIRTABLE_TOKEN='your-personal-access-token'
   export AIRTABLE_BASE_ID='appXXXXXXXXXXXXXX'
   export AIRTABLE_TABLE_NAME='Applications'
   ```

3. Provision or validate the table:

   ```bash
   python3 scripts/application_tracker.py setup
   ```

After Airtable validates the credentials, `setup` saves them under
`.tracking/airtable_credentials.json` with owner-only permissions. Future agent
runs load that local store automatically, so they do not depend on the original
terminal environment. Setup also drains any pending outbox entries.

The setup command is idempotent. It creates missing fields, but refuses to
silently change conflicting field types or an incompatible primary field.

If a token was ever placed in a tracked or shared file, remove it and rotate it
in Airtable even after sanitizing the repository.

## Job input

`job_description.md` always contains the exact posting URL. Its body is
optional:

```markdown
---
url: https://company.example/jobs/123
---

Optional pasted job description
```

When the body is blank, the agent retrieves the posting. When the body is
present, it is authoritative. An inaccessible URL with no pasted description
stops before resume generation.

## Compilation and synchronization

PDF compilation is container-only and mandatory. Docker is preferred when
both Docker and Podman are installed. Override the default image if needed:

```bash
export LATEX_CONTAINER_IMAGE='docker.io/texlive/texlive:latest'
python3 scripts/compile_resume.py applications/.../resume_tailored.tex \
  applications/.../resume_tailored.pdf --page-limit 1
```

The command prints JSON compilation metadata for `application.json`. After the
agent writes that manifest, synchronization is automatic. The underlying
command is:

```bash
python3 scripts/application_tracker.py sync applications/.../application.json
```

Exit status `0` means synchronized. Airtable/network failures and missing
credentials queue the valid manifest and return `2`; the next successful setup
or explicit `retry` drains the queue. Exit `1` is reserved for invalid local
configuration, schema, or artifacts.

The tracker never resets an existing application status and never writes
`Applied On`, `Next Action`, or `Notes`. Direct PDF uploads are limited to 5 MB.
