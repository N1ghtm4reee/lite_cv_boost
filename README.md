# Job-Targeted Resume Agent with Airtable Tracking

This small repository turns a base LaTeX resume and a job posting into an
ATS-oriented, job-specific resume. A coding agent performs the content analysis
and tailoring, while deterministic Python helpers compile the PDF, validate the
page limit, and log the application in Airtable.

The workflow is designed for repeated use: every successful run gets an
immutable local snapshot, and one Airtable record tracks the application across
future resume revisions.

## What it does

- Accepts an exact job-posting URL and optional pasted job description.
- Tailors an existing LaTeX resume without changing protected personal,
  employment, education, project, or certification facts.
- Identifies unsupported technology additions for candidate verification.
- Compiles the resume in Docker or Podman and enforces the configured page
  limit.
- Saves the job snapshot, LaTeX source, PDF, and machine-readable manifest in a
  versioned application directory.
- Creates or updates an Airtable application record and uploads the PDF.
- Preserves manually managed Airtable fields and queues temporary sync
  failures for automatic retry.

## How the project is split

| Component | Purpose |
|---|---|
| `AGENTS.md` | Complete agent policy: inputs, tailoring rules, factual constraints, validation, output, and tracking workflow. |
| `resume.example.tex` | Synthetic base-resume template safe to publish. Copy it to the ignored `resume.tex`. |
| `job_description.example.md` | Synthetic job-input template. Copy it to the ignored `job_description.md`. |
| `optionals.md` | Optional title, known skills, augmentation restrictions, output filename, and page limit. |
| `scripts/compile_resume.py` | Mandatory container compilation and PDF page-count validation. |
| `scripts/application_tracker.py` | Airtable schema setup, idempotent sync, PDF upload, credential loading, and retry queue. |
| `TRACKING.md` | Detailed Airtable and synchronization reference. |
| `applications/` | Generated immutable application bundles; intentionally gitignored. |
| `.tracking/` | Owner-only Airtable credentials and retry state; intentionally gitignored. |

There is intentionally no ordinary `generate` CLI. Resume tailoring requires
job interpretation and is performed by an LLM coding agent following
`AGENTS.md`. The Python scripts own only the reproducible and security-sensitive
parts of the workflow.

## Requirements

- Python 3.10 or newer. Runtime code uses only the standard library.
- A coding agent with repository read/write access and the ability to follow
  `AGENTS.md`.
- Docker or Podman.
- `pdfinfo`, normally provided by `poppler-utils`.
- Internet access when the job description must be retrieved and for Airtable
  synchronization.
- An Airtable account and an existing base for automatic tracking.

Linux, macOS, and WSL are the intended environments because the workflow uses
POSIX file permissions and container bind mounts. On native Windows, WSL is the
recommended compatibility layer. `mypy` is optional and needed only for the
development type-check command shown below.

The first compilation downloads
`docker.io/texlive/texlive:latest`, which is a large image. Later runs reuse the
local image cache.

## Agent compatibility

The repository works directly with agents that discover and honor
`AGENTS.md`, including OpenAI Codex-style coding agents. The agent must be able
to:

1. Read the complete `AGENTS.md`, `resume.tex`, `job_description.md`, and
   `optionals.md` files.
2. Write generated artifacts under `applications/`.
3. Run Python, Docker or Podman, and `pdfinfo`.
4. Access the posting URL when no description body is supplied.

For an agent that does not automatically discover `AGENTS.md`, explicitly tell
it to read that file before working. Do not copy only part of the instructions;
the factual-integrity and candidate-verification sections are required.

Recommended prompt:

```text
Read and follow AGENTS.md. Tailor resume.tex for job_description.md, compile and
validate the PDF, create the application bundle, and synchronize Airtable.
```

You can also give the agent a posting URL and pasted description directly and
ask it to populate `job_description.md` before running the same workflow.

## Quick start

### 1. Prepare the repository

Create private working inputs from the public examples, then replace the sample
resume content with your own:

```bash
cp resume.example.tex resume.tex
cp job_description.example.md job_description.md
```

Both destination files are gitignored because they may contain personal data.
Preserve the LaTeX template in `resume.tex`; the agent is instructed to change
only approved resume content.

Optionally edit the YAML configuration block in `optionals.md`:

| Setting | Default | Meaning |
|---|---|---|
| `target_job_title` | inferred | Desired positioning; never changes historical role titles. |
| `known_additional_skills` | `[]` | Skills asserted by the candidate but absent from the source resume. |
| `augmentation_eligible_projects` | `[]` | Existing projects allowed to receive proposed technology adaptations; empty means all existing projects. |
| `output_filename` | `resume_tailored.tex` | Filename used inside each generated run directory. |
| `page_limit` | base page count | Maximum validated PDF pages; this repository currently configures `1`. |

### 2. Create the Airtable base and token

Create an empty Airtable base. Copy its **Base ID**, which begins with `app`,
from the base URL:

```text
https://airtable.com/appXXXXXXXXXXXXXX/tblXXXXXXXXXXXXXX/...
                     ^ Base ID
```

Do not use the table ID beginning with `tbl` as `AIRTABLE_BASE_ID`.

Create an Airtable personal access token with access to that base and these
scopes:

- `data.records:read`
- `data.records:write`
- `schema.bases:read`
- `schema.bases:write`

Export the credentials once in a trusted terminal:

```bash
export AIRTABLE_TOKEN='your-personal-access-token'
export AIRTABLE_BASE_ID='appXXXXXXXXXXXXXX'
export AIRTABLE_TABLE_NAME='Applications'

python3 scripts/application_tracker.py setup
```

