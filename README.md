# GraphQL Desk

GraphQL Desk is a planned general-purpose GraphQL developer tool for Python, with editor integration as its central goal. It aims to provide a Python-appropriate counterpart to the developer experience and feature set of [gql.tada](https://gql-tada.0no.co/).

## Project status

This repository is in planning and feasibility work. It currently contains no implementation. The first proof of concept will test whether VS Code editing can provide reliable GraphQL feedback and live Python operation types through Pyright, with generated Python types as the baseline. Live query-to-Python typing is an open feasibility question; Pyright's Type Server Protocol is promising but has not been validated here.

## Intended scope

The feature checklist will cover schema-aware diagnostics, completion, hover and navigation; typed operation results and variables; scalar mappings; schema handling and CLI checks; persisted documents; and fragment composition and masking. Fragment masking needs a Python-specific design. “Parity” means a useful equivalent developer experience where Python's type system permits it, not identical TypeScript behavior.

Ariadne Codegen and Turms are both under consideration as building blocks. They overlap as generators, so the project will evaluate them as separate backends before deciding whether and how to support them. They should not both generate the same output.

## Planning and contributions

Planning issues use consistent sections:
- **User story / goal**
- **Context**
- **Acceptance criteria**
- **Out of scope**
- **Dependencies**

Spikes state the question, method, deliverables, and decision criteria. Epics describe an outcome and link their child issues. Implementation has not started; when it does, work should be split into small, reviewable pull requests with the relevant issue linked.
