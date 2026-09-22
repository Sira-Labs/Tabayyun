# Developer entry points. Each directory also works on its own (cargo / uv / pnpm).
.PHONY: all core api web test lint fmt synth demo dev-infra api-dev web-dev db-upgrade db-revision

all: lint test

core:
	cd core && cargo build --release

test:
	cd core && cargo test
	cd api && uv run pytest -q
	cd web && pnpm build

lint:
	cd core && cargo fmt --check && cargo clippy --all-targets -- -D warnings
	cd api && uv run ruff check . && uv run ruff format --check . && uv run mypy
	cd web && pnpm lint

fmt:
	cd core && cargo fmt
	cd api && uv run ruff format . && uv run ruff check --fix .

# Generate a faulty synthetic series and run all checks on it.
demo:
	cd core && cargo run -q --bin tabayyun -- synth --out /tmp/tabayyun-demo.csv --n 2880 \
	  --faults gap,flatline,nans,negative,range,duplicates,out_of_order
	cd core && cargo run -q --bin tabayyun -- run --input /tmp/tabayyun-demo.csv \
	  --quality-col quality --unit "m3/h" --physical-max 200 --pretty

dev-infra:
	docker compose -f deploy/compose.dev.yaml up -d

api-dev:
	cd api && uv run uvicorn tabayyun.main:app --reload --port 8000

# Database schema: apply migrations / autogenerate a new one from the models (m="message").
db-upgrade:
	cd api && uv run alembic upgrade head

db-revision:
	cd api && uv run alembic revision --autogenerate -m "$(m)"

web-dev:
	cd web && pnpm dev
