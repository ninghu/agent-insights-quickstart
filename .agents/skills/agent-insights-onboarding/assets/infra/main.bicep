targetScope = 'resourceGroup'

@description('Globally unique prefix used when deriving resource names.')
@minLength(2)
@maxLength(12)
param namePrefix string

@description('Globally unique suffix used when deriving resource names.')
@minLength(4)
@maxLength(12)
param nameSuffix string

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Object ID of the Entra user that starts the deployment.')
param initiatingUserObjectId string

@description('Prompt grants the caller Foundry User on the project. Hosted grants Foundry Project Manager on the project.')
@allowed([
  'prompt'
  'hosted'
])
param agentType string = 'prompt'

@description('Model name for the single deployment created on the Foundry account.')
param modelName string = 'gpt-5.4'

@description('Model version for the single deployment created on the Foundry account.')
param modelVersion string = '2026-03-05'

@description('Model format for the single deployment created on the Foundry account.')
param modelFormat string = 'OpenAI'

@description('Deployment name for the model deployment.')
param modelDeploymentName string = 'gpt-5-4'

@description('SKU name for the model deployment.')
param modelSkuName string = 'GlobalStandard'

@description('Capacity for the model deployment SKU.')
@minValue(1)
param modelSkuCapacity int = 30

@description('When true, grants Privileged Monitoring Data Reader on the Log Analytics workspace to the project identity and current user.')
param grantPrivilegedMonitoringDataReader bool = true

@description('Shared tags for every taggable resource. Include created-by, run-id, and owner-object-id.')
param tags object

@description('Retention in days for the Log Analytics workspace.')
@minValue(30)
param logAnalyticsRetentionInDays int = 30

var prefixToken = toLower(replace(replace(namePrefix, '-', ''), '_', ''))
var suffixToken = toLower(replace(replace(nameSuffix, '-', ''), '_', ''))
var labelPrefix = toLower(replace(namePrefix, '_', '-'))
var labelSuffix = toLower(replace(nameSuffix, '_', '-'))
// Keep the full run suffix so a soft-deleted Foundry account from an earlier run never
// collides with a later run that happens to share the same prefix.
var baseToken = '${take(prefixToken, 8)}${suffixToken}'
var baseLabel = take('${labelPrefix}-${labelSuffix}', 48)

var foundryAccountName = take('ai${baseToken}', 24)
var foundryProjectName = take('proj-${baseLabel}', 64)
var appInsightsName = take('appi-${baseLabel}', 260)
var logAnalyticsName = take('log-${baseLabel}', 63)
var projectConnectionName = 'appinsights'

var effectiveTags = union(tags, {
  'created-by': contains(tags, 'created-by') ? string(tags['created-by']) : 'azure-cli'
  'run-id': contains(tags, 'run-id') ? string(tags['run-id']) : deployment().name
  'owner-object-id': initiatingUserObjectId
})

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    appInsightsName: appInsightsName
    location: location
    logAnalyticsName: logAnalyticsName
    retentionInDays: logAnalyticsRetentionInDays
    tags: effectiveTags
  }
}

module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  params: {
    accountName: foundryAccountName
    agentType: agentType
    location: location
    modelDeploymentName: modelDeploymentName
    modelFormat: modelFormat
    modelName: modelName
    modelSkuCapacity: modelSkuCapacity
    modelSkuName: modelSkuName
    modelVersion: modelVersion
    projectName: foundryProjectName
    tags: effectiveTags
  }
}

module connections 'modules/connections.bicep' = {
  name: 'connections'
  params: {
    accountName: foundry.outputs.accountName
    appInsightsName: monitoring.outputs.appInsightsName
    projectConnectionName: projectConnectionName
    projectName: foundry.outputs.projectName
  }
}

module roleAssignments 'modules/role-assignments.bicep' = {
  name: 'roleAssignments'
  params: {
    accountName: foundry.outputs.accountName
    agentType: agentType
    appInsightsName: monitoring.outputs.appInsightsName
    grantPrivilegedMonitoringDataReader: grantPrivilegedMonitoringDataReader
    initiatingUserObjectId: initiatingUserObjectId
    logAnalyticsName: monitoring.outputs.logAnalyticsName
    projectName: foundry.outputs.projectName
    projectPrincipalId: foundry.outputs.projectPrincipalId
  }
}

output foundryAccountId string = foundry.outputs.accountId
output foundryAccountName string = foundry.outputs.accountName
output foundryProjectId string = foundry.outputs.projectId
output foundryProjectName string = foundry.outputs.projectName
output foundryProjectEndpoint string = foundry.outputs.projectEndpoint
output foundryProjectPrincipalId string = foundry.outputs.projectPrincipalId
output logAnalyticsWorkspaceId string = monitoring.outputs.logAnalyticsId
output logAnalyticsWorkspaceName string = monitoring.outputs.logAnalyticsName
output applicationInsightsId string = monitoring.outputs.appInsightsId
output applicationInsightsName string = monitoring.outputs.appInsightsName
output projectAppInsightsConnectionId string = connections.outputs.projectConnectionId
output projectAppInsightsConnectionName string = connections.outputs.projectConnectionName
output modelDeploymentId string = foundry.outputs.modelDeploymentId
output modelDeploymentName string = foundry.outputs.modelDeploymentName
