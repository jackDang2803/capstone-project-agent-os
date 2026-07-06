# Makefile for Second Brain Agent

.PHONY: install run playground clean test

install:
	uv sync

run:
	uv run python -m app.fast_api_app

playground:
	agents-cli playground

test:
	uv run pytest tests/

clean:
	rm -rf .venv
	rm -f test.db second_brain.db
