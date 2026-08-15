targetScope = 'subscription'

@description('Foundry account resource group.')
param foundryResourceGroup string

@description('Foundry account name.')
param foundryAccountName string

@description('Foundry project name.')
param foundryProjectName string

@description('Application Insights subscription ID.')
param applicationInsightsSubscriptionId string

@description('Application Insights resource group.')
param applicationInsightsResourceGroup string

@description('Application Insights component name.')
param applicationInsightsName string

@description('Project-level connection name.')
param projectConnectionName string

module connections 'modules/existing-connections.bicep' = {
  name: 'existingAppInsightsConnections'
  scope: resourceGroup(foundryResourceGroup)
  params: {
    accountName: foundryAccountName
    applicationInsightsName: applicationInsightsName
    applicationInsightsResourceGroup: applicationInsightsResourceGroup
    applicationInsightsSubscriptionId: applicationInsightsSubscriptionId
    projectName: foundryProjectName
    projectConnectionName: projectConnectionName
  }
}

output projectConnectionId string = connections.outputs.projectConnectionId
