# Iris on AWS EKS

The owner release prepares one temporary EKS cluster with the existing Iris
operator/CRD, model service, Prometheus, Grafana and metrics-server. **No AWS runtime
or live AI-selected resource adjustment has been verified.** The public portfolio
runs independently; cluster controls stay in the owner's loopback-only workspace.
Local rehearsal uses Docker Desktop and is never evidence of an AWS deployment.

## Runtime and cost boundary

- Northern Virginia (`us-east-1`), Kubernetes 1.35 with standard support only.
- One on-demand `t3.medium` worker, fixed minimum/desired/maximum of one.
- Encrypted 20 GiB gp3 disk, Standard CPU credits, IMDSv2, no SSH key.
- Dedicated VPC with two public subnets; private cluster API plus public API
  restricted to the owner's current external IPv4 `/32`.
- Pod Identity supplies VPC CNI permissions independently of the node role.
  Only networking, DNS, proxy, and Pod Identity add-ons are installed.
- Two private ECR repositories with immutable tags: Iris and operator. Both use verified digests.
- Iris: initially one replica, requests 250m/256Mi, limits 500m/512Mi, non-root UID 10001,
  read-only root, dropped capabilities, no service-account token, health probes,
  ClusterIP service. No model storage volume is required.
- No NAT gateway, load balancer, Auto Mode, GPU, or node autoscaler.

The planning estimate was approximately $3.60/day plus storage, public IPv4,
registry, traffic, and other usage. Recheck prices and eligible credits before
release. A budget argument records approval; it is **not a spending cap or an
automatic shutdown**. Scaling the worker to zero still leaves control-plane
charges. Delete the project stack and verify cleanup at the approved end.

The intended live window is at most 72 hours around the presentation. Choose
the actual UTC end and total usage budget (including credits) before cloud
preparation. Owner approval of a local template is not approval to run it.

## Access and preflight

Use current AWS CLI v2, Docker with Buildx and Linux/amd64, Python 3.12, and kubectl compatible
with Kubernetes 1.35. AWS CLI is installed; account access remains unverified.
The initial browser session was root; release requires a named non-root owner
with the appropriate EKS/CloudFormation/IAM/network/ECR permissions and MFA.
Configure that access interactively when authorized. Never share passwords,
MFA codes, access keys, or session tokens through prompts or commit them.

Use the AWS CLI's interactive login with the named owner, for example:

```powershell
aws login --profile strawhats
.\.venv\Scripts\python.exe -m scripts.cloud.deploy preflight --profile strawhats --account <ACCOUNT_ID>
```

Every AWS invocation uses the supplied profile and `us-east-1`; kubectl always
names the exact EKS ARN context and the ignored project kubeconfig. The owner's
normal Docker authentication and default Kubernetes context are preserved.
Account mismatch and root credentials stop the sequence.

Preflight reads the account plan, standard-support version, instance eligibility,
available AZs/offerings, and EC2/EKS quota ceilings. Check **remaining capacity**,
service restrictions, and credit coverage in AWS as well: quota ceilings are not
remaining capacity, and EC2 eligibility does not prove EKS eligibility. A Free
plan without confirmed `t3.medium` eligibility stops. An unavailable account-plan
API also stops. No command upgrades the plan or increases quotas. If AWS requires
a paid-plan conversion or an architecture change, bring that decision to the owner.

## Reviewed release sequence

Run commands from the repository root. Replace placeholders only after the owner
chooses the live window and confirms EKS/EC2 eligibility and credit coverage.
The following commands contact AWS; **they were not executed during local preparation**.

```powershell
.\.venv\Scripts\python.exe -m scripts.cloud.deploy preview --profile strawhats --account <ACCOUNT_ID> --owner-arn <IAM_USER_OR_ROLE_ARN> --admin-cidr <CURRENT_PUBLIC_IPV4/32> --az-a <AZ_A> --az-b <AZ_B> --budget-usd <APPROVED_USAGE_USD> --end-at <UTC_ISO_TIMESTAMP> --eligibility-confirmed
```

Preview rejects an existing cluster/stack, validates the template in AWS, and
creates a **CREATE** change set without executing it. Its recovery receipt and
full change list are `.local/milestone-one/preview.json` and `change-set.json`.
Inspect the actual resources, IAM permissions, account, region, access CIDR,
budget, and end time. Review before supplying the exact change-set ARN:

