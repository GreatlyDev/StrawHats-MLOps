# StrawHats MLOps

**COSC 472 · Group 12 · Straw Hats**

A model operations dashboard for registering ML models, testing inference, preparing Kubernetes deployment configuration, and asking for deployment guidance through LangChain. This repository is the first local foundation of the group's ML Deployment Orchestrator.

**Status:** the local dashboard and API work. Cloud Kubernetes orchestration, production deployment, the public demo, and demo GIF are not implemented yet.

## Team

| Member | Responsibility |
| --- | --- |
| GreatAnthony Umukoro ([GreatlyDev](https://github.com/GreatlyDev)) | UI, integration, deployment and release decisions |
| Mikayla Brown | Backend development |
| Mahki Titus | Backend development |

Each teammate reviews, tests, and commits their contributions through feature branches and pull requests.

## Features in this release

- Light responsive React dashboard with Overview, Models, Deployment previews, Assistant, and Activity.
- Persistent SQLite model/container registry and timeline.
- Reproducible Iris classifier with held-out evaluation and real local predictions.
- Kubernetes Namespace, Deployment, and Service YAML previews with selected resources, replicas, probes, and nonroot settings; copy/download support.
- Read-only cluster reachability check that reports the actual local context.
- Observed prediction counts/latency and Prometheus endpoints.
- LangChain/OpenAI guidance with server-only credentials and actionable provider errors.
- Standalone Iris model service ready for container image validation.

Container registrations store metadata. The name/version Iris classifier v1.0.0 is reserved for the built-in example. Local inference supports the bundled Iris example; external images require a separately deployed endpoint. Previews do not apply workloads to a cluster.

## Architecture

```text
React / TypeScript / Vite dashboard
             │ /api (local Vite proxy)
             ▼
FastAPI ── SQLite + local trained artifact
  ├── model registry, inference, measured metrics
  ├── Kubernetes YAML preview + read-only kubectl health
  └── LangChain ── OpenAI (only on assistant submission)

Standalone model service: /health, /predict, /metrics
Future: Kubernetes CRD/operator + Prometheus/Grafana + cloud release
```

The dashboard does not receive provider keys. Local records and model artifacts live in `.local/`, which is excluded from Git. Prometheus counters reset with the process; dashboard totals persist in SQLite. Median latency covers the last 30 recorded predictions.

## Requirements and setup

Use Python 3.12 and Node 24 (or Node 22.12+). Docker and kubectl are optional for this local release and required for later container/cluster verification.

From the repository root on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
if (-not (Test-Path .env.local)) { Copy-Item .env.example .env.local }
cd frontend
npm.cmd ci
cd ..
```

If `.env.local` already exists, preserve it. Add your own `OPENAI_API_KEY` there only if you need the assistant. `OPENAI_MODEL` defaults to `gpt-4.1-mini`. The registry, inference, metrics, and previews work without an API key. API billing/credits are required for a successful provider request.

The checked-in `backend/requirements-lock.txt` records the verified local environment, including dev tools. `scikit-learn==1.7.2` and `uuid-utils==0.12.0` avoid native library incompatibilities encountered on the development Windows machine.

In two PowerShell terminals from the repository root:

```powershell
# Terminal 1
.\scripts\start-backend.ps1
```

```powershell
# Terminal 2
.\scripts\start-frontend.ps1
```

Open [the dashboard](http://127.0.0.1:5173) and [API documentation](http://127.0.0.1:8000/docs). Both development services bind to loopback. On macOS/Linux use `.venv/bin/python` and run the equivalent uvicorn/npm commands directly.

## Local walkthrough

1. Open Models and select **Train Iris example**.
2. Inspect the measured held-out accuracy and macro F1.
3. Run a prediction with measurements `[5.1, 3.5, 1.4, 0.2]`; inspect the label, probabilities, and measured latency.
4. Open Deployment previews, select a registered model, and enter an image you control. Generate and inspect the YAML. This saves a preview only.
5. Open Overview/Activity to see real counts and recorded events.
6. Submit a deployment question in Assistant when API credits are available.

For Iris, features are sepal length, sepal width, petal length, and petal width in centimeters. The bundled [scikit-learn Iris dataset](https://scikit-learn.org/1.7/datasets/toy_dataset.html#iris-plants-dataset) has 150 observations and 3 classes. A stratified split uses 120 training and 30 held-out test samples with seed 42. StandardScaler is fitted inside the training pipeline; logistic regression is evaluated on the held-out samples. These scores describe this educational dataset, not production model quality.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q --disable-plugin-autoload
.\.venv\Scripts\python.exe -m ruff check backend
.\.venv\Scripts\python.exe -m ruff format --check backend
.\.venv\Scripts\python.exe -m pip check
cd frontend
npm.cmd test
npm.cmd run format:check
npm.cmd run build
```

Tests exercise persistence, input rejection, real predictions, manifest contents, measured metrics, provider error handling, model-service health/inference, and browser API transport. No provider calls or cluster writes occur during tests. Browser verification covers local training, inference, YAML export, navigation, and responsive layouts.

## Containers and deployment

Build from the repository root after starting Docker:

```powershell
docker build -f backend/Dockerfile.model -t strawhats-iris:0.1.0 .
docker run --rm -p 127.0.0.1:8001:8000 strawhats-iris:0.1.0
```

The model image runs as UID 10001 and exposes `/health`, `/predict`, and `/metrics` on port 8000, matching the generated manifests. Push an image to a registry accessible to the cluster before using it in a real workload. Image build/run remains unverified on the initial development machine because its Docker engine was stopped.

`backend/Dockerfile` is a separate orchestrator API image. It needs persistent storage for `/app/.local` and runtime environment secrets. It does not include kubectl. Do not deploy the current management API publicly until authentication/authorization, request budgets, shared persistence, and production CORS are implemented.

For a future Vercel frontend project, select `frontend` as the root directory, Vite as the framework, `npm run build`, and output directory `dist`. Set `VITE_API_BASE_URL` to the verified HTTPS backend origin before building. Only public configuration belongs in Vite variables. The Kubernetes operator and model workloads run in Kubernetes.

## Contributing

Create feature branches from `main` and propose changes through pull requests. **Keep feature branches after merging.** GreatAnthony coordinates frontend changes, integration, and releases.

Read the [API contract](docs/api-contract.md) before changing backend interfaces. GitHub Actions runs backend tests/lint and frontend tests/build on pushes and pull requests. Use the verification commands above before opening a PR.

Format frontend changes with `npm run format` in `frontend`, and backend changes with `python -m ruff format backend` from the repository root.

Commit source, tests, maintained documentation, dependency lock files, and build configuration. Keep credentials, local data, generated artifacts, planning notes, and teammate handoff packages out of Git. `.env.example` contains placeholders only; real environment files stay local.

## Live demo and GIF

The current verified demo runs locally at `http://127.0.0.1:5173`. A public live URL and embedded GIF will be added after cloud deployment and the complete demonstration flow are verified.

## License

No license has been selected yet. Third-party packages and dataset terms remain governed by their respective licenses.
