# Iris on AWS EKS

This milestone prepares one EKS cluster and one stateless Iris model. **The image
is verified locally; no AWS runtime has been created or verified.** Operator,
monitoring, AI resource adjustment, and the public website are later assignment
milestones. The deployment CLI is owner-operated, not an automatic cloud CI job.

## Runtime and cost boundary

- Northern Virginia (`us-east-1`), Kubernetes 1.35 with standard support only.
- One on-demand `t3.medium` worker, fixed minimum/desired/maximum of one.
- Encrypted 20 GiB gp3 disk, Standard CPU credits, IMDSv2, no SSH key.
- Dedicated VPC with two public subnets; private cluster API plus public API
  restricted to the owner's current external IPv4 `/32`.
- Pod Identity supplies VPC CNI permissions independently of the node role.
  Only networking, DNS, proxy, and Pod Identity add-ons are installed.
- Private ECR repository with immutable tags; workload uses the actual digest.
- Iris: one replica, requests 250m/256Mi, limits 500m/512Mi, non-root UID 10001,
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
with Kubernetes 1.35. Local preparation did not install AWS CLI or create keys.
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
.\.venv\Scripts\python.exe -m scripts.cloud.deploy publish --profile strawhats --account <ACCOUNT_ID>
.\.venv\Scripts\python.exe -m scripts.cloud.deploy apply --profile strawhats --account <ACCOUNT_ID>
.\.venv\Scripts\python.exe -m scripts.cloud.deploy verify --profile strawhats --account <ACCOUNT_ID>
```

Create rechecks the reviewed template, parameters, ownership tags, identity,
eligibility, and future end time. It awaits stack completion and checks exactly
one Ready worker. Publish requires a clean Git checkout, builds a commit-SHA
Linux image, verifies that actual rebuilt container before ECR login/push, and
records its digest and the Linux/amd64 manifest/config identities (including
OCI indexes with separate attestations). The ECR password goes directly to Docker's stdin; temporary
task-only Docker credentials are removed afterward. Apply renders an ignored
digest overlay and awaits rollout. Verify checks workload security/resources,
the published release digest, its owned Ready pod and ready Service EndpointSlice,
real HTTP inference, invalid-input rejection and metrics, then restarts the pod
and repeats those checks on a replacement pod. HTTP verification targets the
verified pod through a loopback port-forward;
this milestone does not expose the model or management API publicly.

Evidence stays under `.local/milestone-one/`, including source SHA, image digest,
timestamps and measured responses. Successful image publication or rollout alone
does not mean cloud inference passed. Release mutations stop after the approved
end; each cloud/Kubernetes mutation rechecks the deadline, including after long
builds or preflight reads. Cleanup remains available. A failure/timeout stops subsequent operations.
Safe failure records in `.local/milestone-one/command-failures.jsonl` retain the
time, operation, status, stack/change-set identifiers, and allowlisted AWS or
kubectl error codes. Arbitrary process output and credential-operation output
are excluded. Inspect these records and AWS stack events for fuller diagnosis.
Inspect AWS stack events and current state before retrying; a partially created
stack can incur charges even when the CLI reports failure. Do not repeatedly
create replacement stacks, overwrite immutable tags, or delete other resources.

## Explicit cleanup

Inspect the project stack ARN and its resource list first. Shutdown requires
separate owner authorization. Supply its exact ARN to the cleanup command:

```powershell
.\.venv\Scripts\python.exe -m scripts.cloud.deploy cleanup --profile strawhats --account <ACCOUNT_ID> --confirm-delete <INSPECTED_PROJECT_STACK_ARN>
```

Cleanup verifies the account, named owner, and project/milestone tags, empties
only the project's ECR repository, deletes the stack, waits, and checks that the
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