```powershell
.\.venv\Scripts\python.exe -m scripts.cloud.deploy create --profile strawhats --account <ACCOUNT_ID> --confirm-execute <REVIEWED_CHANGE_SET_ARN>
.\.venv\Scripts\python.exe -m scripts.cloud.owner_release publish --profile strawhats --account <ACCOUNT_ID>
.\.venv\Scripts\python.exe -m scripts.cloud.owner_release install --profile strawhats --account <ACCOUNT_ID> --receipt .local/owner-eks/runtime.json
.\.venv\Scripts\python.exe -m scripts.cloud.owner_release verify --profile strawhats --account <ACCOUNT_ID> --receipt .local/owner-eks/runtime.json
```

Publish requires a clean committed SHA with successful **main push CI** for that
exact SHA: backend, frontend and monitoring jobs. It builds and checks both
Linux/amd64 images, then verifies their actual ECR manifest/config identities.
`.local/owner-eks/images.json` records the complete pair. An interrupted publication
keeps `publication-progress.json` separate and incomplete; it cannot release a mixed
pair. Docker credentials are task-local and removed after publication.

Install renders fixed namespaces/RBAC/CRD/operator/ModelDeployment and monitoring
from maintained sources. It records each accepted object UID, preserving partial
inventory on failure. Matching shared CRD/addon objects are borrowed; foreign or
legacy Iris resources stop installation. No second worker is added if capacity is
insufficient. Server-side acceptance is not readiness: `verify` checks the exact
operator, images, owned ready Pods, EndpointSlices and real Service inference.

The legacy `scripts.cloud.deploy publish/apply/verify` commands serve the earlier
raw Deployment milestone only. They do not complete the operator assignment.
Legacy apply refuses operator-owned Iris. Use `owner_release` for this project.

Every mutation rechecks the account, named non-root owner, owned stack, approved
window and release binding after slow work. Failed/expired bindings never fall
back to Docker Desktop. Cleanup may run after expiry. The dedicated release
kubeconfig contains transport metadata and a controlled named-profile token helper;
normal Docker Desktop configuration and authentication are preserved.

### Owner workspace and runtime commands

Set these **backend-only** values in ignored `.env.local`, then start the API on
127.0.0.1:8000 and the normal workspace frontend on 127.0.0.1:5173:

```dotenv
OWNER_CLUSTER_TARGET=eks
EKS_AWS_PROFILE=strawhats
EKS_EXPECTED_ACCOUNT=<ACCOUNT_ID>
EKS_RELEASE_RECEIPT=<ABSOLUTE_RUNTIME_RECEIPT_PATH>
```

`OWNER_CLUSTER_TARGET=local` preserves the existing Docker Desktop panel and its
`LOCAL_OPERATOR_ENABLED=1` opt-in. `rehearsal` uses `REHEARSAL_RECEIPT` instead of
AWS identity. Restart the API after changing selection. The descriptor reports
configuration, not connectivity; status/action responses show actual provenance.
The browser cannot choose an account, context, namespace or image. The standalone
portfolio disables these operations and makes no provider or cluster request.

Keep only the selected runtime's monitoring forwarded to **127.0.0.1:9090/3000**.
`managed_forwards(connection)` in `scripts.cloud.runtime_forwards` owns these two
kubectl children, refuses occupied ports and stops only its own children. Use the
validated release kubeconfig and exact context when forwarding manually. Never
reuse an old local forward to demonstrate EKS metrics. Observations additionally
verify current owned Pods, image, source timestamps and recording rules.

```powershell
python -m scripts.cloud.observe_iris --target eks --profile strawhats --account <ACCOUNT_ID> --receipt .local/owner-eks/runtime.json
python -m scripts.cloud.traffic_iris --target eks --profile strawhats --account <ACCOUNT_ID> --receipt .local/owner-eks/runtime.json --run-demo
python -m scripts.cloud.adjust_iris --target eks --profile strawhats --account <ACCOUNT_ID> --receipt .local/owner-eks/runtime.json
# Only after a successful live preview and owner approval:
python -m scripts.cloud.adjust_iris --target eks --profile strawhats --account <ACCOUNT_ID> --receipt .local/owner-eks/runtime.json --apply
```

