targetScope = 'resourceGroup'

@description('Name of the Azure AI Foundry account.')
param accountName string

@description('Name of the child project.')
param projectName string

@description('Azure region for all Foundry resources.')
param location string

@description('Prompt or hosted. Used only to stamp a clear project description and choose the caller role in the parent template.')
@allowed([
  'prompt'
  'hosted'
])
param agentType string

@description('Model name for the account deployment.')
param modelName string

@description('Model version for the account deployment.')
param modelVersion string

@description('Model format for the account deployment.')
param modelFormat string

@description('Deployment name for the account deployment.')
param modelDeploymentName string

@description('SKU name for the account deployment.')
param modelSkuName string

@description('Capacity for the account deployment SKU.')
@minValue(1)
param modelSkuCapacity int

@description('Shared tags for every taggable resource.')
param tags object

var projectDescription = agentType == 'hosted'
  ? 'Scratch Azure AI Foundry project for hosted-agent insights onboarding.'
  : 'Scratch Azure AI Foundry project for prompt-agent insights onboarding.'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'S0'
  }
  tags: tags
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  name: projectName
  parent: account
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  tags: tags
  properties: {
    displayName: projectName
    description: projectDescription
  }
}

resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  name: modelDeploymentName
  parent: account
  sku: {
    capacity: modelSkuCapacity
    name: modelSkuName
  }
  tags: tags
  properties: {
    model: {
      format: modelFormat
      name: modelName
      version: modelVersion
    }
  }
}

output accountId string = account.id
output accountName string = account.name
output projectId string = project.id
output projectName string = project.name
output projectEndpoint string = project.properties.endpoints['AI Foundry API']
output projectPrincipalId string = project.identity.principalId
output modelDeploymentId string = modelDeployment.id
output modelDeploymentName string = modelDeployment.name
