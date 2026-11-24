// The two identities Valxel signs in as (main.bicep), in the `valxel` resource group. Each trusts
// only Valxel's own workload identity: its issuer, its service account, Azure's token-exchange
// audience.
param location string
param tags object
param valxelIssuer string
param valxelSubject string

var jobs = ['deploy', 'agents']

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [
  for job in jobs: {
    name: 'valxel-${job}'
    location: location
    tags: tags
  }
]

resource trust 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = [
  for (job, i) in jobs: {
    parent: identity[i]
    name: 'valxel'
    properties: {
      issuer: valxelIssuer
      subject: valxelSubject
      audiences: ['api://AzureADTokenExchange']
    }
  }
]

output deployPrincipalId string = identity[0].properties.principalId
output deployClientId string = identity[0].properties.clientId
output agentsPrincipalId string = identity[1].properties.principalId
output agentsClientId string = identity[1].properties.clientId
