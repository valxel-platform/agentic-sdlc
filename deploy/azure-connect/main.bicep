// Connects an Azure subscription to a Valxel workspace (tracker CP-404; agentic-sdlc ADR 0010).
// Deploy at subscription scope, from Azure Cloud Shell, with the command Valxel's connections
// page gives you (it fills in the parameters):
//
//   az deployment sub create --name valxel-connect --location <region> --template-file main.bicep \
//     --parameters workspace=... connectLabel=... mode=lite|full valxelIssuer=... valxelSubject=...
//
// It makes a resource group `valxel` with two user-assigned managed identities, each trusting only
// Valxel's own workload identity through a federated credential, so no secret is created or
// shared:
//
//   valxel-deploy  creates and runs your products. lite: virtual machines and their networks.
//                  full: AKS and the catalog's managed services (Contributor), and may give your
//                  products' own identities the data roles the catalog names, and no other role.
//   valxel-agents  reads: resources and their tags, metrics, costs and Defender findings.
//
// Both are tagged with your workspace and a fingerprint of its external ID, which Valxel checks
// before it uses them. To disconnect, delete the `valxel` resource group: the identities and their
// role assignments go with it. Give Valxel subscriptions of their own: Azure scopes these roles to
// the subscription, not to tags.
targetScope = 'subscription'

@description('Your Valxel workspace')
param workspace string

@description('The fingerprint of your workspace\'s external ID, from the connections page')
@minLength(40)
@maxLength(40)
param connectLabel string

@description('lite: virtual machines. full: AKS and the catalog\'s managed services')
@allowed(['lite', 'full'])
param mode string

@description('Valxel\'s OIDC issuer (its cluster\'s), which the identities trust')
param valxelIssuer string

@description('Valxel\'s service account, as that issuer names it')
param valxelSubject string

param location string = deployment().location

var tags = {
  'valxel-workspace': workspace
  'valxel-connect': connectLabel
  'valxel-mode': mode
}

// Built-in roles (Azure's fixed role definition IDs).
var role = {
  reader: 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
  costManagementReader: '72fafb9e-0641-4937-9268-a91bfd8191a3'
  monitoringReader: '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
  securityReader: '39bc4728-0917-49c7-9d2c-d95423bc2eb4'
  virtualMachineContributor: '9980e02c-c2be-4d73-94e8-173b1dc7cf3c'
  networkContributor: '4d97b98b-1d4f-4787-a291-c67834d212e7'
  managedIdentityOperator: 'f1a07417-d97a-45cb-824c-7a7467783830'
  rbacAdministrator: 'f58310d9-a9f6-439a-9e8d-f62e7b41a168'
}

// The data roles full mode may give a product's identities: the catalog's access, nothing else.
var dataRoles = [
  'ba92f5b4-2d11-453d-a403-e96b0029c9fe' // Storage Blob Data Contributor
  '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1' // Storage Blob Data Reader
  '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39' // Azure Service Bus Data Sender
  '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0' // Azure Service Bus Data Receiver
  '2b629674-e913-4c01-ae53-ef4638d8f975' // Azure Event Hubs Data Sender
  'a638d3c7-ab3a-418d-83e6-5f17a39d4fde' // Azure Event Hubs Data Receiver
  'e147488a-f6f5-4113-8e2d-b22465e65bf6' // Key Vault Crypto Service Encryption User (customer-managed keys)
]
var dataRoleList = join(dataRoles, ', ')
var delegation = '((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/write\'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${dataRoleList}})) AND ((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/delete\'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${dataRoleList}}))'

var deployRoles = mode == 'lite'
  ? [role.virtualMachineContributor, role.networkContributor, role.managedIdentityOperator]
  : [role.contributor]
var agentsRoles = [role.reader, role.costManagementReader, role.monitoringReader, role.securityReader]

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'valxel'
  location: location
  tags: tags
}

module identities 'identities.bicep' = {
  scope: group
  name: 'valxel-identities'
  params: {
    location: location
    tags: tags
    valxelIssuer: valxelIssuer
    valxelSubject: valxelSubject
  }
}

resource deployRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in deployRoles: {
    name: guid(subscription().id, 'valxel-deploy', r)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
      principalId: identities.outputs.deployPrincipalId
      principalType: 'ServicePrincipal'
      description: 'Valxel deploy (${mode} mode) for workspace ${workspace}'
    }
  }
]

resource deployDelegation 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (mode == 'full') {
  name: guid(subscription().id, 'valxel-deploy', role.rbacAdministrator)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role.rbacAdministrator)
    principalId: identities.outputs.deployPrincipalId
    principalType: 'ServicePrincipal'
    description: 'Valxel deploy may give products\' identities the catalog\'s data roles, and no other role'
    condition: delegation
    conditionVersion: '2.0'
  }
}

resource agentsRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in agentsRoles: {
    name: guid(subscription().id, 'valxel-agents', r)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
      principalId: identities.outputs.agentsPrincipalId
      principalType: 'ServicePrincipal'
      description: 'Valxel agents (read-only) for workspace ${workspace}'
    }
  }
]

@description('Paste these into Valxel\'s connections page')
output connection object = {
  subscription: subscription().subscriptionId
  tenant: tenant().tenantId
  deployClientId: identities.outputs.deployClientId
  agentsClientId: identities.outputs.agentsClientId
}
