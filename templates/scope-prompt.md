# Scope Prompt

You are scoping a work order for an AI coding agent.

## Target Module
{{ module }}

## Known Files
{{ known_files }}

## Acceptance Criteria
{{ acceptance_criteria }}

## Instructions
Identify the minimal set of files that need to be read, modified, or created
to satisfy the acceptance criteria. For each file, note whether it is:
- **read**: needs to be understood for context
- **modify**: needs changes to existing code
- **create**: needs to be created from scratch

Output a structured scope analysis with file paths and estimated complexity.