Adjustment defaults to preview and uses the existing server-only key. It offers
Hold or one bounded CPU/memory ladder step, preserves 1–2 replicas, enforces fresh
telemetry and a five-minute cooldown, and verifies the exact accepted revision
and real prediction. Failed known rollouts permit one guarded restoration with
its own 180-second deadline. Unknown write outcomes never retry blindly. A
rate-limited/missing provider is an unavailable outcome, not a successful AI demo.
Evidence is separated by runtime in `.local/owner-eks/` and stays outside Git.

### Local release rehearsal

This workflow creates only dedicated Docker Desktop namespaces `strawhats` and
`strawhats-monitoring`. It refuses collisions, builds/imports real source-SHA model
and operator images, and preserves the original `*-local` workloads and borrowed
CRD/metrics-server. Start from a clean committed branch:

```powershell
python -m scripts.cloud.rehearse prepare --receipt .local/owner-eks/rehearsal/runtime.json
python -m scripts.cloud.rehearse verify --receipt .local/owner-eks/rehearsal/runtime.json
python -m scripts.cloud.rehearse cleanup --receipt .local/owner-eks/rehearsal/runtime.json
```

For WSL, use Linux Python3.12 and pass the existing Windows kubeconfig as
`--kubeconfig /mnt/c/Users/<USER>/.kube/config` to `prepare`. It reads certificate
authentication without copying credentials. WSL can resolve Windows `aws.exe`;
the version bridge was checked without sign-in or an AWS endpoint call. Use one
consistent runtime/path convention for a receipt. If Windows blocks native orjson,
use Linux Python; do not disable Windows application security.

Cleanup uses recorded UIDs and refuses foreign children in owned namespaces.
Inspect the partial receipt after any failure; do not reset Kubernetes or delete
namespaces manually to get around an ownership failure. Local acceptance and
controlled provider tests do not establish live EKS or live AI acceptance.

## Explicit cleanup

Inspect the project stack ARN and its resource list first. Shutdown requires
separate owner authorization. Remove the runtime first, then supply the exact stack ARN. Both stages require
owner authorization; this preparation stage executes neither AWS command:

```powershell
python -m scripts.cloud.owner_release remove --profile strawhats --account <ACCOUNT_ID> --receipt .local/owner-eks/runtime.json
```

```powershell
.\.venv\Scripts\python.exe -m scripts.cloud.deploy cleanup --profile strawhats --account <ACCOUNT_ID> --confirm-delete <INSPECTED_PROJECT_STACK_ARN>
```

Cleanup verifies the account, named owner, and project/milestone tags, empties
only both verified project ECR repositories, deletes the stack, waits, and checks that the
cluster, worker instances, tagged disks, tagged elastic IPs, and VPC network
interfaces are gone. Review partial-failure stack events if cleanup stops.
Resources already deleted in a previous attempt may need manual, individually
verified recovery; the command never adopts an unrelated cluster. Keep all Git
branches after release or merge.

An unexecuted preview may leave a `REVIEW_IN_PROGRESS` stack. Inspect its ARN,
change set and resource list, then remove that empty preview through AWS after
authorization; the runtime cleanup command requires an identifiable project VPC.

## Local verification

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q --disable-plugin-autoload
.\.venv\Scripts\python.exe -m ruff check backend scripts/cloud
.\.venv\Scripts\python.exe -m ruff format --check backend scripts/cloud
kubectl kustomize infra/kubernetes/iris
# Optional isolated local CloudFormation schema validator:
cfn-lint -t infra/aws/cluster.yaml -r us-east-1
```

The template passed cfn-lint 1.57.2 resource-schema validation locally. Kubernetes
manifests rendered locally, and the hardened model container passed actual HTTP
checks before and after restart. AWS template validation, IAM/API permissions,
Free-plan eligibility, add-on/node bootstrap, and cloud inference remain pending
the authorized live window. Offline tests do not establish those outcomes.

AWS references: [EKS CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-eks-cluster.html),
[node IAM role](https://docs.aws.amazon.com/eks/latest/userguide/create-node-role.html),
[account plan state](https://docs.aws.amazon.com/cli/latest/reference/freetier/get-account-plan-state.html),
[Kubernetes version availability](https://docs.aws.amazon.com/cli/latest/reference/eks/describe-cluster-versions.html).
