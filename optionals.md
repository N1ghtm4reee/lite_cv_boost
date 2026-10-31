# Resume Tailoring — Optional Configuration

Edit the values in the YAML block below before running Codex. This file is optional: if it is absent, `AGENTS.md` defaults apply. Leave a value as `null` or a list as `[]` to use its documented default.

## Configuration

```yaml
# String or null. null = infer the target role from job_description.md.
target_job_title: null
# Example: target_job_title: "Platform Engineer Intern"

# List of skills you know you have but that may be missing from resume.tex.
# [] = no additional known skills. These are candidate-asserted, not proof of project use.
known_additional_skills: []
# Example:
# known_additional_skills:
#   - AWS
#   - Python
#   - Terraform

# Exact names of EXISTING projects in resume.tex that Codex may propose
# augmenting with JD technologies. [] = any existing project is eligible.
# A non-empty list blocks technology augmentation in all other projects.
augmentation_eligible_projects: []
# Example:
# augmentation_eligible_projects:
#   - ft_transcendence
#   - cloud-1

# Output filename ending in .tex, or null for resume_tailored.tex.
# Only the basename is used; the agent writes it inside the immutable run folder.
# Must not be resume.tex.
output_filename: null
# Example: output_filename: "platform_engineer_resume.tex"

# Positive integer, or null to preserve the original page count if possible.
# Codex should compile and count pages when tooling is available.
page_limit: 1
# Example: page_limit: 1
```

## How to use

Copy `resume.example.tex` to the gitignored `resume.tex` and `job_description.example.md` to the gitignored `job_description.md`. Customize only the YAML values above; remove or leave the example comments as you prefer. Any technology newly inserted into a project or experience remains an **unverified draft claim** until you confirm you actually performed that work. The agent must list those additions separately for review, never annotate the LaTeX resume.

**Defaults:** title inferred from JD; no extra known skills; all existing projects eligible for coherent technology proposals; output filename `resume_tailored.tex` inside the application run directory; target the base resume's page count.
