targetScope = 'resourceGroup'

@description('Name of the existing Azure AI Foundry account.')
param accountName string

@description('Name of the existing child project.')
param projectName string

@description('Name of the existing Application Insights component.')
param appInsightsName string

@description('Name of the existing Log Analytics workspace.')
param logAnalyticsName string

@description('Object ID of the project managed identity.')
param projectPrincipalId string

@description('Object ID of the current Entra user running the deployment.')
param initiatingUserObjectId string

@description('Prompt grants the caller Foundry User. Hosted grants Foundry Project Manager.')
@allowed([
  'prompt'
  'hosted'
])
param agentType string

@description('When true, grant Privileged Monitoring Data Reader on the Log Analytics workspace to the project identity and current user.')
param grantPrivilegedMonitoringDataReader bool = true

var foundryUserRoleGuid = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
var foundryProjectManagerRoleGuid = 'eadc314b-1a2d-4efa-be10-5d325db5065e'
var cognitiveServicesOpenAIUserRoleGuid = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var monitoringReaderRoleGuid = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
var privilegedMonitoringDataReaderRoleGuid = 'dbc9c667-e97f-4491-aee6-90b9cf960190'
var callerProjectRoleGuid = agentType == 'hosted' ? foundryProjectManagerRoleGuid : foundryUserRoleGuid

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

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

resource projectManagedIdentityModelUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, projectPrincipalId, cognitiveServicesOpenAIUserRoleGuid)
  scope: account
  properties: {
    principalId: projectPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUserRoleGuid)
  }
}

resource projectManagedIdentityMonitoringReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, projectPrincipalId, monitoringReaderRoleGuid)
  scope: appInsights
  properties: {
    principalId: projectPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleGuid)
  }
}

resource projectManagedIdentityPrivilegedMonitoringDataReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (grantPrivilegedMonitoringDataReader) {
  name: guid(logAnalytics.id, projectPrincipalId, privilegedMonitoringDataReaderRoleGuid)
  scope: logAnalytics
  properties: {
    principalId: projectPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', privilegedMonitoringDataReaderRoleGuid)
  }
}

resource callerProjectRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, initiatingUserObjectId, callerProjectRoleGuid)
  scope: project
  properties: {
    principalId: initiatingUserObjectId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', callerProjectRoleGuid)
  }
}

resource callerMonitoringReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, initiatingUserObjectId, monitoringReaderRoleGuid)
  scope: appInsights
  properties: {
    principalId: initiatingUserObjectId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleGuid)
  }
}

resource callerPrivilegedMonitoringDataReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (grantPrivilegedMonitoringDataReader) {
  name: guid(logAnalytics.id, initiatingUserObjectId, privilegedMonitoringDataReaderRoleGuid)
  scope: logAnalytics
  properties: {
    principalId: initiatingUserObjectId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', privilegedMonitoringDataReaderRoleGuid)
  }
}
