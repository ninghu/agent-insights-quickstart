targetScope = 'resourceGroup'

@description('Name of the existing Azure AI Foundry account.')
param accountName string

@description('Name of the existing child project.')
param projectName string

@description('Name of the existing Application Insights component.')
param appInsightsName string

@description('Stable name for the account-level Application Insights connection.')
param accountConnectionName string = 'appinsights-account'

@description('Stable name for the project-level Application Insights connection.')
param projectConnectionName string = 'appinsights-project'

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

// Pattern adapted from public Microsoft Learn and microsoft-foundry/foundry-samples references:
// create one App Insights connection on the account and one on the project.
resource accountAppInsightsConnection 'Microsoft.CognitiveServices/accounts/connections@2025-06-01' = {
  name: accountConnectionName
  parent: account
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

output accountConnectionId string = accountAppInsightsConnection.id
output accountConnectionName string = accountAppInsightsConnection.name
output projectConnectionId string = projectAppInsightsConnection.id
output projectConnectionName string = projectAppInsightsConnection.name
