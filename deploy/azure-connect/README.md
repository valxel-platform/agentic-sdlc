# Connect an Azure subscription to Valxel

Valxel's connections page opens [Azure Cloud Shell](https://shell.azure.com) and gives you one
command: it clones this folder and deploys `main.bicep` to your subscription with your workspace's
values. Run it as someone who can assign roles on the subscription (Owner, or User Access
Administrator with Contributor).

What it makes, in a resource group named `valxel`:

| Identity | Roles on the subscription | What Valxel uses it for |
|---|---|---|
| `valxel-deploy` | lite: Virtual Machine Contributor, Network Contributor, Managed Identity Operator. full: Contributor, and Role Based Access Control Administrator limited to the catalog's data roles | Creating and running your products |
| `valxel-agents` | Reader, Cost Management Reader, Monitoring Reader, Security Reader | Inventory, costs, alerts and Defender findings |

In **full mode** it also makes the data plane's identities, which Valxel's data plane runs as once it
builds the product's AKS cluster here (each trusts nothing until then: the cluster's build adds a
federated credential for its own service account), and a storage account (`vxst…`, Entra sign-in only)
for the data plane's OpenTofu state:

| Identity | Roles on the subscription |
|---|---|
| `valxel-aks` | Network Contributor, Key Vault Crypto User (the cluster joins its subnet, reads the etcd key) |
| `valxel-crossplane` | Contributor, and the same data-roles-only delegation as deploy |
| `valxel-db-logins` | none: each product database's Entra admin, set by the catalog |
| `valxel-cert-manager` | DNS Zone Contributor |
| `valxel-external-secrets` | Key Vault Secrets User |

and gives `valxel-deploy` Azure Kubernetes Service RBAC Cluster Admin and Key Vault Crypto Officer, to
build the cluster and its keys.

Each identity has a federated credential that trusts only Valxel's workload identity, so there is
no secret to create, store or rotate. Valxel signs in as one for a single job at a time.

When it finishes, the command prints the subscription, tenant and the two client IDs. Paste the
subscription and client IDs into the connections page and choose Check: Valxel signs in as each
identity and checks it was made for your workspace (its tags carry the workspace and a fingerprint
of your external ID).

To disconnect, delete the `valxel` resource group, then choose Disconnect in Valxel.

Give Valxel subscriptions of their own: Azure scopes these roles to the subscription, so anything
else in it is in reach of the deploy identity.
