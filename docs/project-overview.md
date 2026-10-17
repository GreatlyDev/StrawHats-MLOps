# What StrawHats MLOps does

The October 17, 2026 demonstration verified AWS deployment, inference, scaling,
snapshot rollback, monitoring/warning recovery and one real bounded AI adjustment.
Read the [README](../README.md) for the dated results and actual recording.

StrawHats MLOps demonstrates how to run and manage a machine-learning service.
The dashboard is the control panel, Python handles validation and backend work,
and Kubernetes manages the running containers. The assignment's main challenge
is the operational workflow around a model: deploy the intended version, confirm
it can answer requests, observe its behavior, change capacity and recover a
verified configuration. The flower classifier gives those operations a small,
real workload to manage.

There is one Iris classifier, with three possible answers: setosa, versicolor and
virginica. Those names are flower classes, not three different models. An input
contains four measurements in centimeters: sepal length, sepal width, petal length
and petal width. The trained model calculates a probability for each class and
selects the largest. A prediction's probability describes that particular input;
the separate held-out evaluation measures performance across test examples.
Neither number guarantees every future prediction will be correct.

A Docker image packages the model service, Python dependencies and startup
instructions. A running instance becomes a container. Kubernetes runs that
container inside a Pod, which is its basic running unit. A worker is the machine
that supplies CPU and memory for those Pods. A Service gives requests a stable
destination while individual Pods are created or replaced. Two replicas mean
two running copies of the same model service, not two newly trained models.

Kubernetes is useful because describing a desired state is easier to manage than
manually restarting and reconnecting every container. It schedules Pods, replaces
failed instances and coordinates configuration changes. It does not train this
classifier or improve its accuracy. Our custom resource definition, or CRD, adds
a small model-specific object called ModelDeployment. The Python operator watches
that object and manages the ordinary Kubernetes Deployment and Service it needs.

Desired state and observed state are different. Clicking Scale Iris submits a
request; it does not prove another copy is ready. The operator checks the current
configuration, running Pods and ready Service endpoints before reporting success.
Scaling changes the number of replicas. CPU and memory requests reserve resources
for each replica, while limits bound usage. Rollback restores the most recent
verified ready snapshot. A later successful configuration replaces that snapshot,
so this button is not an unlimited history of every previous version.

Docker Desktop provides a local Kubernetes environment on the owner's computer.
AWS EKS provides a cloud environment for the assignment's temporary deployment.
AWS manages the Kubernetes control plane, while the worker runs our workloads.
Cloud spending pays for that managed control plane, worker compute, disks,
container registry storage, network addresses and applicable traffic. Infrastructure
can incur charges when nobody opens the dashboard. OpenAI API requests are a
separate service with separate billing; AWS credits do not pay for those requests.

Vercel has a different job: it serves the public portfolio dashboard and exported
Iris model parameters. A visitor's browser performs genuine predictions, creates
downloadable Kubernetes YAML previews and records its own session Activity. These
features remain useful without a cluster. Reloading starts a new session; previews
do not deploy anything. The private connected workspace can instead send
predictions to the actual Kubernetes Service and operate the configured cluster.
Public browser predictions must not be described as requests to a cloud model.

Monitoring tells us what the service is doing. Prometheus collects counters,
timings and resource observations; Grafana presents them as charts; metrics-server
supplies Kubernetes CPU and memory readings. Our warning demonstrates an unusual
concentration of recent predictions in one flower class. Controlled traffic can
make it fire and then recover. This signal is not proof of statistical drift:
without true answers for new inputs, a class imbalance alone cannot establish
that the classifier's accuracy has declined. It also does not automatically mean
the service needs more CPU.

LangChain connects Python to a language model for deployment guidance and bounded
resource selection. The conversational assistant recommends configurations. A
separate owner-run adjustment command uses fresh verified measurements to offer
permitted CPU or memory changes. The AI can choose one offered step or Hold;
Python validates the decision, applies at most one field change and checks the
exact resulting revision and a real prediction. This is not a continuous
autoscaler. It does not give the AI a shell, change the image or invent metrics.

The project has a finite finish line: verify the required cloud lifecycle,
inference, monitoring, warning and AI adjustment; record the actual demonstration;
complete the README, embedded GIF, live URL, contribution and professor-access
checks; and submit the materials. Those cloud checks and the recording passed
on October 17. Owner-authorized runtime and stack removal, independent resource
absence checks and temporary deployer removal are complete. Final owner review,
professor repository-access confirmation and course submission remain. The useful
public Vercel site can remain available for presentation and a resume; a fresh
browser prediction passed after AWS deletion. The dated recording preserves the
completed AWS demonstration.
