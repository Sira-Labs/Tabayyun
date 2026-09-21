# Spec NNN — Title

Sprint N, story SN-M. Depends on: specs it needs. Packages: the directories it touches.

## Goal

One paragraph: what exists when the spec is done, stated as observable behaviour.

## User story

As a <persona from docs/architecture/01-product-vision.md>, I <do something> so that <outcome>.

## Interface

The exact surface: API routes with request and response shapes, CLI flags, config keys with
defaults, database tables with columns, UI routes. Anything a test can assert against.

## Behaviour

1. Numbered, testable steps in the order they happen, including every error path and its
   status code or exit code.

## Acceptance criteria

- [ ] Checkbox per criterion; each must be verifiable by a test or a command.

## Test cases

Unit (`package`): named tests and what each asserts.
Integration (`api/tests` or `tests/`): named tests, fixtures they need.

## Out of scope

Bullet list naming the spec or sprint where each deferred item lives.
