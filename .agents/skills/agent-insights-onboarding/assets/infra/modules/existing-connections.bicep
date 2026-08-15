targetScope = 'resourceGroup'

param accountName string
param projectName string
param applicationInsightsSubscriptionId string
param applicationInsightsResourceGroup string
param applicationInsightsName string
param accountConnectionName string
param projectConnectionName string
param createAccountConnection bool

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  name: projectName
  parent: account
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
  scope: resourceGroup(applicationInsightsSubscriptionId, applicationInsightsResourceGroup)
}

resource accountConnection 'Microsoft.CognitiveServices/accounts/connections@2025-06-01' = if (createAccountConnection) {
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

resource existingAccountConnection 'Microsoft.CognitiveServices/accounts/connections@2025-06-01' existing = if (!createAccountConnection) {
  name: accountConnectionName
  parent: account
}

resource projectConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
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

output accountConnectionId string = createAccountConnection
  ? accountConnection.id
  : existingAccountConnection.id
output projectConnectionId string = projectConnection.id
