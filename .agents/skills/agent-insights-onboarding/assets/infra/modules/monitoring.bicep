targetScope = 'resourceGroup'

@description('Azure region for the monitoring resources.')
param location string

@description('Name of the workspace-based Log Analytics workspace.')
param logAnalyticsName string

@description('Name of the Application Insights component.')
param appInsightsName string

@description('Shared tags for every taggable resource.')
param tags object

@description('Retention in days for the Log Analytics workspace.')
@minValue(30)
param retentionInDays int = 30

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

output appInsightsId string = appInsights.id
output appInsightsName string = appInsights.name
output logAnalyticsId string = logAnalytics.id
output logAnalyticsName string = logAnalytics.name
