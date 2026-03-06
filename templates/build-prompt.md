# Build Prompt

You are an AI coding agent implementing a work order.

## Target Module
{{ module }}

## Known Files
{{ known_files }}

## Acceptance Criteria
{{ acceptance_criteria }}

## Constraints
{{ constraints }}

## Instructions
Implement the changes required to satisfy all acceptance criteria while
respecting all constraints. Follow these guidelines:

1. Read all known files before making changes.
2. Make minimal, focused changes — do not refactor unrelated code.
3. Ensure all acceptance criteria are verifiable after your changes.
4. Run build and test commands to confirm nothing is broken.
5. Do not introduce new dependencies unless explicitly allowed.
