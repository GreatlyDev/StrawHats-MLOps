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

Overview:
{"models":1,"deployment_plans":0,"predictions":3,"median_latency_ms":1.23,"recent_inferences":[{"created_at":"ISO timestamp","latency_ms":1.23,"label":"setosa","model_id":"iris-classifier"}]}
- GET /overview -> Overview
- GET /events -> [{"id":1,"kind":"model","title":"Model registered","description":"Iris classifier 1.0.0","created_at":"ISO timestamp"}]
- GET /health -> {"status":"ok","ai_configured":true,"ai_model":"gpt-4.1-mini","version":"0.1.0"}
- POST /assistant; body {"messages":[{"role":"user","content":"How should I deploy this model?"}]} -> {"message":"AI answer","model":"gpt-4.1-mini"}; provider unavailability -> sanitized 503.
- GET /metrics (outside /api) -> Prometheus exposition.

UI: real empty states; explicit preview labels; no pretend cluster operations. Use /api proxy locally. Optional VITE_API_BASE_URL is the backend origin on Vercel (no trailing /api).
