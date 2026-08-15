targetScope = 'resourceGroup'

@description('Name of the existing Azure AI Foundry account.')
param accountName string

@description('Name of the existing child project.')
param projectName string

@description('Name of the existing Application Insights component.')
param appInsightsName string

@description('Stable name for the project-level Application Insights connection.')
param projectConnectionName string = 'appinsights'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  name: projectName
  parent: account
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource projectAppInsightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  name: projectConnectionName
  parent: project
  properties: {
    authType: 'ApiKey'
    category: 'AppInsights'
    credentials: {
      key: appInsights.properties.ConnectionString
    }
    isSharedToAll: true
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsights.id
    }
    target: appInsights.id
  }
}

output projectConnectionId string = projectAppInsightsConnection.id
output projectConnectionName string = projectAppInsightsConnection.name
