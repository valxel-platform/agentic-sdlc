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

Each identity has a federated credential that trusts only Valxel's workload identity, so there is
no secret to create, store or rotate. Valxel signs in as one for a single job at a time.

When it finishes, the command prints the subscription, tenant and the two client IDs. Paste the
subscription and client IDs into the connections page and choose Check: Valxel signs in as each
identity and checks it was made for your workspace (its tags carry the workspace and a fingerprint
of your external ID).

To disconnect, delete the `valxel` resource group, then choose Disconnect in Valxel.

Give Valxel subscriptions of their own: Azure scopes these roles to the subscription, so anything
else in it is in reach of the deploy identity.
