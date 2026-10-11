# Frontend/API contract

Base path /api. Errors return {"detail":"Human-readable message"}; validation errors may contain FastAPI's detail array.

Model:
{"id":"iris-classifier","name":"Iris classifier","version":"1.0.0","framework":"scikit-learn","description":"...","image":null,"kind":"local","created_at":"ISO timestamp","metrics":{"accuracy":0.96,"f1_macro":0.96,"train_accuracy":0.98,"train_samples":120,"test_samples":30},"feature_names":["sepal length (cm)","sepal width (cm)","petal length (cm)","petal width (cm)"],"labels":["setosa","versicolor","virginica"]}
The metric values above describe the shape, not fixed results. Backend returns measured values.
- GET /models -> Model[]
- POST /models/example -> Model (201), duplicate -> 409
- POST /models -> Model (201); body {"name":"...","version":"1.0.0","framework":"PyTorch","description":"...","image":"ghcr.io/owner/model:v1"}. External models have kind="container", metrics={}, feature_names=[], labels=[]. Registration text is trimmed before the existing length checks; required name/version/framework values cannot become blank after trimming, and internal formatting is preserved while description whitespace-only values become empty strings.
- POST /models/{id}/predict; body {"features":[5.1,3.5,1.4,0.2]} -> {"label":"setosa","confidence":0.99,"probabilities":{"setosa":0.99,"versicolor":0.01,"virginica":0.0},"latency_ms":1.23}

DeploymentPlan:
{"id":"plan-...","model_id":"iris-classifier","name":"iris-service","namespace":"strawhats","image":"ghcr.io/owner/model:v1","replicas":2,"cpu_millicores":250,"memory_mebibytes":256,"created_at":"ISO timestamp","status":"preview","manifest":"YAML string"}
- POST /deployment-plans; body {"model_id":"...","name":"iris-service","namespace":"strawhats","image":"...","replicas":2,"cpu_millicores":250,"memory_mebibytes":256} -> DeploymentPlan (201)
- GET /deployment-plans -> DeploymentPlan[]
- GET /cluster -> {"connected":false,"context":"docker-desktop","message":"No cluster is reachable. Deployment previews are available."}

Local Iris (separate from manifest previews):
- GET /local-iris -> {"enabled":true,"connected":true,"operator_ready":true,"context":"docker-desktop","namespace":"strawhats-operator-local","name":"iris","phase":"Ready","mode":"Deploy","image":"strawhats-iris:milestone-one","reported_image":"strawhats-iris:milestone-one","desired_replicas":1,"ready_replicas":1,"generation":5,"observed_generation":5,"rollback_available":true,"message":"Requested revision and endpoints are ready"}. Example values describe shape, not fixed observations.
- POST /local-iris/deploy; body {"replicas":1} -> current Local Iris status (202); creates/updates the fixed CR with the verified image and default CPU/memory.
- POST /local-iris/scale; body {"replicas":2} -> current Local Iris status (202); requires a verified Ready/RolledBack generation and preserves its effective image/resources.
- POST /local-iris/rollback; body {} -> current Local Iris status (202); requests the most recent verified snapshot, leaving the operator's status intact.
- POST /local-iris/predict; body {"features":[5.1,3.5,1.4,0.2]} -> {"label":"setosa","confidence":0.99,"probabilities":{"setosa":0.99,"versicolor":0.01,"virginica":0.0},"latency_ms":1.23,"source":"kubernetes","context":"docker-desktop","namespace":"strawhats-operator-local","service":"iris","image":"strawhats-iris:milestone-one","generation":5} (200). Values illustrate shape. Exactly four finite numeric measurements greater than 0 and at most 30 cm are required; strings, booleans and extra fields return 422 before credentials load.
- Prediction requires a current Ready/RolledBack generation, ready operator and at least one ready replica. Missing/pending/unready deployment or an unrelated Service returns 409. The fixed owned ClusterIP Service selector and port are checked before forwarding JSON to iris:8000/predict via the Kubernetes API proxy. There is no selectable URL, port-forward requirement or workspace fallback. Invalid upstream prediction data returns sanitized 502; transport/access failures return sanitized 503/403.
- Successful deployed inference records an Activity event and increments the serving model's metrics. It does not register/train a workspace model or change Overview's workspace prediction count. Returned generation/image describe the operator revision observed at request start; latency_ms is model processing time, excluding proxy/network overhead.
- Replica inputs are strict integers 1–2. Extra fields (including namespace/name/image) are rejected with 422. All actions require application/json.
- Controls default to disabled unless LOCAL_OPERATOR_ENABLED=1; test-mode app instances always disable them. Disabled GET reports phase Disabled without loading cluster credentials; actions return 503. When enabled, API access requires a loopback caller/host and a permitted local browser origin.
- The transport explicitly selects docker-desktop, disables inherited HTTP proxy routing, and refuses remote/insecure API endpoints or an unexpected proxy. It writes only iris in strawhats-operator-local; no workload writes, namespace creation, CRD installation, or AWS calls occur here.
- Ready/RolledBack reflects controller-observed generation; newer desired generations report Pending. Reported replica/image values are the last controller observations. An offline controller reports Unavailable. A missing Iris resource reports NotDeployed when the controller is ready.
- Version conflicts/missing rollback snapshot/non-ready scaling return 409; inaccessible local Kubernetes returns sanitized 503 or access-denied 403. Failed requests do not create Activity events. Accepted requests log a request event, not claimed completion.
- The Models prediction route remains the workspace predictor; deployed inference uses only /local-iris/predict.

Overview:
{"models":1,"deployment_plans":0,"predictions":3,"median_latency_ms":1.23,"recent_inferences":[{"created_at":"ISO timestamp","latency_ms":1.23,"label":"setosa","model_id":"iris-classifier"}]}
- GET /overview -> Overview
- GET /events -> [{"id":1,"kind":"model","title":"Model registered","description":"Iris classifier 1.0.0","created_at":"ISO timestamp"}]
- GET /health -> {"status":"ok","ai_configured":true,"ai_model":"gpt-4.1-mini","version":"0.1.0"}
- POST /assistant; body {"messages":[{"role":"user","content":"How should I deploy this model?"}]} -> {"message":"AI answer","model":"gpt-4.1-mini"}; provider unavailability -> sanitized 503.
- GET /metrics (outside /api) -> Prometheus exposition.

UI: real empty states; explicit preview labels; no pretend cluster operations. Use /api proxy locally. Optional VITE_API_BASE_URL is the backend origin on Vercel (no trailing /api).
