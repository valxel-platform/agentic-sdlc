// Full mode (main.bicep): the data plane's managed identities, in the `valxel` resource group, and
// the storage account its OpenTofu state lives in. The identities trust nothing yet: the data plane's
// stack adds a federated credential for its cluster's service account when it builds the cluster.
// Their roles on the subscription are main.bicep's.
param location string
param tags object
param names array
param deployPrincipalId string
param stateAccountName string

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [
  for name in names: {
    name: 'valxel-${name}'
    location: location
    tags: tags
  }
]

// The state: private to Entra sign-in (no shared keys), TLS 1.2, versioned blobs, never public.
resource state 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: stateAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_ZRS' }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    defaultToOAuthAuthentication: true
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: state
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: { enabled: true, days: 30 }
  }
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'tfstate'
  properties: { publicAccess: 'None' }
}

// Storage Blob Data Contributor, on this account only.
resource deployState 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(state.id, deployPrincipalId, 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  scope: state
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: deployPrincipalId
    principalType: 'ServicePrincipal'
    description: 'Valxel deploy writes the data plane\'s OpenTofu state'
  }
}

// In the order of `names`.
output identities array = [
  for (name, i) in names: {
    key: name
    id: identity[i].id
    clientId: identity[i].properties.clientId
    principalId: identity[i].properties.principalId
    name: identity[i].name
  }
]

output stateAccount string = state.name