`.env.example` is a reference for variable names; the tracker deliberately does
not auto-load a plaintext `.env` file.

`setup` creates or validates the Airtable table, stores the validated
credentials in `.tracking/airtable_credentials.json` with owner-only `0600`
permissions, and drains queued sync operations. Future agent runs load this
local credential store automatically, so they do not require exported variables
or manual synchronization.

Never put real credentials in `README.md`, `TRACKING.md`, `.env.example`, a
prompt, or a committed file.

### 3. Add a job

Write the exact posting URL in `job_description.md`. The body is optional:

```markdown
---
url: https://company.example/jobs/123
---

Optional full job description pasted here.
```

- When the body is present, it is authoritative.
- When the body is empty, the agent retrieves the URL and snapshots the
  posting.
- If the URL cannot be read and no body is supplied, the workflow stops instead
  of guessing.
- Prefer the direct posting URL over a search-results URL.

### 4. Run the agent

Ask your compatible coding agent to execute the recommended prompt above. A
successful run will:

1. Analyze the posting and tailor the resume.
2. Create a unique application/run directory.
3. Compile and validate the PDF.
4. Write `application.json` only after compilation succeeds.
5. Upsert the Airtable record and upload the run-specific PDF.

No manual Airtable sync should be needed after the one-time setup.

## Generated output

Each successful run is stored under:

```text
applications/
└── YYYY-MM-DD-company-role-urlhash/
    └── runs/
        └── YYYYMMDDTHHMMSSZ/
            ├── job.md
            ├── resume_tailored.tex
            ├── resume_tailored.pdf
            └── application.json
```

The manifest records the normalized job URL, company, role, location,
compensation, compilation image digest, page count, artifact paths, and
candidate-verification notes.

The normalized URL determines the Airtable `Application ID`. Re-running the
same posting updates the same Airtable record, preserves an advanced status,
and appends only a new run-specific PDF.

## Airtable behavior

The setup command provisions these fields automatically:

- Application ID, Company, Role, Location, Compensation, and Job URL
- Status: Draft, Applied, Interview, Offer, Rejected, or Withdrawn
- Created At, Updated At, and Applied On
- Job Description and Resume LaTeX
- Resume PDF attachment
- Local TeX and PDF paths
- Verification Notes, Next Action, and Notes

New records start as `Draft`. Automatic sync never overwrites `Applied On`,
`Next Action`, `Notes`, or an existing application status. PDF uploads use
Airtable's direct attachment API and must be 5 MB or smaller.

## Failure and retry behavior

Resume generation and Airtable availability are deliberately separated:

- Compilation or page-limit failure stops the run before a manifest or Airtable
  Draft is created.
- Airtable/network failure preserves the valid bundle and writes an idempotent
  entry under `.tracking/outbox/`.
- Missing credentials also queue a valid manifest.
- The next successful `setup` drains the queue automatically.
- A manual retry is available when desired:

  ```bash
  python3 scripts/application_tracker.py retry
  ```

Tracker exit codes:

| Code | Meaning |
|---|---|
| `0` | Synchronization or setup completed successfully. |
| `1` | Invalid local configuration, schema, manifest, or artifacts. |
| `2` | Valid work is retained but one or more Airtable operations remain queued. |

## Credential rotation

If the Airtable token changes, export only the replacement and run setup again;
the stored base and table configuration are reused:

```bash
export AIRTABLE_TOKEN='your-new-token'
python3 scripts/application_tracker.py setup
unset AIRTABLE_TOKEN
```

If a token ever appears in Git, documentation, terminal output shared with
others, or a chat message, revoke it in Airtable and issue a new one.

## Testing

Run the complete local test and type-check suite with:

```bash
python3 -m unittest discover -s tests -v
mypy scripts/application_tracker.py scripts/compile_resume.py
git diff --check
```

The unit tests mock Airtable. A live Airtable call occurs only when running
`setup`, `sync`, or `retry` with configured credentials.

## Troubleshooting

### `AIRTABLE_TOKEN is required`

The local credential store does not exist or is unreadable. Export the token and
base ID, then run `python3 scripts/application_tracker.py setup` once.

### Credential file must be owner-only

Restore secure permissions:

```bash
chmod 700 .tracking
chmod 600 .tracking/airtable_credentials.json
```

### Airtable base or table errors

Confirm that `AIRTABLE_BASE_ID` begins with `app`, that the token can access the
base, and that all four required scopes were granted. Re-run `setup`; it is
idempotent and reports incompatible existing fields instead of silently
changing them.

### Job page cannot be retrieved

Paste the full posting below the front matter in `job_description.md`. The
agent will use that snapshot without depending on the website.

### Container compilation fails

Confirm Docker or Podman is running and that the TeX image can be pulled:

```bash
docker pull docker.io/texlive/texlive:latest
# or
podman pull docker.io/texlive/texlive:latest
```

The compilation helper detects a Podman-backed Docker compatibility wrapper and
uses native Podman behavior when needed.

### Resume exceeds the page limit

The agent must shorten editable bullets or lower-priority skills. It must not
change protected template settings, margins, font size, identities, dates, or
credentials merely to force the page count.

## Factual integrity

Technology additions absent from the source resume are proposals, not verified
claims. The agent records each such addition in the manifest and its completion
message. Review and confirm every proposed claim before submitting the resume.

This workflow improves job relevance and ATS readability, but it does not
guarantee an ATS score, interview, or hiring outcome.
